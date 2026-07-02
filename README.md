### 极简版本harness方便理解-qw

## 配置
请根据实际情况修改 `config.yaml` 文件。

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
│   'edit_file'                       │
│   'shell_edit_file'                 │
│   'multi_edit'                      │
│   'bash'                            │
│   'grep'                            │
│   'glob'                            │
│   'ls'                              │
│   'web_fetch'                       │
│   'ask'                             │
│   'todo_write'                      │
│   'web_search']                     │
│ Skills: ['explore'                  │
│   'research'                        │
│   'review'                          │
│   'test'                            │
│   'doc'                             │
│   'refactor'                        │
│   'memory'                          │
│   'domain-modeling'                 │
│   'setup-matt-pocock-skills'        │
│   'memory'                          │
│   'writing-great-skills'            │
│   'handoff'                         │
│   'diagnosing-bugs'                 │
│   'ask-matt'                        │
│   'sn-md-to-html-report'            │
│   'improve-codebase-architecture'   │
│   'meeting-minutes'                 │
│   'codebase-design'                 │
│   'triage'                          │
│   'prototype'                       │
│   'to-issues'                       │
│   'writing-fragments'               │
│   'resolving-merge-conflicts'       │
│   'writing-shape'                   │
│   'review'                          │
│   'grill-with-docs'                 │
│   'implement'                       │
│   'teach'                           │
│   'decision-mapping'                │
│   'ppt-master'                      │
│   'tdd'                             │
│   'grill-me'                        │
│   'to-prd'                          │
│   'grilling'                        │
│   'writing-beats']                  │
│ Provider: gemini2                   │
│ Model: gemini/gemini-3.1-flash-lite │
│ Base URL:                           │
╰─────────────────────────────────────╯
╭───────────────────────────────────── ℹ️ HELP ──────────────────────────────────────╮
│                                                                                    │
│ SYNOPSIS                                                                           │
│   Harness Kernel                                                                   │
│                                                                                    │
│ COMMANDS                                                                           │
│   /new              Start a new conversation (automatically saves current session) │
│   /clear            Same as /new, clears conversation context                      │
│   /model            List all available providers                                   │
│   /model <name/idx> Switch to the specified provider                               │
│   /plan             Enable plan mode (next request is planned before execution)    │
│   /plan on/off    Enable or disable plan mode                                      │
│   /plan status      Check current plan mode status                                 │
│   /context          Display current context and LLM request payload                │
│   /exit, /quit      Exit (automatically saves session)                             │
│   q                 Exit (same as /quit)                                           │
│                                                                                    │
│ INTERRUPTS                                                                         │
│   Ctrl-C            Cancel the current operation                                   │
│   Ctrl-D            Exit the interactive session                                   │
│                                                                                    │
╰────────────────────────────────────────────────────────────────────────────────────╯
▶
```
