#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Reasonix Kernel - Python Micro-code Representation

Changes from v1:
  - All config loaded from YAML (Pydantic models)
  - Skills are configurable via YAML, not hardcoded
  - Significant code simplification
"""

from __future__ import annotations
import os
import sys
import json


def _load_dotenv(env_file: str = ".env") -> None:
    """从 .env 文件补充环境变量（不覆盖已有的系统环境变量）。"""
    try:
        from dotenv import load_dotenv

        load_dotenv(env_file, override=False)
    except ImportError:
        pass  # python-dotenv 未安装时静默跳过


import glob
import subprocess
import readline
from abc import ABC, abstractmethod
from typing import Any, Optional, List, Dict
from typing import List as TypingList  # noqa: F401
from enum import Enum
from pathlib import Path
import logging

logger = logging

# =============================================================================
# Dependencies
# =============================================================================
try:
    import yaml
except ImportError:
    raise ImportError("PyYAML is required. Install: pip install pyyaml")

try:
    from pydantic import BaseModel, Field
except ImportError:
    raise ImportError("Pydantic is required. Install: pip install pydantic")


# =============================================================================
# 1. Pydantic Configuration Models (loaded from YAML)
# =============================================================================


class AgentConfig(BaseModel):
    system_prompt: str = ""
    language_policy: str = ""
    max_steps: int = 0
    planner_max_steps: int = 12
    temperature: float = 0.0
    auto_plan: bool = False
    reasoning_language: str = "auto"
    planner_model: str = ""
    subagent_model: str = ""
    subagent_models: Dict[str, str] = Field(default_factory=dict)
    output_style: str = ""
    soft_compact_ratio: float = 0.5
    compact_ratio: float = 0.8
    compact_force_ratio: float = 0.9


class ShellConfig(BaseModel):
    prefer: str = "auto"
    path: str = ""


class ToolsConfig(BaseModel):
    enabled: List[str] = Field(default_factory=list)
    shell: ShellConfig = Field(default_factory=ShellConfig)
    bash_timeout_seconds: int = 120


class SkillsConfig(BaseModel):
    enabled: List[str] = Field(default_factory=list)


class PermissionsConfig(BaseModel):
    allow_write: bool = True
    allow_bash: bool = True
    allow_web_fetch: bool = True
    auto_approve: bool = False


class SandboxConfig(BaseModel):
    enabled: bool = False
    allowed_paths: List[str] = Field(default_factory=list)
    blocked_paths: List[str] = Field(default_factory=list)


class ProviderEntry(BaseModel):
    name: str = ""
    kind: str = "openai"
    base_url: str = ""
    model: str = ""
    models: List[str] = Field(default_factory=list)
    default: bool = False
    api_key_env: str = ""
    context_window: int = 0
    request_timeout: int = 120
    price: Dict[str, float] = Field(default_factory=dict)
    effort: str = ""
    thinking: str = ""


class SkillEntry(BaseModel):
    """Skill definition from YAML — replaces hardcoded BUILTIN_SKILLS."""

    name: str
    description: str = ""
    body: str = ""
    allowed_tools: List[str] = Field(default_factory=list)
    run_as: str = "subagent"  # "subagent" | "inline"


class Config(BaseModel):
    """Root configuration model — loaded from YAML."""

    default_model: str = ""
    providers: List[ProviderEntry] = Field(default_factory=list)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    skills: SkillsConfig = Field(default_factory=SkillsConfig)
    permissions: PermissionsConfig = Field(default_factory=PermissionsConfig)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    # skills_data is loaded separately from skills.yaml, not from main config

    @classmethod
    def load_for_root(cls, workspace_root: str) -> Config:
        """Load config from YAML with resolution: project > user > defaults."""
        # 1. Start with defaults
        cfg = Config()

        # 2. Merge user config (~/.reasonix/config.yaml)
        user_config = Path.home() / ".reasonix" / "config.yaml"
        if user_config.exists():
            cfg = cfg._merge_yaml(user_config)

        # 3. Merge project config (./config.yaml)
        project_config = Path(workspace_root) / "config.yaml"
        if project_config.exists():
            cfg = cfg._merge_yaml(project_config)

        # 4. Load skills from separate skills.yaml
        skills_yaml = Path(workspace_root) / "skills.yaml"
        if skills_yaml.exists():
            with open(skills_yaml, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
                cfg._skills_data = [SkillEntry.model_validate(s) for s in data.get("skills", [])]
        else:
            cfg._skills_data = []

        return cfg

    def _merge_yaml(self, path: Path) -> Config:
        """Merge YAML file into current config recursively."""
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        current = self.model_dump()
        merged = _deep_merge(current, data)
        return Config.model_validate(merged)

    @staticmethod
    def _resolve_text_or_file(value: str, root: str) -> str:
        """If value starts with 'file:', read the referenced file (relative to root).

        Supported formats:
          system_prompt: "file:prompt.md"          # relative to workspace root
          system_prompt: "file:./prompts/sys.md"   # same, explicit ./
          system_prompt: "file:/abs/path/sys.md"   # absolute path
        If the prefix is absent the value is returned as-is.
        """
        stripped = value.strip()
        if not stripped.lower().startswith("file:"):
            return value
        file_path_str = stripped[5:].strip()  # strip "file:" prefix
        file_path = Path(file_path_str)
        if not file_path.is_absolute():
            file_path = Path(root) / file_path
        try:
            return file_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise FileNotFoundError(f"system_prompt file not found: {file_path}\n" f"  (referenced as '{file_path_str}' relative to '{root}')")

    def resolve_system_prompt(self, root: str) -> str:
        """Build system prompt with customizations.

        system_prompt and language_policy both support 'file:' references, e.g.:
          system_prompt: "file:my_prompt.md"
        """
        base = self._resolve_text_or_file(self.agent.system_prompt, root)
        if self.agent.language_policy:
            base += "\n\n" + self._resolve_text_or_file(self.agent.language_policy, root)
        if self.agent.output_style:
            base += f"\n\nOutput style: {self.agent.output_style}"
        memory_path = Path(root) / "REASONIX.md"
        if memory_path.exists():
            base += f"\n\n# Project Memory\n{memory_path.read_text(encoding='utf-8')}"
        return base

    @property
    def skills_data(self) -> List[SkillEntry]:
        return getattr(self, "_skills_data", [])

    def get_skill(self, name: str) -> Optional[SkillEntry]:
        for s in self.skills_data:
            if s.name == name:
                return s
        return None

    def enabled_skills(self) -> List[SkillEntry]:
        if not self.skills.enabled:
            return self.skills_data
        return [s for s in self.skills_data if s.name in self.skills.enabled]


# =============================================================================
# Logging helpers
# =============================================================================

_LOG_STYLES = {
    "user": ("📝 USER", "│"),
    "model": ("🤖 MODEL", "│"),
    "tool": ("🔧 TOOL", "│"),
    "mcp": ("🌐 MCP", "│"),
    "boot": ("⚙️  BOOT", "│"),
}


def log_box(category: str, text: str, max_width: int = 90) -> None:
    """Print text in a left-bordered box with category label."""
    style = _LOG_STYLES.get(category, (category.upper(), "│"))
    label, bar = style
    lines = text.splitlines() or [""]
    # truncate long lines
    lines = [l[:max_width] for l in lines]
    width = max(len(label) + 2, max(len(l) for l in lines) + 2, 40)
    top = f"┌─ {label} {'─' * (width - len(label) - 3)}"
    bot = f"└{'─' * (width)}"
    print(top)
    for l in lines:
        print(f"{bar} {l}")
    print(bot)


# =============================================================================
# 2. Built-in Tools (simplified, self-registering)
# =============================================================================


def should_enable(tool_name: str, enabled_list: List[str]) -> bool:
    return not enabled_list or tool_name in enabled_list


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base."""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        elif key in result and isinstance(result[key], list) and isinstance(value, list):
            # For lists, override replaces (standard YAML merge behavior)
            result[key] = value
        else:
            result[key] = value
    return result


