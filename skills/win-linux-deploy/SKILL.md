---
name: win-linux-deploy
description: "Run commands and move files on a Linux host over SSH from Windows: check passwordless login first, configure it with a human-typed password when missing, then run any remote command in a directory. Use for SSH key setup, connecting to Linux, running commands remotely, inspecting a host, deploying, and starting or stopping services."
---

# Remote command runner

Always start by checking whether passwordless login already works. Only when it does not, configure it. Only after it works, connect and run commands. Authentication and remote execution go through `scripts/remote.ps1` in this directory.

This script is a transport, not a task. It authenticates once and then runs whatever remote bash you pass, so the same flow covers inspecting a host, deploying files, starting or stopping a service, tailing a log, or anything else. Nothing in the script knows about a particular host, port, account, or job — every value comes from the call. Never hardcode an IP, a port, a user name, or a machine-specific path into the script, into this document, or into a call; read them from the human or from the task.

The human types the password only at the `password:` prompt in their own terminal. Never ask for it, never let them paste it into the chat. When auth is needed, hand over the `HUMAN_COMMAND` the script prints. Do not write your own ssh with a password, plink, or sshpass.

## Gather first

Ask if missing, do not guess:

- `-User`: login name
- `-Server`: hostname or IP. Do not use `-Host` (it is read-only in PowerShell)
- `-Port`: default 22; pass the real port when the server uses a non-standard one
- `-Identity`: optional key path. Default `~/.ssh/id_ed25519`, then `id_rsa`, else it generates an ed25519 key with no passphrase. The key must have no passphrase
- To run or upload: `-RemoteDir` is an absolute Linux path, default `/tmp`; `-Command` is a remote bash snippet, not PowerShell
- To copy files: `-Upload` local path, repeatable

Done: facts gathered, no password collected.

## 1. Check passwordless login

Always run this first, even when the human believes it is already set up. No `-RemoteDir`, no `-Command`, no `-Upload`:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File '<skill-dir>\scripts\remote.ps1' -User '<user>' -Server '<host>' -Port 22
```

This only probes with `BatchMode`; it never prompts and changes nothing on the server.

- `0` with `PASSWORDLESS_OK`: already passwordless. If the human wants to log in themselves, hand over the command after `SSH_COMMAND=`. Go to step 3.
- `2` with `NEED_HUMAN_PASSWORD`: not passwordless yet. Go to step 2.
- `3` with `HOST_KEY_CHANGED`: stop. Hand the output to the human. Do not edit `known_hosts`, do not keep connecting.
- `1`: hand the output to the human and stop.

Done: you know which of the two flows you are in (`0` or `2`), or you stopped.

## 2. Configure passwordless login

Only after step 1 returned `2`, never on a hunch that it is missing.

The step 1 run already opened a terminal for the human. Tell them: check that the `port` line matches the target (the password prompt itself does not show a port); type the server password at `password:`; answer `yes` if asked about the host fingerprint; never paste the password into the chat. If no window appeared, have them run the `HUMAN_COMMAND=` from the output. Then stop and wait for their reply.

After they confirm, rerun the step 1 command unchanged. If it returns `2` again, stop; do not ask for a password a third time.

Done: the rerun prints `PASSWORDLESS_OK`.

## 3. Connect and run in a directory

Only after `PASSWORDLESS_OK`. Wrap values in PowerShell single quotes. Chain remote commands with `&&` inside one `-Command`.

Read [Command quoting failures](#command-quoting-failures) before running anything. The form below assumes a real host shell. Where the host layer does not interpret quotes, `-Command '...'` arrives split at spaces; the section gives the replacement call shape.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File '<skill-dir>\scripts\remote.ps1' -User '<user>' -Server '<host>' -Port 22 -Command '<remote-bash>'
```

Leave `-RemoteDir` out to use `/tmp`. Add `-RemoteDir '<abs-path>'` only when the work must live in a specific directory.

Add `-Upload '<local>'` to copy files first. Do not open a separate ssh without the `cd`.

`-RemoteDir` is created with `mkdir -p` before anything runs, so an existing path is not required. With `-Upload` the script copies the files after creating the directory, then runs `-Command`; if the copy fails the command does not run.

