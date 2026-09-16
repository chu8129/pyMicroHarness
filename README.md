# Harness

A streamlined, lightweight harness for orchestrating tasks and managing configurations.

## Prerequisites

- Python 3.x
- Virtual environment (recommended)

## Installation & Setup

### Environment Requirements
- Python 3.14+
- A Unix-based terminal (Linux or macOS)

### 1. Create a Virtual Environment
It is recommended to use a virtual environment to manage dependencies:

```bash
# Create virtual environment
python3 -m venv venv

# Activate it
source venv/bin/activate
```

### 2. Install Dependencies
Install the required packages using pip:

```bash
pip install -r requirements.txt
```

### 3. Environment Variables
Configure your credentials by creating a `.env` file in the project root:

```bash
export BEDROCK_API_KEY="your_key_here"
export KIMI_API_KEY="your_key_here"
```

Load the configuration before execution:
```bash
source .env
```

### 4. Verification
Verify the installation by ensuring the script can initialize:

```bash
python . --help
```

## Configuration

The system loads `config.yaml` using the following order of precedence:

1. **Command-line arguments**
2. `./config.yaml` (Current directory)
3. `~/.reasonix/config.yaml` (Global configuration)

Modify the `providers` list in `config.yaml` to manage model interfaces.

## Usage

Start the service by running:

```bash
python .
```

## Feishu / Lark Gateway

[feishu app link](https://open.feishu.cn/app)

The harness can serve the same agent as a Feishu (Lark) bot. Events arrive over a
WebSocket long connection, so no public IP, webhook, or port forwarding is needed.

### 1. Create the bot

On the [Feishu Open Platform](https://open.feishu.cn/app) (or [Lark](https://open.larksuite.com/app)):

1. Create an app and copy its **App ID** and **App Secret**.
2. Enable the **Bot** capability.
3. Under *Events and callbacks*, choose **long connection** mode and subscribe to
   `im.message.receive_v1` (required).
4. Grant the bot permissions: `im:message`, `im:message:send_as_bot`
   (and `im:chat:readonly` if you use group chats).
5. Publish a version so the app is available to your tenant.

### 2. Configure

```bash
export FEISHU_APP_ID="cli_xxx"
export FEISHU_APP_SECRET="xxx"
```

…or fill in the `feishu:` block in `config.yaml` (`app_id`, `app_secret`,
`allow_from`, `group_policy`, `reply_to_message`, …).

Install the SDK and start the gateway — it reads `config.yaml` from the current
directory and needs no command-line options:

```bash
pip install lark-oapi
python . gateway        # or run `python .` and type /gateway
```

### Behaviour

- Each chat gets its own isolated, persisted session (resume with `--resume`).
- Duplicate events are dropped; messages from senders outside `allow_from` are ignored.
- Group chats only trigger the bot when it is `@mentioned` unless `group_policy: all`.
- Long answers are split into several messages; by default the reply quotes the
  user's message (`reply_to_message`).
- When the model needs a decision (`ask` tool) the question and its numbered options are
  posted into the chat and the turn waits (default 300s, `feishu.ask_timeout_seconds`) for a
  reply. Answer with `1`, `2`, … or the option text; any other text is passed through as a
  free-form answer. If nobody answers in time the agent states its assumption and continues.

## Global Command Setup

To invoke the tool from any directory, choose one of the following methods:

### Option 1: Shell Alias (Recommended)
Add an alias to your shell profile (e.g., `~/.zshrc` or `~/.bashrc`):

```bash
alias harness='cd /path/to/your/harness && python3 .'
```

### Option 2: Global Executable Script
1. Create a `harness` script in the root directory:
   ```bash
   #!/bin/bash
   # Replace with your actual virtual environment path
   /path/to/your/venv/bin/python /path/to/your/harness/__main__.py "$@"
   ```
2. Make it executable and move it to your system path:
   ```bash
   chmod +x harness
   sudo mv harness /usr/local/bin/
   ```
After setup, you can simply run `harness` from any terminal.