class Tool(ABC):
    @abstractmethod
    def name(self) -> str: ...
    @abstractmethod
    def description(self) -> str: ...
    @abstractmethod
    def schema(self) -> dict: ...
    @abstractmethod
    def execute(self, ctx: Any, args: dict) -> str: ...
    @abstractmethod
    def read_only(self) -> bool: ...

    def to_dict(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": self.schema(),
            },
        }


class ReadFileTool(Tool):
    def name(self):
        return "read_file"

    def description(self):
        return "Read a file's contents."

    def schema(self):
        return {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}

    def read_only(self):
        return True

    def execute(self, ctx, args):
        try:
            return Path(args["path"]).read_text(encoding="utf-8")
        except Exception as e:
            return f"Error: {e}"


class WriteFileTool(Tool):
    def name(self):
        return "write_file"

    def description(self):
        return "Write content to a file. Overwrites if exists."

    def schema(self):
        return {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}

    def read_only(self):
        return False

    def execute(self, ctx, args):
        path = Path(args["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(args["content"], encoding="utf-8")
        return f"Wrote {path} ({len(args['content'])} chars)"


class EditFileTool(Tool):
    def name(self):
        return "edit_file"

    def description(self):
        return "Edit a file with search/replace."

    def schema(self):
        return {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}

    def read_only(self):
        return False

    def execute(self, ctx, args):
        path = Path(args["path"])
        content = path.read_text(encoding="utf-8")
        old = args["old_text"]
        if old not in content:
            return f"Error: old_text not found in {path}"
        path.write_text(content.replace(old, args["new_text"], 1), encoding="utf-8")
        return f"Edited {path}"


class MultiEditTool(Tool):
    def name(self):
        return "multi_edit"

    def description(self):
        return "Apply multiple edits to a file atomically."

    def schema(self):
        return {"type": "object", "properties": {"path": {"type": "string"}, "edits": {"type": "array", "items": {"type": "object", "properties": {"old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["old_text", "new_text"]}}}, "required": ["path", "edits"]}

    def read_only(self):
        return False

    def execute(self, ctx, args):
        path = Path(args["path"])
        content = path.read_text(encoding="utf-8")
        for edit in args["edits"]:
            old = edit["old_text"]
            if old not in content:
                return f"Error: old_text not found: {old[:50]}..."
            content = content.replace(old, edit["new_text"], 1)
        path.write_text(content, encoding="utf-8")
        return f"Applied {len(args['edits'])} edits to {path}"


class BashTool(Tool):
    def __init__(self, prefer="auto", path="", timeout=120):
        self.prefer, self.path, self.timeout = prefer, path, timeout

    def name(self):
        return "bash"

    def description(self):
        return "Execute a shell command. Use for builds, tests, git, package managers."

    def schema(self):
        return {"type": "object", "properties": {"command": {"type": "string"}, "run_in_background": {"type": "boolean"}}, "required": ["command"]}

    def read_only(self):
        return False

    def execute(self, ctx, args):
        try:
            result = subprocess.run(args["command"], shell=True, capture_output=True, text=True, timeout=self.timeout, executable=self.path or None)
            out = result.stdout
            if result.returncode != 0:
                out += f"\n[exit {result.returncode}]\n{result.stderr}"
            return out or "(no output)"
        except subprocess.TimeoutExpired:
            return f"Error: timed out after {self.timeout}s"
        except Exception as e:
            return f"Error: {e}"


class GrepTool(Tool):
    def __init__(self, rg_path=""):
        self.rg_path = rg_path

    def name(self):
        return "grep"

    def description(self):
        return "Search for a regex pattern in files."

    def schema(self):
        return {"type": "object", "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}}, "required": ["pattern", "path"]}

    def read_only(self):
        return True

    def execute(self, ctx, args):
        import re

        pattern, path = args["pattern"], Path(args["path"])
        try:
            if self.rg_path and Path(self.rg_path).exists():
                r = subprocess.run([self.rg_path, "--no-heading", "-n", "--with-filename", pattern, str(path)], capture_output=True, text=True)
                return r.stdout or "(no matches)"
            rx = re.compile(pattern)
            files = [path] if path.is_file() else [p for p in path.rglob("*") if p.is_file()]
            matches = []
            for fp in files:
                try:
                    for i, line in enumerate(fp.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                        if rx.search(line):
                            matches.append(f"{fp}:{i}:{line}")
                except Exception:
                    continue
            return "\n".join(matches) if matches else "(no matches)"
        except Exception as e:
            return f"Error: {e}"


class GlobTool(Tool):
    def name(self):
        return "glob"

    def description(self):
        return "Find files matching a glob pattern."

    def schema(self):
        return {"type": "object", "properties": {"pattern": {"type": "string"}}, "required": ["pattern"]}

    def read_only(self):
        return True

    def execute(self, ctx, args):
        return "\n".join(glob.glob(args["pattern"], recursive=True)) or "(no matches)"


class LsTool(Tool):
    def name(self):
        return "ls"

    def description(self):
        return "List directory contents."

    def schema(self):
        return {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}

    def read_only(self):
        return True

    def execute(self, ctx, args):
        p = Path(args["path"])
        return "\n".join(f"{'d' if e.is_dir() else 'f'} {e.name}" for e in sorted(p.iterdir())) if p.exists() else f"Error: not found {p}"


class WebFetchTool(Tool):
    def __init__(self, proxy=None):
        self.proxy = proxy

    def name(self):
        return "web_fetch"

    def description(self):
        return "Fetch content from a URL."

    def schema(self):
        return {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}

    def read_only(self):
        return True

    def execute(self, ctx, args):
        try:
            import urllib.request

            req = urllib.request.Request(args["url"], headers={"User-Agent": "Reasonix/1.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except Exception as e:
            return f"Error: {e}"


class AskTool(Tool):
    def name(self):
        return "ask"

    def description(self):
        return "Ask the user for clarification when a consequential choice is required."

    def schema(self):
        return {"type": "object", "properties": {"question": {"type": "string"}, "options": {"type": "array", "items": {"type": "string"}}}, "required": ["question"]}

    def read_only(self):
        return True

    def execute(self, ctx, args):
        print(f"\n[ASK] {args['question']}")
        for i, opt in enumerate(args.get("options", []), 1):
            print(f"  {i}. {opt}")
        if not sys.stdin.isatty():
            return "<model-assumption> Proceeding with default."
        try:
            return input("Your choice: ")
        except EOFError:
            return "<model-assumption> Proceeding with default."


class TodoWriteTool(Tool):
    def name(self):
        return "todo_write"

    def description(self):
        return "Track multi-step task progress."

    def schema(self):
        return {"type": "object", "properties": {"todos": {"type": "array", "items": {"type": "object", "properties": {"id": {"type": "string"}, "content": {"type": "string"}, "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}}, "required": ["id", "content", "status"]}}}, "required": ["todos"]}

    def read_only(self):
        return False

    def execute(self, ctx, args):
        if hasattr(ctx, "todos"):
            ctx.todos = args["todos"]
        return f"Updated {len(args['todos'])} todos"


# =============================================================================
# 3. Tool Registry
# =============================================================================


class Registry:
    def __init__(self):
        self._tools: Dict[str, Tool] = {}

    def add(self, tool: Tool) -> None:
        self._tools[tool.name()] = tool

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def list(self) -> List[Tool]:
        return list(self._tools.values())

    def schemas(self) -> List[dict]:
        return [t.to_dict() for t in self._tools.values()]


ALL_TOOLS = [ReadFileTool, WriteFileTool, EditFileTool, MultiEditTool, BashTool, GrepTool, GlobTool, LsTool, WebFetchTool, AskTool, TodoWriteTool]


def register_all_builtins(reg: Registry, cfg: Config, root: str, proxy=None) -> None:
    enabled = cfg.tools.enabled
    for cls in ALL_TOOLS:
        name = cls().name()
        if not should_enable(name, enabled):
            continue
        if cls is BashTool:
            reg.add(cls(prefer=cfg.tools.shell.prefer, path=cfg.tools.shell.path, timeout=cfg.tools.bash_timeout_seconds))
        elif cls is GrepTool:
            reg.add(cls(rg_path=""))
        elif cls is WebFetchTool:
            reg.add(cls(proxy=proxy))
        else:
            reg.add(cls())


# =============================================================================
# 4. Context / Message History
# =============================================================================


class MessageRole(Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class Message:
    def __init__(self, role: MessageRole, content: str, name: str = None, tool_call_id: str = None, tool_calls: list = None):
        self.role, self.content, self.name, self.tool_call_id, self.tool_calls = role, content, name, tool_call_id, tool_calls

    def to_dict(self) -> dict:
        return {
            "role": self.role.value,
            "content": self.content,
            "name": self.name,
            "tool_call_id": self.tool_call_id,
            "tool_calls": self.tool_calls,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Message":
        return cls(
            role=MessageRole(d["role"]),
            content=d["content"],
            name=d.get("name"),
            tool_call_id=d.get("tool_call_id"),
            tool_calls=d.get("tool_calls"),
        )


class Context:
    def __init__(self, system_prompt: str, cfg: AgentConfig):
        self.system_prompt = system_prompt
        self.cfg = cfg
        self.messages: List[Message] = []
        self.todos: List[dict] = []

    def add_user(self, content: str) -> None:
        self.messages.append(Message(MessageRole.USER, content))

    def add_assistant(self, content: str, tool_calls: list = None) -> None:
        self.messages.append(Message(MessageRole.ASSISTANT, content, tool_calls=tool_calls))

    def add_tool_result(self, name: str, tid: str, result: str) -> None:
        self.messages.append(Message(MessageRole.TOOL, result, name=name, tool_call_id=tid))

    def estimate_tokens(self) -> int:
        total = len(self.system_prompt) + sum(len(m.content) for m in self.messages)
        return total // 4

    def compact(self, force: bool = False) -> None:
        ratio = self.cfg.compact_force_ratio if force else self.cfg.compact_ratio
        max_tokens = 128000 * ratio
        if self.estimate_tokens() < max_tokens:
            return
        # Simple compaction: summarize oldest messages
        to_compress = []
        keep = []
        for m in self.messages:
            if m.role == MessageRole.SYSTEM:
                continue
            if len(to_compress) < len(self.messages) // 2:
                to_compress.append(m)
            else:
                keep.append(m)
        if to_compress:
            summary = f"[Summary of {len(to_compress)} messages]"
            self.messages = [Message(MessageRole.SYSTEM, self.system_prompt), Message(MessageRole.ASSISTANT, summary)] + keep

    def to_openai(self) -> List[dict]:
        out = [{"role": "system", "content": self.system_prompt}]
        for m in self.messages:
            entry = {"role": m.role.value, "content": m.content}
            if m.name:
                entry["name"] = m.name
            if m.tool_call_id:
                entry["tool_call_id"] = m.tool_call_id
            if m.tool_calls:
                entry["tool_calls"] = m.tool_calls
            out.append(entry)
        return out

    def to_dict(self) -> dict:
        return {
            "system_prompt": self.system_prompt,
            "messages": [m.to_dict() for m in self.messages],
            "todos": self.todos,
        }

    @classmethod
    def from_dict(cls, d: dict, cfg: AgentConfig) -> "Context":
        ctx = cls(system_prompt=d["system_prompt"], cfg=cfg)
        ctx.messages = [Message.from_dict(m) for m in d.get("messages", [])]
        ctx.todos = d.get("todos", [])
        return ctx


# =============================================================================
# 5. Provider / LLM Interface
# =============================================================================


class Provider:
    def __init__(self, entry: ProviderEntry):
        self.entry = entry
        self.api_key = os.environ.get(entry.api_key_env, "") or entry.api_key_env

    def chat(self, messages: List[dict], tools: List[dict], temperature: float = 0.0) -> dict:
        """发送请求到 LLM。在子线程中执行阻塞的 HTTP 调用，主线程轮询 future
        以便 Ctrl+C（KeyboardInterrupt）能即时响应并取消请求。"""
        import urllib.request
        import urllib.error
        import concurrent.futures
        import threading
        import time

        payload = {"model": self.entry.model, "messages": messages, "temperature": temperature}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        data = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"}
        url = f"{self.entry.base_url.rstrip('/')}/chat/completions"

        # 用于通知子线程停止重试的标志
        _cancel = threading.Event()

        def _call_once():
            """单次 HTTP 请求（在子线程中运行）。"""
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=self.entry.request_timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))

        def _call_with_retry():
            """最多重试 3 次，遇到取消信号立即退出。"""
            last_exc: Optional[Exception] = None
            for attempt in range(3):
                if _cancel.is_set():
                    raise InterruptedError("Request cancelled by user.")
                try:
                    return _call_once()
                except (urllib.error.HTTPError, urllib.error.URLError, OSError) as e:
                    last_exc = e
                    if _cancel.is_set():
                        raise InterruptedError("Request cancelled by user.")
                    if attempt < 2:
                        # 分段 sleep，每 0.2 s 检查一次取消标志
                        for _ in range(5):
                            if _cancel.is_set():
                                raise InterruptedError("Request cancelled by user.")
                            time.sleep(0.2)
            raise last_exc  # type: ignore[misc]

        # 在独立线程池中执行，主线程以短超时轮询以响应 Ctrl+C
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = executor.submit(_call_with_retry)
        try:
            while True:
                try:
                    result = future.result(timeout=0.2)
                    break
                except concurrent.futures.TimeoutError:
                    continue  # 继续等待，让信号处理器有机会运行
        except KeyboardInterrupt:
            _cancel.set()  # 通知子线程停止重试
            executor.shutdown(wait=False)
            raise  # 向上传播，由 Controller / CLI 层处理
        finally:
            executor.shutdown(wait=False)

        # 解析正常响应
        try:
            choice = result["choices"][0]
            msg = choice["message"]
            return {"content": msg.get("content", ""), "tool_calls": msg.get("tool_calls", []), "finish_reason": choice.get("finish_reason", "")}
        except (urllib.error.HTTPError,) as e:
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            err_msg = f"Error: HTTP {e.code} {e.reason}"
            if body:
                err_msg += f"\n{body}"
            return {"content": err_msg, "tool_calls": [], "finish_reason": "error"}
        except Exception as e:
            return {"content": f"Error: {e}", "tool_calls": [], "finish_reason": "error"}


# =============================================================================
# 6. Agent Controller
# =============================================================================


class Controller:
    def __init__(self, workspace_root: str = "."):
        self.root = os.path.abspath(workspace_root)
        self.cfg = Config.load_for_root(self.root)
        self.registry = Registry()
        self.provider: Optional[Provider] = None
        self.context: Optional[Context] = None
        self.step_count = 0

    def _sessions_dir(self) -> Path:
        d = Path.home() / ".reasonix" / "sessions"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def save_session(self) -> str:
        import time, hashlib

        ts = time.strftime("%Y%m%d_%H%M%S")
        rand = hashlib.sha256(str(time.time()).encode()).hexdigest()[:6]
        sid = f"{ts}_{rand}"
        data = {
            "session_id": sid,
            "workspace_root": self.root,
            "step_count": self.step_count,
            "context": self.context.to_dict() if self.context else {},
        }
        path = self._sessions_dir() / f"{sid}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return sid

    def load_session(self, sid: str) -> bool:
        path = self._sessions_dir() / f"{sid}.json"
        if not path.exists():
            return False
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.root = data.get("workspace_root", self.root)
        self.step_count = data.get("step_count", 0)
        ctx_data = data.get("context", {})
        if ctx_data:
            self.context = Context.from_dict(ctx_data, self.cfg.agent)
        return True

    def list_providers(self) -> List[str]:
        current_name = self.provider.entry.name if self.provider else None
        lines = []
        for i, p in enumerate(self.cfg.providers):
            active = " ◀ active" if p.name == current_name else ""
            lines.append(f"{i+1}. {p.name} ({p.model}){active}")
        return lines

    def switch_provider(self, name_or_idx: str) -> str:
        target = None
        if name_or_idx.isdigit():
            idx = int(name_or_idx) - 1
            if 0 <= idx < len(self.cfg.providers):
                target = self.cfg.providers[idx]
        else:
            for p in self.cfg.providers:
                if p.name == name_or_idx or p.model == name_or_idx:
                    target = p
                    break
        if not target:
            return f"Provider '{name_or_idx}' not found. Available:\n" + "\n".join(self.list_providers())
        self.provider = Provider(target)
        return f"Switched to provider: {target.name} ({target.model})"

    def reset_context(self) -> None:
        system_prompt = self.cfg.resolve_system_prompt(self.root)
        self.context = Context(system_prompt, self.cfg.agent)
        self.step_count = 0

    def boot(self) -> None:
        register_all_builtins(self.registry, self.cfg, self.root)
        if not self.cfg.providers:
            raise RuntimeError("No providers configured. Add at least one provider to config.yaml.")
        default = next((p for p in self.cfg.providers if p.default), self.cfg.providers[0])
        self.provider = Provider(default)
        system_prompt = self.cfg.resolve_system_prompt(self.root)
        self.context = Context(system_prompt, self.cfg.agent)
        log_box("boot", f"Workspace: {self.root}\nTools: {[t.name() for t in self.registry.list()]}\nSkills: {[s.name for s in self.cfg.enabled_skills()]}\nProvider: {self.provider.entry.name}")

    def run(self, user_request: str) -> str:
        if self.context is None:
            self.boot()
        self.context.add_user(user_request)
        log_box("user", user_request[:500])
        max_steps = self.cfg.agent.max_steps or 25
        for _ in range(max_steps):
            self.step_count += 1
            print(f"\n{'=' * 40} Step {self.step_count} {'=' * 40}")
            self.context.compact(force=False)
            messages = self.context.to_openai()
            tools = self.registry.schemas()
            if not self.provider:
                return "Error: no provider"
            # KeyboardInterrupt 在此处直接向上传播，不捕获
            response = self.provider.chat(messages, tools, self.cfg.agent.temperature)
            content = response.get("content", "")
            tool_calls = response.get("tool_calls", [])
            finish = response.get("finish_reason", "")
            model_parts = []
            if finish:
                model_parts.append(f"[finish={finish}]")
            if content:
                model_parts.append(content[:500])
            if tool_calls:
                tc_names = [tc.get("function", {}).get("name", "?") for tc in tool_calls]
                model_parts.append(f"→ tools: {', '.join(tc_names)}")
            if model_parts:
                log_box("model", "\n".join(model_parts))
            self.context.add_assistant(content or "", tool_calls=tool_calls or None)
            if tool_calls:
                for tc in tool_calls:
                    tid = tc.get("id", "unknown")
                    fn = tc.get("function", {})
                    tname = fn.get("name", "")
                    try:
                        args = json.loads(fn.get("arguments", "{}")) if isinstance(fn.get("arguments"), str) else fn.get("arguments", {})
                    except json.JSONDecodeError:
                        args = {}
                    tool = self.registry.get(tname)
                    # 工具执行期间的 KeyboardInterrupt 同样向上传播
                    result = tool.execute(self.context, args) if tool else f"Error: tool '{tname}' not found"
                    self.context.add_tool_result(tname, tid, result)
                    call_str = f"call: {tname}\nargs: {json.dumps(args, ensure_ascii=False)[:1024]}"
                    res_str = f"result:\n{result[:600]}"
                    log_box("tool", f"{call_str}\n{'─' * 36}\n{res_str}")
            else:
                return content or "(no response)"
            if finish == "stop":
                return content or "(stopped)"
        return "(max_steps reached)"


# =============================================================================
# 7. CLI Entry Point
# =============================================================================


def _read_input_auto(timeout: float = 0.08) -> str:
    """Read input and auto-detect multi-line paste.

    In an interactive terminal, after reading the first line a short timeout
    window is opened.  Lines arriving within that window are treated as a
    single pasted block and merged together.  Lines typed manually have a
    longer inter-line delay, so only the current line is returned after the
    timeout expires.
    """
    import queue
    import threading

    # Lazily start a background reader thread (daemon — exits with the main process).
    if not hasattr(_read_input_auto, "_queue"):
        q: queue.Queue = queue.Queue()
        ready = threading.Event()  # 主线程放好 prompt 后 set，线程读完后 clear
        _read_input_auto._queue = q  # type: ignore[attr-defined]
        _read_input_auto._prompt_holder = [""]  # type: ignore[attr-defined]
        _read_input_auto._ready = ready  # type: ignore[attr-defined]

        def _reader() -> None:
            while True:
                _read_input_auto._ready.wait()  # 等主线程准备好 # type: ignore[attr-defined]
                prompt = _read_input_auto._prompt_holder[0]  # type: ignore[attr-defined]
                _read_input_auto._ready.clear()  # type: ignore[attr-defined]
                try:
                    q.put(input(prompt))
                except (EOFError, KeyboardInterrupt):
                    q.put(None)
                    break

        threading.Thread(target=_reader, daemon=True).start()

    q = _read_input_auto._queue  # type: ignore[attr-defined]
    _read_input_auto._prompt_holder[0] = "> "  # type: ignore[attr-defined]
    _read_input_auto._ready.set()  # type: ignore[attr-defined]

    first = q.get()
    if first is None:
        raise EOFError()

    lines = [first]

    # Non-interactive terminal (pipe / redirect): skip multi-line detection.
    if not sys.stdin.isatty():
        return first

    # Collect subsequent lines within the timeout window (pasted lines arrive instantly).
    while True:
        try:
            line = q.get(timeout=timeout)
            if line is None:
                break
            lines.append(line)
        except queue.Empty:
            break

    return "\n".join(lines)


def main(argv=None) -> None:
    import argparse

    # 从项目根目录的 .env 文件补充环境变量（如果尚未通过系统环境变量注入）
    _load_dotenv()

    parser = argparse.ArgumentParser(description="Reasonix Kernel (Python)")
    parser.add_argument("--root", default=".", help="Workspace root")
    parser.add_argument("--model", default="", help="Override default model")
    parser.add_argument("--resume", default="", help="Resume session ID")
    parser.add_argument("request", nargs="?", help="User request to execute")
    args = parser.parse_args(argv)
    ctrl = Controller(workspace_root=args.root)

    if args.resume:
        if ctrl.load_session(args.resume):
            register_all_builtins(ctrl.registry, ctrl.cfg, ctrl.root)
            if not ctrl.cfg.providers:
                raise RuntimeError("No providers configured. Add at least one provider to config.yaml.")
            default = next((p for p in ctrl.cfg.providers if p.default), ctrl.cfg.providers[0])
            ctrl.provider = Provider(default)
            log_box("boot", f"Resumed session: {args.resume}\nWorkspace: {ctrl.root}\nMessages: {len(ctrl.context.messages)}")
        else:
            print(f"Session '{args.resume}' not found. Starting fresh.")
            ctrl.boot()
    else:
        ctrl.boot()

    if args.request:
        print(f"\n=== Result ===\n{ctrl.run(args.request)}")
    else:
        print("Reasonix ready. Enter requests (Ctrl-D to exit). Commands: /new, /model")
        while True:
            try:
                req = _read_input_auto()
            except EOFError:
                print()
                break
            except KeyboardInterrupt:
                print()
                continue
            req = req.strip()
            if not req:
                continue
            if req == "/new":
                sid = ctrl.save_session()
                ctrl.reset_context()
                print(f"New context started. Previous session: --resume {sid}")
                continue
            if req.startswith("/model"):
                parts = req.split(None, 1)
                if len(parts) == 1:
                    print("\n".join(ctrl.list_providers()))
                else:
                    print(ctrl.switch_provider(parts[1]))
                continue
            try:
                print(f"\n{ctrl.run(req)}\n")
            except KeyboardInterrupt:
                print("\n\n⚠️  已取消（Ctrl+C）。上下文已保留，可继续输入新请求。")
                continue
        sid = ctrl.save_session()
        print(f"\nSession saved. Resume with: --resume {sid}")


# Rebuild models to resolve forward references
Config.model_rebuild()
AgentConfig.model_rebuild()
ToolsConfig.model_rebuild()
SkillsConfig.model_rebuild()
PermissionsConfig.model_rebuild()
SandboxConfig.model_rebuild()
ShellConfig.model_rebuild()
ProviderEntry.model_rebuild()
SkillEntry.model_rebuild()

if __name__ == "__main__":
    import sys

    # 启动时从 .env 文件补充环境变量（不覆盖已有的系统环境变量）
    _load_dotenv()

    if "ipykernel" not in sys.argv[0]:
        main(sys.argv[1:])
