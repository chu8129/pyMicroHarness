[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$User,
    [Parameter(Mandatory = $true, Position = 1)]
    [string]$Server,
    [int]$Port = 22,
    [string]$Identity,
    [string]$RemoteDir = "/tmp",
    [string]$Command,
    [string[]]$Upload
)

$ErrorActionPreference = "Stop"

function Fail([string]$Message, [int]$Code) {
    Write-Output $Message
    exit $Code
}

function Resolve-Exe([string]$Name) {
    $cmd = Get-Command $Name -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source) { return $cmd.Source }
    $fallback = Join-Path $env:WINDIR ("System32\OpenSSH\" + $Name + ".exe")
    if (Test-Path -LiteralPath $fallback) { return $fallback }
    return $null
}

function Invoke-Probe([string]$Ssh, [string[]]$BaseArgs) {
    $ErrorActionPreference = "Continue"
    $lines = & $Ssh @BaseArgs "echo win-linux-deploy-ok" 2>&1
    return @{ Code = $LASTEXITCODE; Text = (@($lines) | ForEach-Object { "$_" }) -join "`n" }
}

if ($env:OS -ne "Windows_NT") { Fail "This script runs on Windows." 1 }
if ($User -match "[\s@]" -or $Server -match "[\s@]") { Fail "User and Server must not contain spaces or @." 1 }
if ($Port -lt 1 -or $Port -gt 65535) { Fail "Port must be 1-65535." 1 }
$uploadItems = @($Upload | Where-Object { $_ })
$hasWork = [bool]$Command -or ($uploadItems.Count -gt 0)
if ($hasWork -and -not $RemoteDir) { Fail "RemoteDir is required with -Command or -Upload." 1 }
if ($RemoteDir -notmatch "^/") { Fail "RemoteDir must be an absolute Linux path." 1 }
if ($RemoteDir -match "[\r\n]") { Fail "RemoteDir must not contain newlines." 1 }
foreach ($item in $uploadItems) {
    if (-not (Test-Path -LiteralPath $item)) { Fail "Upload not found: $item" 1 }
}

$ssh = Resolve-Exe "ssh"
$keygen = Resolve-Exe "ssh-keygen"
$scp = Resolve-Exe "scp"
if (-not $ssh -or -not $keygen) {
    Fail "OpenSSH client not found. Install as admin: Add-WindowsCapability -Online -Name OpenSSH.Client~~~~0.0.1.0" 1
}

$keyPath = $Identity
if (-not $keyPath) {
    $keyPath = Join-Path $env:USERPROFILE ".ssh\id_ed25519"
    if (-not (Test-Path -LiteralPath $keyPath)) {
        $rsaPath = Join-Path $env:USERPROFILE ".ssh\id_rsa"
        if (Test-Path -LiteralPath $rsaPath) { $keyPath = $rsaPath }
    }
}
if (-not (Test-Path -LiteralPath $keyPath)) {
    if ($Identity) { Fail "Identity not found: $keyPath" 1 }
    $keyDir = Split-Path -Parent $keyPath
    if (-not (Test-Path -LiteralPath $keyDir)) { New-Item -ItemType Directory -Path $keyDir | Out-Null }
    $name = if ($env:USERNAME) { $env:USERNAME } else { "windows" }
    $comment = ($name -replace "[^A-Za-z0-9._-]", "") + "@windows"
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $keygen
    $psi.Arguments = '-t ed25519 -q -f "' + $keyPath + '" -N "" -C "' + $comment + '"'
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardInput = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $proc = New-Object System.Diagnostics.Process
    $proc.StartInfo = $psi
    [void]$proc.Start()
    $proc.StandardInput.Close()
    [void]$proc.StandardOutput.ReadToEnd()
    $keyErr = $proc.StandardError.ReadToEnd()
    $proc.WaitForExit()
    if ($proc.ExitCode -ne 0) { Fail ("ssh-keygen failed: " + $keyErr) 1 }
}
$pubPath = "$keyPath.pub"
if (-not (Test-Path -LiteralPath $pubPath)) { Fail "Public key missing: $pubPath" 1 }
[Console]::Error.WriteLine("IDENTITY=$keyPath")

$target = "$User@$Server"
$batch = @(
    "-i", $keyPath,
    "-o", "IdentitiesOnly=yes",
    "-o", "BatchMode=yes",
    "-o", "PreferredAuthentications=publickey",
    "-o", "GSSAPIAuthentication=no",
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "ConnectTimeout=10",
    "-p", "$Port",
    $target
)

$ErrorActionPreference = "Continue"
$probe = Invoke-Probe $ssh $batch
$authed = ($probe.Code -eq 0) -and ($probe.Text.Contains("win-linux-deploy-ok"))

