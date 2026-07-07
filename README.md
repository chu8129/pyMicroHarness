### 极简版本harness方便理解-qw

## 配置说明

### 1. 环境变量 (.env)
系统支持通过环境变量管理密钥和敏感信息。建议在根目录下创建 `.env` 文件：
```bash
# 例如
export BEDROCK_API_KEY="your_key_here"
export KIMI_API_KEY="your_key_here"
```
启动前请执行 `source .env` 以加载配置。

### 2. 全局配置 (config.yaml)
系统根据以下优先级加载 `config.yaml`：
1. 启动命令行参数
2. `./config.yaml` (当前目录)
3. `~/.reasonix/config.yaml` (全局配置)

修改 `config.yaml` 中的 `providers` 列表来切换或添加模型接口配置。

## 全局命令配置

若要实现全局命令调用（例如在任意目录运行 `harness`），有两种推荐方式：

### 方案一：Shell Alias (推荐)
在 shell 配置文件（如 `~/.zshrc` 或 `~/.bashrc`）中添加 alias：
```bash
# 将路径替换为实际项目根目录路径
alias harness='cd /path/to/your/harness && python3 .'
```

### 方案二：全局可执行脚本
为了在任意路径下调用，可以将项目封装为系统命令：
1. 在项目根目录创建一个名为 `harness` 的文件，写入以下内容（**请将路径替换为实际路径**）：
   ```bash
   #!/bin/bash
   # 使用您的虚拟环境 Python 路径
   /path/to/your/venv/bin/python /path/to/your/harness/__main__.py "$@"
   ```
2. 设置权限并移动至系统路径：
   ```bash
   chmod +x harness
   sudo mv harness /usr/local/bin/
   ```
完成后，在终端输入 `harness` 即可直接启动程序。

## 启动服务
确保已安装所需的依赖，使用以下命令启动服务：

```bash
python .
```

## screen log
```
╭───────── 🚀 SYSTEM STARTUP ─────────╮
│ Workspace: /Users/mvgz0022/pyreson  │
│ Tools: ['read_file'                 │
│   'shell_read_file'                 │
│   'write_file'                      │
│   'shell_write_file'                │
```
