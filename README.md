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