if (-not $authed) {
    if ($probe.Text -match "REMOTE HOST IDENTIFICATION HAS CHANGED|Host key verification failed") {
        Fail ("HOST_KEY_CHANGED`n" + $probe.Text) 3
    }
    if ($probe.Text -match "UNPROTECTED PRIVATE KEY|bad permissions") {
        Fail ("KEY_PERMISSIONS`n" + $probe.Text) 1
    }
    if ($probe.Text -match "Could not resolve|Connection refused|Connection timed out|Operation timed out|No route to host|Network is unreachable") {
        Fail ("CONNECT_FAILED`n" + $probe.Text) 1
    }

    $human = -not [Console]::IsInputRedirected
    if ($human) {
        try { $null = [Console]::WindowHeight } catch { $human = $false }
    }

    if (-not $human) {
        $humanCmd = "powershell -NoProfile -ExecutionPolicy Bypass -File '" + $PSCommandPath + "' -User '" + $User + "' -Server '" + $Server + "' -Port $Port"
        if ($PSBoundParameters.ContainsKey("Identity") -and $Identity) { $humanCmd += " -Identity '" + $Identity + "'" }
        $argList = @("-NoExit", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $PSCommandPath, "-User", $User, "-Server", $Server, "-Port", "$Port")
        if ($PSBoundParameters.ContainsKey("Identity") -and $Identity) { $argList += @("-Identity", $Identity) }
        Start-Process -FilePath "powershell.exe" -ArgumentList $argList | Out-Null
        Fail ("NEED_HUMAN_PASSWORD`nA terminal is open for passwordless setup. Type the server password at the password: prompt; do not paste it into the chat.`nHUMAN_COMMAND=" + $humanCmd) 2
    }

    $line = @(Get-Content -LiteralPath $pubPath | Where-Object { $_.Trim() -ne "" } | Select-Object -First 1)
    if (-not $line) { Fail "Public key is empty: $pubPath" 1 }
    $parts = @($line.Trim() -split "\s+")
    if ($parts.Count -lt 2) { Fail "Public key is not one ssh line: $pubPath" 1 }
    $material = $parts[0] + " " + $parts[1]
    if ($material -notmatch "^ssh-(ed25519|rsa|ecdsa-sha2-nistp256|ecdsa-sha2-nistp384|ecdsa-sha2-nistp521) AAAA[A-Za-z0-9+/=]+$") {
        Fail "Unsupported public key type. Expected ed25519, rsa, or ecdsa." 1
    }

    $key = "'" + $material + "'"
    $remote = 'umask 077 && mkdir -p ~/.ssh && chmod 700 ~/.ssh && touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys && { grep -qxF -- ' + $key + ' ~/.ssh/authorized_keys || printf ''%s\n'' ' + $key + ' >> ~/.ssh/authorized_keys; }'
    Write-Host ("SSH " + $User + "@" + $Server + " port " + $Port)
    Write-Host "Type the server password at the password: prompt. It is not saved. Do not paste it into the chat."
    & $ssh -p "$Port" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 -o NumberOfPasswordPrompts=1 -o PreferredAuthentications=password,keyboard-interactive -o PubkeyAuthentication=no "$target" $remote
    $installCode = $LASTEXITCODE
    if ($installCode -eq 255) { Fail "PASSWORD_REJECTED The server rejected the password or the connection dropped; the public key was not written. Do not paste the password into the chat. Confirm it and retry once." 1 }
    if ($installCode -ne 0) { Fail ("PUBKEY_INSTALL_FAILED Could not write the public key remotely, exit code " + $installCode + ". The password was accepted; do not re-enter it.") 1 }

    $probe = Invoke-Probe $ssh $batch
    $authed = ($probe.Code -eq 0) -and ($probe.Text.Contains("win-linux-deploy-ok"))
    if (-not $authed) { Fail ("Passwordless login still failing. If this key has a passphrase, rerun with -Identity pointing at a passphrase-free key.`n" + $probe.Text) 1 }
}

if (-not $hasWork) {
    Write-Output "PASSWORDLESS_OK"
    Write-Output ("SSH_COMMAND=ssh -i '" + $keyPath + "' -o IdentitiesOnly=yes -o BatchMode=yes -p $Port " + $target)
    exit 0
}

$qdir = "'" + $RemoteDir.Replace("'", "'\''") + "'"
& $ssh @batch ("mkdir -p -- " + $qdir)
if ($LASTEXITCODE -ne 0) { Fail "Could not create remote directory: $RemoteDir" 1 }
if ($uploadItems.Count -gt 0) {
    if (-not $scp) { Fail "scp not found." 1 }
    $scpArgs = @(
        "-i", $keyPath,
        "-o", "IdentitiesOnly=yes",
        "-o", "BatchMode=yes",
        "-o", "GSSAPIAuthentication=no",
        "-o", "StrictHostKeyChecking=accept-new",
        "-P", "$Port"
    ) + $uploadItems + @($target + ":" + $RemoteDir)
    & $scp @scpArgs
    if ($LASTEXITCODE -ne 0) { Fail "scp failed: $LASTEXITCODE" 1 }
}

if ($Command) {
    & $ssh @batch ("cd -- " + $qdir + " && " + $Command)
    exit $LASTEXITCODE
}

Write-Output "UPLOAD_OK"
exit 0