Never hardcode a machine-specific path in a call: `<skill-dir>` is the directory holding this `SKILL.md`, and `<user>`, `<host>`, `<port>` come from the human. The script itself resolves its key and paths from `$env:USERPROFILE` and `$PSCommandPath`, so it works unchanged for any Windows user.

Done: the remote command's exit code is returned. If the `cd` fails, the command does not run and the exit code is non-zero.

## Any task, same flow

The steps above are the whole protocol. It does not change with the job: authenticating once, then sending remote bash, is the same for a read-only check, a file deploy, a service start or stop, a restart, a log read, or a cleanup.

Shape every request the same way:

- Compose the remote bash for what the human asked. Read-only inspection and state-changing operations are the same call; the difference is only in the snippet.
- Put anything destructive behind an explicit human request. Do not invent a stop, delete, or restart.
- Keep one concern per call so a failure is unambiguous, and report the exit code that comes back.
- For a service, the snippet is the task, for example start, stop, or status on a named unit; the script is never extended per-task and never carries a task of its own.

Do not add a task-specific parameter, branch, or default to `remote.ps1` for one job. If a call needs a value, it comes from the human.

## Command quoting failures

Symptoms seen in practice, and what each one really means. Check this list before retrying anything.

| Symptom | Cause | Fix |
|---|---|---|
| `Cannot convert value "<port>;" to type "System.Int32"` after `-Port` | The host layer passed the port together with the separator; it is not a shell | Drop `;`, `&&`, `\|` from the call; run one command per call, or `cmd /c` |
| `The -File parameter ... does not support a path format` with a path that `Test-Path` says exists | Single quotes were passed through literally, so the path began with `'` | Do not quote arguments, or launch through `cmd /c` |
| `A positional parameter cannot be found` / `NamedParameterNotFound` naming part of the command, e.g. `h;` | The snippet `free -h; echo ...` was split at spaces; only the first word bound to `-Command`, the rest became stray parameters | This is the normal case for any snippet with spaces. There is no quoting trick: see the payload rule below |
| Script parses as garbage, parameters shift | The `.ps1` was written with a UTF-8 BOM; PowerShell 5.1 folds it into the first token | Save without BOM: `[IO.File]::WriteAllBytes($p, [IO.File]::ReadAllBytes($p) -replace '^\xEF\xBB\xBF', '')` or strip `b'\xef\xbb\xbf'` in Python |
| Probe returns `2` immediately with no prompt | No TTY: `[Console]::IsInputRedirected` is true, so the script opens a new window and exits `2`. Nothing is broken | Do not retry. Tell the human to use the window or the printed `HUMAN_COMMAND=` |

Facts about the host layer that cause most of the above:

- `echo one; echo two` prints `one; echo two` as one string. `;`, `&&`, `|`, heredocs and `$vars` are **not** interpreted.
- Double and single quotes reach PowerShell verbatim, so `'<path>'` breaks `-File` and `"<value>"` becomes part of the value.
- `write_file`-created scripts carry a UTF-8 BOM that PowerShell 5.1 mis-parses.

Rules that make the call work:

1. One command per call. No `;` or `&&` in the host command line.
2. Never quote arguments; use plain absolute paths with drive letter and backslashes.
3. Launch through the native shell when the line needs separators: `cmd /c "powershell -NoProfile -ExecutionPolicy Bypass -File <skill-dir>\scripts\remote.ps1 -User <user> -Server <host> -Port <port>"`. Confirm `cmd.exe` exists first (`where.exe cmd`); otherwise assume no shell and use rule 1. Use the real absolute path to `remote.ps1`, never a placeholder and never a path belonging to another machine.
4. Payload rule: a remote bash snippet with spaces **cannot** travel on the command line, whatever the quoting. Wrap it in a helper `.ps1` that assigns it to a variable and calls `& $script -Command $remote` — a PowerShell variable binds as one argument. Keep that helper in a temp file, ASCII with no BOM, and delete it afterwards.
5. Read the exit code. `4294770688` is a PowerShell usage/parse failure, not a remote result; never report it as a server state.

Do not "fix" a quoting failure by rewriting the ssh call by hand, adding a password, or editing `known_hosts`. Fix the call shape, then rerun.
