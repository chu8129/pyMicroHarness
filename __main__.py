#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Harness Kernel - Python Micro-code Representation

Changes from v1:
  - All config loaded from YAML (Pydantic models)
  - Skills are configurable via YAML, not hardcoded
  - Significant code simplification
  - PermissionManager integrated directly

Usage Example:
  # Simple query
  python3 run.py "Hello, who are you?"

  # Enable plan mode and ask
  python3 run.py --root .
  > /plan
  > Create a new file named test.txt with content 'hello'
"""

from __future__ import annotations

# =============================================================================
# Imports
# =============================================================================
import os
import socket

os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
os.environ["LITELLM_DISABLE_PRICING"] = "True"
import ipaddress
import sys
import json
import re
import glob
import time
import uuid
import httpx
import subprocess
import asyncio
import threading
from collections import OrderedDict, deque

if sys.platform != "win32":
    import readline

import urllib.request
import yaml
from loguru import logger
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Tuple
from enum import Enum
from pathlib import Path
from pydantic import BaseModel, Field
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

console = Console()


def rich_print(text: str):
    console.print(Markdown(text))


# =============================================================================
# OpenTelemetry Trace
# =============================================================================

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, ConsoleSpanExporter

_trace_provider = TracerProvider()
_trace_provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
trace.set_tracer_provider(_trace_provider)
tracer = trace.get_tracer("harness")


# =============================================================================
# Nsjail Sandbox Decorator
# =============================================================================

import functools
import shlex
import shutil

_NSJAIL_BIN: Optional[str] = None


def _find_nsjail(cfg_path: str = "nsjail") -> Optional[str]:
    global _NSJAIL_BIN
    if _NSJAIL_BIN is None:
        _NSJAIL_BIN = shutil.which(cfg_path) or shutil.which("nsjail")
    return _NSJAIL_BIN


def _check_path(path: str, cfg: "SandboxConfig", workspace: str) -> Optional[str]:
    """Returns error string if path violates sandbox policy, else None."""
    resolved = str(Path(path).resolve())
    for blocked in cfg.blocked_paths:
        if resolved.startswith(str(Path(blocked).resolve())):
            return f"Sandbox: blocked path: {resolved}"
    for ap in cfg.allowed_paths + [workspace]:
        if resolved.startswith(str(Path(ap).resolve())):
            return None
    return f"Sandbox: path outside allowed scope: {resolved}"


def _build_nsjail_cmd(command: str, *, nsjail: str, cfg: "SandboxConfig", workspace: str, rw: bool) -> str:
    """Build nsjail-wrapped command string."""
    cmd = [
        nsjail,
        "--mode",
        "o",
        "--quiet",
        "--rlimit_as",
        str(cfg.rlimit_as_mb),
        "--rlimit_cpu",
        str(cfg.rlimit_cpu_s),
        "--rlimit_fsize",
        str(cfg.rlimit_fsize_mb),
        "--chroot",
        "/",
    ]
    if cfg.cgroup_mem_max_mb > 0 and Path("/sys/fs/cgroup/memory").is_dir():
        cmd += ["--cgroup_mem_max", str(cfg.cgroup_mem_max_mb * 1024 * 1024)]
    workspace_flag = "--bindmount" if rw else "--bindmount_ro"
    cmd += [workspace_flag, f"{workspace}:{workspace}"]
    for p in cfg.allowed_paths:
        cmd += ["--bindmount", f"{p}:{p}"]
    if cfg.net_disabled:
        cmd += ["--disable_clone_newnet"]
    cmd += ["--", "/bin/bash", "-c", shlex.quote(command)]
    return " ".join(cmd)


def sandboxed(mode: str = "r"):
    """Decorator factory: @sandboxed("rw") or @sandboxed("r") or @sandboxed("net").

    - "r"   : path check (read-only scope enforcement)
    - "rw"  : path check (write scope enforcement)
    - "net" : block if sandbox has net_disabled=True

    Applied to SafeTool.__call__(self, ctx, args).
    If sandbox disabled, falls through transparently.
    """

    def decorator(fn):
        @functools.wraps(fn)
        async def wrapper(self, ctx, args):
            cfg = ctx.cfg.sandbox if hasattr(ctx, "cfg") else None
            if not cfg or not cfg.enabled:
                return await fn(self, ctx, args)

            workspace = getattr(ctx, "root", ".")
            tool_name = self.name() if hasattr(self, "name") else fn.__name__

            if mode == "net":
                if cfg.net_disabled:
                    logger.info(f"[sandbox] BLOCKED net | tool={tool_name} | net_disabled=True")
                    return "Error: Sandbox: network access is disabled."
                logger.debug(f"[sandbox] ALLOWED net | tool={tool_name}")
                return await fn(self, ctx, args)

            # Path-based check for file tools
            if "path" in args:
                err = _check_path(args["path"], cfg, workspace)
                if err:
                    logger.info(f"[sandbox] BLOCKED {mode} | tool={tool_name} | path={args['path']} | workspace={workspace} | allowed={cfg.allowed_paths} | blocked={cfg.blocked_paths}")
                    return f"Error: {err}"

            logger.debug(f"[sandbox] ALLOWED {mode} | tool={tool_name} | path={args.get('path', 'N/A')}")
            return await fn(self, ctx, args)

        return wrapper

    return decorator


def _load_dotenv(env_file: str = ".env") -> None:
    """Load environment variables from .env file."""
    try:
        from dotenv import load_dotenv

        load_dotenv(env_file, override=False)
    except ImportError:
        raise ImportError("Required dependency 'python-dotenv' is missing. Please install it with: pip install python-dotenv")


def _stdout(msg: str):
    sys.stdout.write(msg + "\n")
    sys.stdout.flush()


class MCPServerConfig(BaseModel):
    enabled: bool = True
    type: str
    url: str


class SubagentProfileConfig(BaseModel):
    """Named subagent profile — configurable model and prompt per agent type."""

    model: str = ""
    prompt: str = ""
    max_steps: int = 30


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
    subagents: Dict[str, SubagentProfileConfig] = Field(default_factory=dict)
    output_style: str = ""
    compact_ratio: float = 0.8
    compact_force_ratio: float = 0.9


class ShellConfig(BaseModel):
    path: str = ""


class ReflectionToolConfig(BaseModel):
    name: str
    description: str
    command: str
    read_only: bool = True
    schema_dict: dict = Field(alias="schema", default_factory=dict)
    platform: Optional[str] = None


class ToolsConfig(BaseModel):
    enabled: List[str] = Field(default_factory=list)
    reflection_tools: List[ReflectionToolConfig] = Field(default_factory=list)
    shell: ShellConfig = Field(default_factory=ShellConfig)
    bash_timeout_seconds: int = 120


class SandboxConfig(BaseModel):
    enabled: bool = False
    allowed_paths: List[str] = Field(default_factory=list)
    blocked_paths: List[str] = Field(default_factory=list)
    nsjail_path: str = "nsjail"
    rlimit_as_mb: int = 512
    rlimit_cpu_s: int = 30
    rlimit_fsize_mb: int = 64
    cgroup_mem_max_mb: int = 512
    net_disabled: bool = True


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
    delay_seconds: float = 0.0
    retry_times: int = 3
    price: Dict[str, float] = Field(default_factory=dict)
    effort: str = ""
    thinking: str = ""


class SkillEntry(BaseModel):
    """Skill definition from YAML — replaces hardcoded BUILTIN_SKILLS."""

    name: str
    description: str = ""
    body: str = ""
    path: str = ""
    allowed_tools: List[str] = Field(default_factory=list)
    run_as: str = "subagent"


class FeishuConfig(BaseModel):
    """Feishu/Lark gateway settings — used by `python . gateway`."""

    app_id: str = ""
    app_secret: str = ""
    domain: str = "feishu"  # feishu (China) | lark (international)
    encrypt_key: str = ""
    verification_token: str = ""
    allow_from: List[str] = Field(default_factory=list)  # sender open_ids, empty = allow all
    group_policy: str = "mention"  # mention (only when @bot) | all
    reply_to_message: bool = True  # quote the user's message when replying
    ask_timeout_seconds: int = 300  # how long an `ask` question waits for a chat reply
    max_concurrent_turns: int = 4
    max_chats: int = 50

    @property
    def credentials(self) -> tuple:
        """Return (app_id, app_secret), falling back to FEISHU_APP_ID / FEISHU_APP_SECRET."""
        return (
            (self.app_id or os.environ.get("FEISHU_APP_ID", "")).strip(),
            (self.app_secret or os.environ.get("FEISHU_APP_SECRET", "")).strip(),
        )


class Config(BaseModel):
    """Root configuration model — loaded from YAML."""

    default_model: str = ""
    feishu: FeishuConfig = Field(default_factory=FeishuConfig)
    providers: List[ProviderEntry] = Field(default_factory=list)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    mcp_servers: Dict[str, MCPServerConfig] = Field(default_factory=dict)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    skills: List[dict] = Field(default_factory=list)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    plan_mode_marker: str = ""
    plan_approved_message: str = ""

    @classmethod
    def load_for_root(cls, workspace_root: str) -> Config:
        """Load config from YAML with resolution: project > user > defaults."""
        cfg = Config()

        project_config = Path("config.yaml")
        global_config = Path.home() / ".config" / "harness" / "config.yaml"

        if project_config.exists():
            cfg = cfg._merge_yaml(project_config)
        elif global_config.exists():
            cfg = cfg._merge_yaml(global_config)

        all_skills = []
        if cfg.skills:
            all_skills.extend([SkillEntry.model_validate(s) for s in cfg.skills])

        skills_dir = Path(workspace_root) / "skills"
        if skills_dir.exists():
            for s_dir in skills_dir.iterdir():
                if not s_dir.is_dir():
                    continue
                md_path = s_dir / "SKILL.md"
                if not md_path.exists():
                    continue
                content = md_path.read_text(encoding="utf-8")
                data = {"name": s_dir.name, "body": content, "path": str(s_dir.resolve())}
                if content.startswith("---"):
                    parts = content.split("---", 2)
                    if len(parts) >= 3:
                        fm = yaml.safe_load(parts[1]) or {}
                        if isinstance(fm, dict):
                            data["name"] = fm.get("name", s_dir.name)
                            data["description"] = fm.get("description", "")
                all_skills.append(SkillEntry.model_validate(data))

        cfg._skills_data = all_skills
        return cfg

    def _merge_yaml(self, path: Path) -> Config:
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
        file_path_str = stripped[5:].strip()
        file_path = Path(file_path_str)
        if not file_path.is_absolute():
            file_path = Path(root) / file_path
        try:
            return file_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise FileNotFoundError(f"system_prompt file not found: {file_path}")

    def resolve_system_prompt(self, root: str) -> str:
        base = self._resolve_text_or_file(self.agent.system_prompt, root)
        if self.agent.language_policy:
            base += "\n\n" + self._resolve_text_or_file(self.agent.language_policy, root)
        if self.agent.output_style:
            base += f"\n\nOutput style: {self.agent.output_style}"
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
        return self.skills_data


# =============================================================================
# Logging helpers
# =============================================================================

_LOG_STYLES = {"user": ("📝 USER", "│"), "model": ("🤖 MODEL", "│"), "tool": ("🔧 TOOL", "│"), "mcp": ("🌐 MCP", "│"), "boot": ("🚀 SYSTEM STARTUP", "║"), "help": ("ℹ️ HELP", "│")}


def log_box(category: str, text: str, max_width: int = 0) -> None:
    """Print text in a styled box using rich.panel."""
    style = _LOG_STYLES.get(category, (category.upper(), "│"))
    label, _ = style

    if category == "boot":
        formatted_text = text.replace("],", "],\n").replace(", ", "\n  ")
    else:
        formatted_text = text

    panel = Panel(
        formatted_text,
        title=f"[bold]{label}[/bold]",
        subtitle=None,
        border_style="blue" if category != "boot" else "green",
        expand=False,
    )
    console.print(panel)


def _deep_merge(base: dict, override: dict) -> dict:
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        elif key in result and isinstance(result[key], list) and isinstance(value, list):
            result[key] = value
        else:
            result[key] = value
    return result


# =============================================================================
# Shared async HTTP client pool (concurrency-limited)
# =============================================================================

_http_pool: httpx.AsyncClient | None = None
_HTTP_MAX_CONCURRENCY = 10


async def get_http_client() -> "httpx.AsyncClient":
    global _http_pool
    if _http_pool is None:
        import httpx

        _http_pool = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10, read=30, write=10, pool=10),
            follow_redirects=True,
            limits=httpx.Limits(max_connections=_HTTP_MAX_CONCURRENCY, max_keepalive_connections=5),
        )
    return _http_pool


async def close_http_pool():
    global _http_pool
    if _http_pool is not None:
        try:
            await _http_pool.aclose()
        except RuntimeError:
            pass
        _http_pool = None


class Tool(ABC):
    @abstractmethod
    def name(self) -> str: ...
    @abstractmethod
    def description(self) -> str: ...
    @abstractmethod
    def schema(self) -> dict: ...
    @abstractmethod
    async def execute(self, ctx: Any, args: dict) -> str: ...
    @abstractmethod
    def read_only(self) -> bool: ...

    def to_dict(self) -> dict:
        return {"type": "function", "function": {"name": self.name(), "description": self.description(), "parameters": self.schema()}}


# =============================================================================
# Subagent Manager
# =============================================================================

import dataclasses
import uuid as _uuid


@dataclasses.dataclass
class _SubagentTask:
    """Tracks one running subagent."""

    label: str
    task: asyncio.Task
    session_key: str


class SubagentManager:
    """Manages background subagent lifecycle: spawn, track, cancel."""

    def __init__(self, *, max_concurrent: int = 4):
        self.max_concurrent = max_concurrent
        self._running: Dict[str, _SubagentTask] = {}

    def get_running_count(self) -> int:
        self._prune()
        return len(self._running)

    def get_running_count_by_session(self, session_key: str) -> int:
        self._prune()
        return sum(1 for t in self._running.values() if t.session_key == session_key)

    def _prune(self):
        for tid in [tid for tid, t in self._running.items() if t.task.done()]:
            del self._running[tid]

    async def spawn(
        self,
        *,
        task_prompt: str,
        agent_name: str = "",
        label: str = "",
        session_key: str = "",
        parent_controller: Any,
        pending_queue: "asyncio.Queue",
    ) -> str:
        """Spawn a subagent. Returns a status message for the LLM."""
        self._prune()
        if len(self._running) >= self.max_concurrent:
            return f"Cannot spawn subagent: concurrency limit reached " f"({len(self._running)}/{self.max_concurrent} running). " f"Wait for a running subagent to complete before spawning."

        task_id = _uuid.uuid4().hex[:12]
        effective_label = label or task_prompt[:50]

        async def _run_subagent():
            """Run a full agent turn in an isolated context and push result back."""
            try:
                cfg = parent_controller.cfg

                # Resolve subagent profile: named profile > global subagent_model > parent provider
                profile = cfg.agent.subagents.get(agent_name) if agent_name else None
                model_name = (profile.model if profile and profile.model else "") or cfg.agent.subagent_model

                if model_name:
                    provider_entry = next(
                        (p for p in cfg.providers if p.model == model_name or p.name == model_name),
                        None,
                    )
                    assert provider_entry is not None, f"No provider found for subagent model '{model_name}' in config.yaml providers"
                else:
                    provider_entry = parent_controller.provider.entry

                max_steps = (profile.max_steps if profile else 0) or min(cfg.agent.max_steps or 30, 30)

                # Build system prompt: profile prompt > default
                extra_prompt = (profile.prompt if profile and profile.prompt else "") or ("You are a subagent spawned to handle a specific task. " "Complete the task thoroughly and report your results. " "Be concise but complete in your final answer.")
                system_prompt = parent_controller._sys_prompt_with_capabilities(cfg.resolve_system_prompt(parent_controller.root), extra_prompt, exclude=("spawn",))

                sub_context = Context(system_prompt, cfg.agent, parent_controller.perm_manager)
                sub_context.add_user(task_prompt)
                provider = Provider(provider_entry)

                for _ in range(max_steps):
                    response = await provider.chat(
                        sub_context.to_openai(),
                        [t for t in parent_controller.registry.schemas() if t["function"]["name"] != "spawn"],
                        cfg.agent.temperature,
                    )

                    content = response.get("content", "")
                    tool_calls = response.get("tool_calls", [])
                    finish = response.get("finish_reason", "")

                    if finish in ("error", "interrupted"):
                        return content or "(subagent error)"

                    sub_context.add_assistant(content or "", tool_calls=tool_calls or None)

                    if not tool_calls:
                        return content or "(no response)"

                    for tc in tool_calls:
                        fn = tc.get("function", {})
                        tname = fn.get("name", "")
                        try:
                            args = json.loads(fn.get("arguments", "{}")) if isinstance(fn.get("arguments"), str) else fn.get("arguments", {})
                        except json.JSONDecodeError:
                            args = {}
                        result = await parent_controller.registry.execute_gated(tname, parent_controller, args)
                        sub_context.add_tool_result(tname, tc.get("id", "unknown"), result)

                    if finish == "stop":
                        return content or ""

                return "(subagent max_steps reached)"

            except asyncio.CancelledError:
                return "(subagent cancelled)"
            except Exception as e:
                logger.error(f"Subagent {task_id} failed: {e}")
                return f"(subagent error: {e})"

        async def _task_wrapper():
            result = await _run_subagent()
            await pending_queue.put({"task_id": task_id, "label": effective_label, "content": result})
            logger.info(f"Subagent {task_id} completed.")

        self._running[task_id] = _SubagentTask(
            label=effective_label,
            task=asyncio.create_task(_task_wrapper()),
            session_key=session_key,
        )

        _stdout(f"  🚀 Subagent spawned: [{task_id}] {effective_label}")
        return f"Subagent spawned (task_id={task_id}). " f"It will report results when done. You can continue with other work."

    async def cancel_all(self) -> int:
        """Cancel all running subagents. Returns count cancelled."""
        self._prune()
        count = sum(1 for st in self._running.values() if not st.task.done() and st.task.cancel())
        self._running.clear()
        return count

    async def cancel_by_session(self, session_key: str) -> int:
        """Cancel subagents for a specific session."""
        self._prune()
        to_cancel = [tid for tid, st in self._running.items() if st.session_key == session_key]
        count = 0
        for tid in to_cancel:
            st = self._running.pop(tid)
            if not st.task.done():
                st.task.cancel()
                count += 1
        return count


class SafeTool(Tool, ABC):
    """Base class for tools that wraps execution in a robust error handler."""

    async def execute(self, ctx: Any, args: dict) -> str:
        try:
            return await self(ctx, args)
        except Exception as e:
            logger.exception(f"Tool {self.name()} execution failed")
            return f"Error: Tool execution failed - {str(e)}"

    async def __call__(self, ctx, args):
        return await self.execute(ctx, args)

    @abstractmethod
    async def __call__(self, ctx: Any, args: dict) -> str: ...


class WriteFileTool(SafeTool):
    def name(self):
        return "write_file"

    def description(self):
        return "Write content to a file. Overwrites if exists (Python version)."

    def schema(self):
        return {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}

    def read_only(self):
        return False

    @sandboxed("rw")
    async def __call__(self, ctx, args):
        if not await ctx.perm_manager.check_and_request_permission(ctx, args["path"]):
            return "Error: Permission denied by user."

        path = Path(args["path"]).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(args["content"], encoding="utf-8")
        return f"Wrote to {path} (via Python)"


class ReadFileTool(SafeTool):
    def name(self):
        return "read_file"

    def description(self):
        return "Read a file's contents (Python version)."

    def schema(self):
        return {"type": "object", "properties": {"path": {"type": "string"}, "start_line": {"type": "integer", "description": "Start line number (1-indexed)."}, "end_line": {"type": "integer", "description": "End line number (inclusive)."}, "max_chars": {"type": "integer", "description": "Maximum characters to read.", "default": 100000}}, "required": ["path", "start_line", "end_line"]}

    def read_only(self):
        return True

    @sandboxed("r")
    async def __call__(self, ctx, args):
        path = Path(args["path"]).resolve()
        if not path.exists():
            return f"Error: File not found: {path}"

        start = args.get("start_line", 1)
        end = args.get("end_line", 1)
        max_chars = args.get("max_chars", 100000)

        content = []
        current_chars = 0
        with open(path, "r", encoding="utf-8") as f:
            for i, line in enumerate(f, 1):
                if i >= start:
                    if i > end:
                        break
                    content.append(line)
                    current_chars += len(line)
                    if current_chars >= max_chars:
                        break
        return "".join(content)


class EditFileTool(SafeTool):
    def name(self):
        return "edit_file"

    def description(self):
        return "Edit a file with search/replace (Python version)."

    def schema(self):
        return {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}

    def read_only(self):
        return False

    @sandboxed("rw")
    async def __call__(self, ctx, args):
        if not await ctx.perm_manager.check_and_request_permission(ctx, args["path"]):
            return "Error: Permission denied by user."

        path = Path(args["path"]).resolve()
        content = path.read_text(encoding="utf-8")
        if args["old_text"] not in content:
            return f"Error: old_text not found in {path}"
        path.write_text(content.replace(args["old_text"], args["new_text"], 1), encoding="utf-8")
        return f"Edited {path} (via Python)"


class ReflectionShellTool(SafeTool):
    def __init__(self, config: ReflectionToolConfig):
        self.config = config

    def name(self):
        return self.config.name

    def description(self):
        return self.config.description

    def schema(self):
        return self.config.schema_dict

    def read_only(self):
        return self.config.read_only

    async def __call__(self, ctx, args):
        if self.config.read_only == False and not await ctx.perm_manager.check_and_request_permission(ctx, args.get("path", "")):
            return "Error: Permission denied by user."

        full_cmd = self.config.command.format(**args)
        try:
            result = subprocess.check_output(full_cmd, shell=True, text=True, stderr=subprocess.STDOUT)
            return result
        except subprocess.CalledProcessError as e:
            return f"Error executing {self.config.name}: {e.output}"

        return "Apply multiple edits to a file atomically."

    def schema(self):
        return {"type": "object", "properties": {"path": {"type": "string"}, "edits": {"type": "array", "items": {"type": "object", "properties": {"old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["old_text", "new_text"]}}}, "required": ["path", "edits"]}

    def read_only(self):
        return False

    async def __call__(self, ctx, args):
        edit_tool = EditFileTool()
        results = []
        for edit in args["edits"]:
            res = await edit_tool(ctx, {"path": args["path"], "old_text": edit["old_text"], "new_text": edit["new_text"]})
            results.append(res)
            if res.startswith("Error"):
                return f"Multi-edit failed: {res}"
        return f"Applied {len(args['edits'])} edits to {args['path']}"


class BashTool(SafeTool):
    def __init__(self, path="", timeout=120):
        self.path, self.timeout = path, timeout
        self.active_processes = []
        self.allowed_patterns = []

    def name(self):
        return "bash"

    def description(self):
        return "Execute a shell command. Use for builds, tests, git, package managers."

    def schema(self):
        return {"type": "object", "properties": {"command": {"type": "string"}, "run_in_background": {"type": "boolean"}}, "required": ["command"]}

    def read_only(self):
        return False

    def _clean_command(self, command: str) -> str:
        lines = [line for line in command.splitlines() if line.strip() and not line.strip().startswith("#")]
        return "\n".join(lines)

    @sandboxed("rw")
    async def __call__(self, ctx, args):
        command = self._clean_command(args["command"])

        for pattern in self.allowed_patterns:
            if re.search(pattern, command):
                logger.info(f"Bash command matched allowed pattern: {pattern}")
                return await self._execute(command, ctx)

        cmd_base = command.split("\n")[0].split()[0]
        suggestions = [rf"^{re.escape(cmd_base)}\s*.*", ".*"]

        # Prefer the registered `ask` tool: the Feishu gateway swaps it for one that posts
        # the question into the chat and waits for a reply, instead of blocking on stdin.
        ask_tool = (ctx.registry.get("ask") if getattr(ctx, "registry", None) else None) or AskTool()
        display_options = [p.replace(r"\s*", " ") if p != ".*" else "Allow all commands" for p in suggestions] + ["Run once", "Deny"]
        choice = await ask_tool(ctx, {"question": f"Command requires approval: {command}. Choose a pattern to allow or an action:", "options": display_options})

        if choice in display_options:
            idx = display_options.index(choice)
            if idx < len(suggestions):
                choice = suggestions[idx]

        if choice == "Deny" or choice == "Cancelled":
            return "Execution denied by user."
        elif choice == "Run once":
            return await self._execute(command, ctx)
        elif choice in suggestions:
            self.allowed_patterns.append(choice)
            logger.info(f"Allowed pattern added: {choice}")
            return await self._execute(command, ctx)

        return f"Execution denied: Unrecognized choice '{choice}'."

    async def _execute(self, command, ctx=None):
        actual_command = command
        if ctx and hasattr(ctx, "cfg") and ctx.cfg.sandbox.enabled:
            nsjail = _find_nsjail(ctx.cfg.sandbox.nsjail_path)
            if not nsjail:
                raise RuntimeError(f"Sandbox is enabled, but nsjail was not found at path: {ctx.cfg.sandbox.nsjail_path}")
            actual_command = _build_nsjail_cmd(command, nsjail=nsjail, cfg=ctx.cfg.sandbox, workspace=getattr(ctx, "root", "."), rw=True)

        proc = await asyncio.create_subprocess_shell(
            actual_command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            executable=self.path or None,
        )
        self.active_processes.append(proc)
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self.timeout)
            if proc in self.active_processes:
                self.active_processes.remove(proc)
            out = stdout.decode(errors="replace") if stdout else ""
            if proc.returncode != 0:
                err = stderr.decode(errors="replace") if stderr else ""
                logger.error(f"Bash command failed: {command}\n{err}")
                out += f"\n[exit {proc.returncode}]\n{err}"
            return out or "(no output)"
        except asyncio.TimeoutError:
            proc.kill()
            if proc in self.active_processes:
                self.active_processes.remove(proc)
            return f"Command timed out after {self.timeout}s"
        except KeyboardInterrupt:
            proc.kill()
            if proc in self.active_processes:
                self.active_processes.remove(proc)
            return "⚠️  Operation cancelled."

    def __del__(self):
        for proc in self.active_processes:
            try:
                proc.kill()
            except:
                pass
        self.active_processes = []


class GrepTool(SafeTool):
    def name(self):
        return "grep"

    def description(self):
        return "Search for a regex pattern in files."

    def schema(self):
        return {"type": "object", "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}}, "required": ["pattern", "path"]}

    def read_only(self):
        return True

    @sandboxed("r")
    async def __call__(self, ctx, args):
        pattern, path = args["pattern"], Path(args["path"])
        rx = re.compile(pattern)
        files = [path] if path.is_file() else [p for p in path.rglob("*") if p.is_file()]
        matches = []
        for fp in files:
            try:
                for i, line in enumerate(fp.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                    if rx.search(line):
                        matches.append(f"{fp}:{i}:{line}")
                        if len(matches) >= 3000:  # Simple safety limit
                            matches.append("... (limit reached)")
                            break
            except Exception as e:
                logger.warning(f"Error reading {fp}: {e}")
                continue
        return "\n".join(matches) if matches else "(no matches)"


class GlobTool(SafeTool):
    def name(self):
        return "glob"

    def description(self):
        return "Find files matching a glob pattern."

    def schema(self):
        return {"type": "object", "properties": {"pattern": {"type": "string"}}, "required": ["pattern"]}

    def read_only(self):
        return True

    @sandboxed("r")
    async def __call__(self, ctx, args):
        return "\n".join(glob.glob(args["pattern"], recursive=True)) or "(no matches)"


class LsTool(SafeTool):
    def name(self):
        return "ls"

    def description(self):
        return "List directory contents."

    def schema(self):
        return {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}

    def read_only(self):
        return True

    @sandboxed("r")
    async def __call__(self, ctx, args):
        p = Path(args["path"])
        if not p.exists():
            return f"Error: not found {p}"
        return "\n".join(f"{'d' if e.is_dir() else 'f'} {e.name}" for e in sorted(p.iterdir()))


class WebFetchTool(SafeTool):
    def __init__(self, proxy=None):
        self.proxy = proxy

    def name(self):
        return "web_fetch"

    def description(self):
        return "Fetch content from a URL with SSRF protection."

    def schema(self):
        return {"type": "object", "properties": {"url": {"type": "string"}, "headers": {"type": "object", "description": "Optional dictionary of HTTP headers"}}, "required": ["url"]}

    def read_only(self):
        return True

    def _is_safe_ip(self, ip_str: str) -> bool:
        try:
            ip = ipaddress.ip_address(ip_str)
            return not (ip.is_private or ip.is_loopback or ip.is_multicast or ip.is_link_local or ip.is_reserved or ip.is_unspecified)
        except Exception as e:
            logger.warning(f"Error occurred while checking IP safety for {ip_str}: {e}")
            return False

    @sandboxed("net")
    async def __call__(self, ctx, args):
        import httpx

        url = args["url"]
        headers = args.get("headers", {})
        if "User-Agent" not in headers:
            headers["User-Agent"] = "Harness/1.0"

        parsed = urllib.parse.urlparse(url)
        hostname = parsed.hostname

        ip = socket.gethostbyname(hostname)
        if not self._is_safe_ip(ip):
            return f"Error: Security policy violation - cannot fetch internal address {ip}"

        try:
            client = await get_http_client()
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            return resp.text
        except httpx.HTTPStatusError as e:
            return f"Error: Tool execution failed - HTTP Error {e.response.status_code}: {e.response.reason_phrase}"
        except Exception as e:
            raise RuntimeError(f"Error: Tool execution failed - {str(e)}")


class AskTool(SafeTool):
    def name(self):
        return "ask"

    def description(self):
        return "Ask the user for clarification when a consequential choice is required."

    def schema(self):
        return {"type": "object", "properties": {"question": {"type": "string"}, "options": {"type": "array", "items": {"type": "string"}}}, "required": ["question"]}

    def read_only(self):
        return True

    async def __call__(self, ctx: Any, args: dict) -> str:
        _stdout(f"\n[ASK] {args.get('question')}")
        options = args.get("options", [])
        for i, opt in enumerate(options, 1):
            _stdout(f"  {i}. {opt}")
        if not sys.stdin.isatty():
            return "<model-assumption> Proceeding with default."
        try:
            choice = input("Your choice (default: Yes): ").strip()
            if not choice:
                return "Yes"

            if choice.isdigit():
                idx = int(choice) - 1
                if 0 <= idx < len(options):
                    choice = options[idx]

            logger.info(f"User chose: {choice}")
            return choice
        except EOFError:
            return "Cancelled"
        except Exception as e:
            logger.exception(f"Tool {self.name()} execution failed")
            return f"Error: Tool execution failed - {str(e)}"


class TodoWriteTool(SafeTool):
    def name(self):
        return "todo_write"

    def description(self):
        return "Track multi-step task progress. Pass the full todo list each time. You can add, remove, or split todos mid-execution — e.g. expand one step into multiple sub-steps when complexity is discovered."

    def schema(self):
        return {"type": "object", "properties": {"todos": {"type": "array", "items": {"type": "object", "properties": {"id": {"type": "string"}, "content": {"type": "string"}, "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}}, "required": ["id", "content", "status"]}}}, "required": ["todos"]}

    def read_only(self):
        return False

    async def __call__(self, ctx, args):
        target = ctx.context if hasattr(ctx, "context") else ctx
        if hasattr(target, "todos"):
            target.todos = args["todos"]
            todo_display = "\n".join([f"[{t['status']}] {t['content']}" for t in args["todos"]])
            _stdout(f"\n\U0001f4dd TASK PROGRESS:\n{todo_display}\n")
        return todo_display


class WebSearchTool(SafeTool):
    def name(self):
        return "web_search"

    def description(self):
        return "Search the web using duckduckgo, google, or baidu."

    def schema(self):
        return {"type": "object", "properties": {"query": {"type": "string"}, "engine": {"type": "string", "enum": ["duckduckgo", "google", "baidu"], "default": "baidu"}, "count": {"type": "integer", "default": 5}}, "required": ["query"]}

    def read_only(self):
        return True

    @sandboxed("net")
    async def __call__(self, ctx, args):
        import httpx
        from bs4 import BeautifulSoup

        query, engine = args["query"], args.get("engine", "duckduckgo")
        configs = {
            "baidu": ("http://www.baidu.com/s", {"wd": query}, ".c-container"),
            "duckduckgo": ("https://html.duckduckgo.com/html/", {"q": query}, ".result"),
            "google": ("https://www.google.com/search", {"q": query}, ".tF2Cxc"),
        }

        if engine not in configs:
            return f"Unsupported engine: {engine}"

        url, params, selector = configs[engine]
        headers = {"User-Agent": "Mozilla/5.0"}

        client = await get_http_client()
        if engine == "duckduckgo":
            response = await client.post(url, data=params, headers=headers)
        else:
            response = await client.get(url, params=params, headers=headers)

        soup = BeautifulSoup(response.text, "html.parser")
        results = [res.get_text(separator=" ", strip=True) for res in soup.select(selector)]

        if not results and engine == "baidu":
            return soup.get_text(separator=" ", strip=True)[:2000]

        return "\n---\n".join(results) if results else f"No results found for {engine}."


class SpawnTool(SafeTool):
    """Tool for the LLM to spawn background subagents."""

    def __init__(self, manager: SubagentManager, profiles: Dict[str, "SubagentProfileConfig"] = None):
        self._manager = manager
        self._profiles = profiles or {}

    def name(self):
        return "spawn"

    def description(self):
        base = "Spawn a subagent to handle a task in the background. " "Use when: a task has multiple independent parts that can run in parallel; " "a subtask is complex enough to benefit from its own focused context; " "you need to research/read extensively without bloating your main context. " "Each subagent gets its own conversation and tools, reports back automatically. " "Do NOT spawn for trivial tasks (single file read, simple edits) — handle those directly."
        if self._profiles:
            agents_desc = "; ".join(f"'{name}'" + (f" ({p.prompt[:60]}...)" if len(p.prompt) > 60 else f" ({p.prompt})" if p.prompt else "") for name, p in self._profiles.items())
            base += f" Available agents: {agents_desc}."
        return base

    def schema(self):
        return {"type": "object", "properties": {"task": {"type": "string", "description": "The task for the subagent. Be specific and include all necessary context."}, "agent": {"type": "string", "description": "Optional named subagent profile from config (e.g. 'researcher', 'coder'). Uses default if omitted."}, "label": {"type": "string", "description": "Optional short label for display."}}, "required": ["task"]}

    def read_only(self):
        return True

    async def __call__(self, ctx, args):
        assert hasattr(ctx, "subagent_manager") and ctx.subagent_manager is not None, "Subagent system not initialized"
        assert hasattr(ctx, "_pending_queue") and ctx._pending_queue is not None, "Pending queue not available (subagents require async turn context)"

        return await ctx.subagent_manager.spawn(
            task_prompt=args["task"],
            agent_name=args.get("agent", ""),
            label=args.get("label", ""),
            session_key=ctx.current_session_id or "default",
            parent_controller=ctx,
            pending_queue=ctx._pending_queue,
        )


# =============================================================================
# Plan Mode support
# =============================================================================

# (Moved to config.yaml)


def parse_plan_todos(plan: str) -> List[dict]:
    todos: List[dict] = []
    # Regex explanation:
    # ^\s*                : Allow leading spaces
    # (?:-|\*|\+|\d+\.)   : Match list marker (- or * or + or 1.)
    # \s+                 : Match whitespace after marker
    # (.*?)               : Non-greedy match content
    # (?:\n|$)            : End at newline or EOF
    pattern = re.compile(r"^\s*(?:-|\*|\+|\d+\.)\s+(.+)$", re.MULTILINE)

    for match in pattern.finditer(plan):
        content = match.group(1).strip()
        # Clear Markdown modifiers for clean output
        clean_content = re.sub(r"[`*~_]+", "", content).strip()
        if clean_content:
            status = "in_progress" if len(todos) == 0 else "pending"
            todos.append({"id": str(len(todos) + 1), "content": clean_content, "status": status, "level": 0})  # Simplified to flat structure
        if len(todos) >= 20:
            break

    return todos


# =============================================================================
# 3. Permission Manager
# =============================================================================


class PermissionManager:
    """Manages file-level write permissions with interactive approval."""

    def __init__(self):
        # Memory-only cache for the current session
        self.granted_paths = set()

    def check_permission(self, file_path: str) -> bool:
        path = Path(file_path).resolve()
        for granted in self.granted_paths:
            granted_path = Path(granted).resolve()
            if granted_path.is_dir() and (granted_path == path or granted_path in path.parents):
                return True
            if granted_path == path:
                return True
        return False

    async def check_and_request_permission(self, controller, file_path: str) -> bool:
        if self.check_permission(file_path):
            return True

        path_obj = Path(file_path).resolve()
        question = f"Need permission to access:\n  Path: {path_obj}\nAllow this operation (and future ones in this session for this file/folder)?"
        options = ["Yes", "No"]

        ask_tool = controller.registry.get("ask")
        if not ask_tool:
            return False

        choice = await ask_tool.execute(controller.context, {"question": question, "options": options})
        logger.debug(f"Permission choice received: {choice}")
        if choice.lower().strip() in ["1", "y", "yes", ""]:
            self.granted_paths.add(str(path_obj))
            return True
        return False


# =============================================================================
# 4. Tool Registry
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

    async def execute_gated(self, name: str, ctx: Any, args: dict) -> str:
        tool = self.get(name)
        if tool is None:
            return f"Error: tool '{name}' not found"

        # Validate required parameters before execution.
        schema = tool.schema()
        required = schema.get("required", [])
        missing = [p for p in required if p not in args]
        if missing:
            return f"Error: tool '{name}' missing required parameters: {', '.join(missing)}"
        return await tool.execute(ctx, args)


ALL_TOOLS = [ReadFileTool, WriteFileTool, EditFileTool, BashTool, GrepTool, GlobTool, LsTool, WebFetchTool, AskTool, TodoWriteTool, WebSearchTool]


def should_enable(tool_name: str, enabled_list: List[str]) -> bool:
    return not enabled_list or tool_name in enabled_list


def register_all_builtins(reg: Registry, cfg: Config, root: str, proxy=None) -> None:
    enabled = cfg.tools.enabled
    for cls in ALL_TOOLS:
        name = cls().name()
        if not should_enable(name, enabled):
            continue

        if cls is BashTool:
            reg.add(cls(path=cfg.tools.shell.path, timeout=cfg.tools.bash_timeout_seconds))
        elif cls is WebFetchTool:
            reg.add(cls(proxy=proxy))
        else:
            reg.add(cls())

    for rcfg in cfg.tools.reflection_tools:
        if not should_enable(rcfg.name, enabled):
            continue
        if rcfg.platform:
            allowed = [p.strip() for p in rcfg.platform.split(",")]
            if sys.platform not in allowed:
                continue
        reg.add(ReflectionShellTool(rcfg))


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

    def to_agent_dict(self, provider: str = "", model: str = "") -> dict:
        """Agent-style message: role + typed content blocks."""
        timestamp = int(time.time() * 1000)
        if self.role == MessageRole.TOOL:
            return {"role": "toolResult", "toolCallId": self.tool_call_id or "", "toolName": self.name or "", "content": [{"type": "text", "text": self.content or ""}], "isError": False, "timestamp": timestamp}
        if self.role == MessageRole.ASSISTANT:
            blocks = [{"type": "text", "text": self.content}] if self.content else []
            for call in self.tool_calls or []:
                fn = call.get("function") or {}
                args = fn.get("arguments")
                args = json.loads(args) if isinstance(args, str) else args
                blocks.append({"type": "toolCall", "id": call.get("id", ""), "name": fn.get("name", ""), "arguments": args if isinstance(args, dict) else {"value": args}})
            cost = {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0}
            usage = {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "reasoning": 0, "totalTokens": 0, "cost": cost}
            return {"role": "assistant", "content": blocks, "api": "openai-completions", "provider": provider, "model": model, "usage": usage, "stopReason": "toolUse" if self.tool_calls else "stop", "timestamp": timestamp}
        return {"role": "user", "content": [{"type": "text", "text": self.content or ""}], "timestamp": timestamp}

    @classmethod
    def from_agent_dict(cls, d: dict) -> "Message":
        content = d.get("content")
        blocks = content if isinstance(content, list) else []
        text = content if isinstance(content, str) else "\n".join(b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text")
        tool_calls = [{"id": b.get("id", ""), "type": "function", "function": {"name": b.get("name", ""), "arguments": json.dumps(b.get("arguments", {}), ensure_ascii=False)}} for b in blocks if isinstance(b, dict) and b.get("type") == "toolCall"]
        role = {"system": MessageRole.SYSTEM, "assistant": MessageRole.ASSISTANT, "toolResult": MessageRole.TOOL}.get(d.get("role"), MessageRole.USER)
        return cls(role, text, name=d.get("toolName"), tool_call_id=d.get("toolCallId"), tool_calls=tool_calls or None)


class Context:
    def __init__(self, system_prompt: str, cfg: AgentConfig, perm_manager: Any = None):
        self.system_prompt = system_prompt
        self.cfg = cfg
        self.perm_manager = perm_manager
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
        pass

    def compact(self, max_tokens: int, force: bool = False) -> None:
        ratio = self.cfg.compact_force_ratio if force else self.cfg.compact_ratio
        effective_limit = max_tokens * ratio
        if self.estimate_tokens() < effective_limit:
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
            self.messages = [Message(MessageRole.ASSISTANT, summary)] + keep

    def to_openai(self) -> List[dict]:
        openai_messages = [{"role": "system", "content": self.system_prompt}]
        for message in self.messages:
            openai_messages.append(message.to_dict())
        return openai_messages

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

    @tracer.start_as_current_span("llm_chat")
    async def chat(self, messages: List[dict], tools: List[dict], temperature: float = 0.0) -> dict:
        """Send request to LLM using litellm async (supporting OpenAI/Gemini/Anthropic, etc.)."""
        from litellm import acompletion, exceptions

        # Apply delay if configured
        if self.entry.delay_seconds > 0:
            logger.info(f"Delaying {self.entry.delay_seconds}s before request...")
            await asyncio.sleep(self.entry.delay_seconds)

        model = self.entry.model
        kind = self.entry.kind
        if self.entry.base_url and kind and not model.startswith(f"{kind}/"):
            model = f"{kind}/{model}"

        kwargs = {"model": model, "messages": messages, "temperature": temperature, "timeout": self.entry.request_timeout, "api_key": self.api_key}
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        if self.entry.base_url:
            kwargs["api_base"] = self.entry.base_url

        try:
            response = await acompletion(**kwargs)
        except KeyboardInterrupt:
            logger.warning("LLM call interrupted by user. Resuming...")
            return {"content": "(Interrupted by user)", "tool_calls": [], "finish_reason": "interrupted"}
        except exceptions.RateLimitError as e:
            logger.error(f"Rate limit exceeded: {e}")
            return {"content": "Error: API Rate Limit Exceeded. Please wait a moment and try again.", "tool_calls": [], "finish_reason": "error", "error": str(e)}
        except exceptions.AuthenticationError as e:
            logger.error(f"Authentication failed: {e}")
            return {"content": "Error: Authentication failed. Check your API key.", "tool_calls": [], "finish_reason": "error", "error": str(e)}
        except exceptions.ServiceUnavailableError as e:
            logger.error(f"Service Unavailable: {e}")
            return {"content": "Error: Service is currently unavailable (e.g. high load). Please try again in a few moments.", "tool_calls": [], "finish_reason": "error", "error": str(e)}
        except Exception as e:
            logger.error(f"LLM call error: {e}")
            return {"content": f"Error: {e}", "tool_calls": [], "finish_reason": "error", "error": str(e)}

        choice = response.choices[0]
        msg = choice.message
        tool_calls = []
        if msg.tool_calls:
            tool_calls = [{"id": tc.id, "type": "function", "function": {"name": tc.function.name, "arguments": tc.function.arguments}} for tc in msg.tool_calls]

        usage = response.usage
        usage_data = {}
        if usage:
            usage_data = {"input": usage.prompt_tokens, "output": usage.completion_tokens}
            cache_tokens = 0
            if hasattr(usage, "extra") and usage.extra is not None and "cache_hit_tokens" in usage.extra:
                cache_tokens = usage.extra["cache_hit_tokens"]
            elif hasattr(usage, "cache_read_input_tokens") and usage.cache_read_input_tokens:
                cache_tokens = usage.cache_read_input_tokens

            usage_data["cache_rate"] = f"{(cache_tokens / usage.prompt_tokens * 100) if usage.prompt_tokens > 0 else 0:.1f}%"

        return {"content": msg.content or "", "tool_calls": tool_calls, "finish_reason": choice.finish_reason or "", "usage_summary": json.dumps(usage_data)}


# =============================================================================
# 6. Agent Controller
# =============================================================================


class _MCPRemoteTool(SafeTool):
    """
    One remote tool exposed by an MCP server.
    - discover_all(): classmethod that connects to the server, discovers every tool and returns the instances
    - __call__(): opens a fresh connection and performs tools/call on each invocation
    """

    def __init__(self, server_url: str, tool_name: str, tool_description: str, tool_schema: dict):
        self._server_url = server_url
        self._tool_name = tool_name
        self._tool_description = tool_description
        self._tool_schema = tool_schema

    @classmethod
    async def discover_all(cls, server_name: str, url: str) -> list:
        """Connect to the MCP server, discover every tool and return the _MCPRemoteTool instances."""
        tools = []
        try:
            from mcp import ClientSession
            from mcp.client.sse import sse_client

            async with sse_client(url) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.list_tools()
                    for t in result.tools:
                        tools.append(
                            cls(
                                server_url=url,
                                tool_name=t.name,
                                tool_description=t.description or f"MCP tool from {server_name}",
                                tool_schema=t.inputSchema or {"type": "object", "properties": {}},
                            )
                        )
            logger.info(f"MCP '{server_name}' → {len(tools)} tools: {[t._tool_name for t in tools]}")
        except Exception as e:
            logger.warning(f"MCP Discovery failed for '{server_name}' ({url}): {e}")
        return tools

    def name(self) -> str:
        return f"mcp_{self._tool_name}"

    def description(self) -> str:
        return self._tool_description

    def schema(self) -> dict:
        return self._tool_schema

    def read_only(self) -> bool:
        return True

    @sandboxed("net")
    async def __call__(self, ctx, args) -> str:
        return await self._call_remote(args)

    async def _call_remote(self, args: dict) -> str:
        from mcp import ClientSession
        from mcp.client.sse import sse_client

        try:
            async with sse_client(self._server_url) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(self._tool_name, arguments=args)
                    parts = []
                    for item in result.content:
                        if hasattr(item, "text"):
                            parts.append(item.text)
                        elif hasattr(item, "data"):
                            parts.append(str(item.data))
                        else:
                            parts.append(str(item))
                    return "\n".join(parts) if parts else "(empty response)"
        except Exception as e:
            logger.error(f"MCP call failed: {self._tool_name} on {self._server_url}: {e}")
            return f"Error: MCP tool call failed — {e}"


class Controller:
    def __init__(self, workspace_root: str = "."):
        self.root = os.path.abspath(workspace_root)
        self.cfg = Config.load_for_root(self.root)
        self.registry = Registry()
        self.perm_manager = PermissionManager()
        self.context: Optional[Context] = None
        self.step_count = 0
        self._plan_mode: bool = self.cfg.agent.auto_plan
        self.current_session_id: Optional[str] = None
        self.subagent_manager: SubagentManager = SubagentManager(max_concurrent=4)
        self._pending_queue: Optional[asyncio.Queue] = None
        self._last_turn_error: str = ""

    def set_plan_mode(self, on: bool) -> None:
        self._plan_mode = on
        self.registry.plan_mode = on
        _stdout(f"Plan mode: {'ON' if on else 'OFF'}")

    def _sys_prompt_with_capabilities(self, base: str, extra: str = "", exclude: tuple = ()) -> str:
        """base (+ extra) followed by the live capabilities block."""

        def block() -> str:
            rows = [f"- {t.name()}: {' '.join(t.description().split())}" for t in sorted(self.registry.list(), key=lambda t: t.name()) if t.name() not in exclude]
            rows += [f"- /{s.name}: {' '.join((s.description or '').split())}" for s in sorted(self.cfg.enabled_skills(), key=lambda s: s.name)]
            servers = [n for n, c in sorted(self.cfg.mcp_servers.items()) if c.enabled]
            return ("Available capabilities:\n" + "\n".join(rows) + (f"\nMCP servers: {', '.join(servers)}" if servers else "")) if rows else ""

        return "\n\n".join(p for p in (base, extra, block()) if p)

    @property
    def plan_mode(self) -> bool:
        return self._plan_mode

    def _sessions_dir(self) -> Path:
        slug = re.sub(r"[/\\:]", "-", re.sub(r"^[/\\]", "", os.path.abspath(self.root)))
        d = Path.home() / ".pi" / "agent" / "sessions" / f"--{slug}--"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def save_session(self) -> str:
        if self.current_session_id:
            sid = self.current_session_id
        else:
            sid = str(uuid.uuid7() if hasattr(uuid, "uuid7") else uuid.uuid4())
            self.current_session_id = sid

        messages = self.context.messages if self.context else []
        provider = self.provider.entry.name if getattr(self, "provider", None) else ""
        model = self.provider.entry.model if getattr(self, "provider", None) else ""

        now = time.time()
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now)) + f".{int(now * 1000) % 1000:03d}Z"
        entries = [{"type": "session", "version": 3, "id": sid, "timestamp": timestamp, "cwd": self.root}]
        parent_id = None
        system_prompt = self.context.system_prompt if self.context else ""
        if system_prompt:
            entry_id = uuid.uuid4().hex[:8]
            entries.append({"type": "message", "id": entry_id, "parentId": None, "timestamp": timestamp, "message": {"role": "system", "content": [{"type": "text", "text": system_prompt}], "timestamp": int(now * 1000)}})
            parent_id = entry_id
        for message in messages:
            entry_id = uuid.uuid4().hex[:8]
            entries.append({"type": "message", "id": entry_id, "parentId": parent_id, "timestamp": timestamp, "message": message.to_agent_dict(provider, model)})
            parent_id = entry_id
        entries.append({"type": "custom", "id": uuid.uuid4().hex[:8], "parentId": parent_id, "timestamp": timestamp, "customType": "harness.state", "data": {"todos": self.context.todos if self.context else [], "step_count": self.step_count}})

        sessions_dir = self._sessions_dir()
        existing = sorted(sessions_dir.glob(f"*_{sid}.jsonl"))
        path = existing[-1] if existing else sessions_dir / f"{timestamp.replace(':', '-').replace('.', '-')}_{sid}.jsonl"
        with open(path, "w", encoding="utf-8") as f:
            for entry in entries:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        logger.info(f"Session saved to: {path}")
        return sid

    def load_session(self, sid: str) -> bool:
        sessions_dir = self._sessions_dir()
        path = sessions_dir / (sid if sid.endswith(".jsonl") else f"{sid}.jsonl")
        if not path.exists():
            hits = sorted(sessions_dir.glob(f"*_{sid}.jsonl"))
            if not hits:
                return False
            path = hits[-1]
        logger.info(f"Resuming session from: {path}")
        entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        if not entries or entries[0].get("type") != "session":
            return False
        state = next((e["data"] for e in entries if e.get("type") == "custom" and isinstance(e.get("data"), dict)), {})
        self.current_session_id = str(entries[0].get("id") or sid)
        self.step_count = state.get("step_count", 0)
        self.context = Context(self._sys_prompt_with_capabilities(self.cfg.resolve_system_prompt(self.root)), self.cfg.agent)
        self.context.messages = [Message.from_agent_dict(e["message"]) for e in entries if e.get("type") == "message" and isinstance(e.get("message"), dict) and e["message"].get("role") != "system"]
        self.context.todos = state.get("todos", [])
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
        system_prompt = self._sys_prompt_with_capabilities(self.cfg.resolve_system_prompt(self.root))
        self.context = Context(system_prompt, self.cfg.agent, self.perm_manager)
        self.step_count = 0

    async def _register_mcp_tools(self):
        """Connect to every configured MCP server, discover the tools it exposes and register them."""
        tasks = [_MCPRemoteTool.discover_all(name, cfg.url) for name, cfg in self.cfg.mcp_servers.items() if cfg.enabled]
        all_results = await asyncio.gather(*tasks)
        for tools in all_results:
            for tool in tools:
                self.registry.add(tool)

    @tracer.start_as_current_span("boot")
    async def boot(self) -> None:
        await self._register_mcp_tools()
        register_all_builtins(self.registry, self.cfg, self.root)
        # Register spawn tool for subagent support
        self.registry.add(SpawnTool(self.subagent_manager, self.cfg.agent.subagents))
        if not self.cfg.providers:
            raise RuntimeError("No providers configured. Add at least one provider to config.yaml.")
        default = next((p for p in self.cfg.providers if p.default), self.cfg.providers[0])
        self.provider = Provider(default)
        system_prompt = self._sys_prompt_with_capabilities(self.cfg.resolve_system_prompt(self.root))
        self.context = Context(system_prompt, self.cfg.agent, self.perm_manager)

        import datetime

        now = datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
        system_info = f"\nSystem Info: Platform={sys.platform}, Python={sys.version.split()[0]}, Time={now}"
        self.context.add_user(f"System initialized. {system_info}")

        mcp_tools = [t.name() for t in self.registry.list() if t.name().startswith("mcp_")]
        builtin_tools = [t.name() for t in self.registry.list() if not t.name().startswith("mcp_")]

        log_box(
            "boot",
            f"Workspace: {self.root}\nBuilt-in Tools: {builtin_tools}\nMCP Tools: {mcp_tools}\nSkills: {[s.name for s in self.cfg.enabled_skills()]}\nProvider: {self.provider.entry.name}\nModel: {self.provider.entry.model}\nBase URL: {self.provider.entry.base_url}",
        )

    # ------------------------------------------------------------------
    # Plan-mode helpers
    # ------------------------------------------------------------------

    def _compose(self, text: str) -> str:
        if self._plan_mode:
            return self.cfg.plan_mode_marker + "\n\n" + text
        return text

    def _request_plan_approval(self, proposal: str) -> bool:
        _stdout("\n" + "\u2550" * 60)
        _stdout("\U0001f4cb  PLAN MODE \u2014 proposed plan:")
        _stdout("\u2550" * 60)
        _stdout(proposal)
        _stdout("\u2550" * 60)
        return True

    @tracer.start_as_current_span("run_turn")
    async def _run_turn(self, composed_input: str) -> str:
        self.context.add_user(composed_input)
        turn_assistant_contents = []  # Track assistant content independently of compaction
        max_steps = self.cfg.agent.max_steps or 10**8

        # Initialize pending queue for subagent result injection
        self._pending_queue = asyncio.Queue()
        self._last_turn_error = ""

        try:
            for _ in range(max_steps):
                # --- Drain pending subagent results before each step ---
                for inj in await self._drain_pending_queue():
                    inject_text = f"[Subagent Result (task_id={inj['task_id']}, label={inj['label']})]\n{inj['content']}"
                    self.context.add_user(inject_text)
                    log_box("mcp", f"⬅ Subagent result: [{inj['task_id']}] {inj['label']}\n{inj['content'][:300]}")

                self.step_count += 1
                logger.info(f"--- Step {self.step_count} ---")
                # Get max tokens for current provider
                assert self.provider is not None, "Controller.provider must be initialized before running a turn"
                self.context.compact(max_tokens=self.provider.entry.context_window, force=False)
                messages = self.context.to_openai()
                tools = self.registry.schemas()
                try:
                    response = await self.provider.chat(messages, tools, self.cfg.agent.temperature)
                except KeyboardInterrupt:
                    logger.warning("\nInterrupted by user during LLM call.")
                    raise
                except Exception as e:
                    sys.stdout.write(f"\n⚠️  LLM call failed after retries: {e}\n")
                    sys.stdout.flush()
                    return f"Error: LLM call failed — {e}"

                content = response.get("content", "")
                tool_calls = response.get("tool_calls", [])
                finish = response.get("finish_reason", "")
                usage_summary = response.get("usage_summary", "")

                model_parts = []
                if finish:
                    model_parts.append(f"[finish={finish}]")
                if content:
                    model_parts.append(content)
                if tool_calls:
                    tc_names = [tc.get("function", {}).get("name", "?") for tc in tool_calls]
                    model_parts.append(f"\u2192 tools: {', '.join(tc_names)}")
                    for tc in tool_calls:
                        model_parts.append(f"  └─ call: {tc.get('function', {}).get('name')}\n     args: {tc.get('function', {}).get('arguments')}")

                if model_parts:
                    log_box("model", "\n".join(model_parts))

                if usage_summary:
                    console.print(f"[dim]{usage_summary}[/dim]")

                if finish in ("error", "interrupted"):
                    if finish == "error":
                        self._last_turn_error = response.get("error") or content or "(error)"
                    return content or "(error)"

                if tool_calls:
                    self.context.add_assistant(content or "", tool_calls=tool_calls)
                    if content:
                        turn_assistant_contents.append(content)
                    for tc in tool_calls:
                        tid = tc.get("id", "unknown")
                        fn = tc.get("function", {})
                        tname = fn.get("name", "")
                        try:
                            args = json.loads(fn.get("arguments", "{}")) if isinstance(fn.get("arguments"), str) else fn.get("arguments", {})
                        except json.JSONDecodeError:
                            args = {}
                        result = await self.registry.execute_gated(tname, self, args)
                        self.context.add_tool_result(tname, tid, result)

                        def format_args(args_dict):
                            try:

                                def truncate(v):
                                    if isinstance(v, str) and len(v) > 50:
                                        return v[:47] + "..."
                                    if isinstance(v, dict):
                                        return {k: truncate(val) for k, val in v.items()}
                                    if isinstance(v, list):
                                        return [truncate(val) for val in v]
                                    return v

                                truncated = truncate(args_dict)
                                s = json.dumps(truncated, ensure_ascii=False)
                                return s[:200] + "..." if len(s) > 200 else s
                            except:
                                return str(args_dict)[:200]

                        call_str = f"call: {tname}\nargs: {format_args(args)}"
                        res_str = f"result:\n{result[:600]}"
                        log_box("tool", f"{call_str}\n{'-' * 36}\n{res_str}")
                else:
                    # No tool calls — if subagents still running, discard this
                    # transitional reply and wait for results before re-looping
                    if self.subagent_manager.get_running_count() > 0:
                        _stdout("⏳ Waiting for running subagents to complete...")
                        while self.subagent_manager.get_running_count() > 0:
                            try:
                                msg = await asyncio.wait_for(self._pending_queue.get(), timeout=300)
                            except asyncio.TimeoutError:
                                _stdout("⚠️  Timeout waiting for subagents.")
                                break
                            self.context.add_user(f"[Subagent Result (task_id={msg['task_id']}, label={msg['label']})]\n{msg['content']}")
                            log_box("mcp", f"⬅ Subagent result: [{msg['task_id']}] {msg['label']}\n{msg['content'][:300]}")
                        continue
                    # Only return if model signaled finish=stop; otherwise continue requesting
                    if finish == "stop":
                        self.context.add_assistant(content or "")
                        if content:
                            turn_assistant_contents.append(content)
                        # Warn user if todos are incomplete
                        incomplete = [t for t in self.context.todos if t.get("status") in ("pending", "in_progress")]
                        if incomplete:
                            _stdout(f"⚠️  Model stopped with {len(incomplete)} incomplete todo items remaining.")
                        return "\n\n".join(turn_assistant_contents) if turn_assistant_contents else content or "(no output)"
                    # Model didn't finish — add to context and continue loop
                    self.context.add_assistant(content or "")
                    if content:
                        turn_assistant_contents.append(content)
                    continue
                if finish == "stop":
                    self.context.add_assistant(content or "")
                    if content:
                        turn_assistant_contents.append(content)
                    # Warn user if todos are incomplete
                    incomplete = [t for t in self.context.todos if t.get("status") in ("pending", "in_progress")]
                    if incomplete:
                        _stdout(f"⚠️  Model stopped with {len(incomplete)} incomplete todo items remaining.")
                    return "\n\n".join(turn_assistant_contents) if turn_assistant_contents else content or "(no output)"
        except KeyboardInterrupt:
            logger.warning("\nInterrupted by user. Resuming...")
            await self.subagent_manager.cancel_all()
            return "(Interrupted by user)"
        finally:
            self._pending_queue = None
        return "\n\n".join(turn_assistant_contents) if turn_assistant_contents else "(max_steps reached)"

    async def _drain_pending_queue(self, limit: int = 5) -> List[dict]:
        """Drain completed subagent results from the pending queue (non-blocking)."""
        if self._pending_queue is None:
            return []
        items = []
        while len(items) < limit:
            try:
                items.append(self._pending_queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return items

    @tracer.start_as_current_span("agent_run")
    async def run(self, user_request: str) -> str:
        if self.context is None:
            await self.boot()

        log_box("user", user_request[:500])
        composed = self._compose(user_request)

        if not self._plan_mode:
            return await self._run_turn(composed)

        # ── Plan mode: research / planning turn ───────────────────────────────
        proposal = await self._run_turn(composed)

        if not proposal or not proposal.strip():
            return "(plan mode: no proposal generated)"

        approved = self._request_plan_approval(proposal)
        if not approved:
            return "Plan rejected. Plan mode is still active. Send revised instructions."

        # ── Approved: exit plan mode and execute ──────────────────────────────
        _stdout("\n✅ Plan approved — executing...")
        pending = getattr(self.context, "pending_writes", [])
        if pending:
            _stdout(f"\n⚙ Executing {len(pending)} queued write operations...")
            for tool, args in pending:
                _stdout(f"  Running {tool.name()} on {args.get('path', 'unknown')}...")
                await tool.execute(self, args)

        self.set_plan_mode(False)

        todos = parse_plan_todos(proposal)
        if todos and self.context is not None:
            self.context.todos = todos
            todo_log = "\n".join(f"  [{t['status']}] {t['content']}" for t in todos)
            log_box("tool", f"todo_write (plan seed)\n{todo_log}")

        # Execution turn with plan-approved nudge (mirrors planApprovedMessage turn).
        return await self._run_turn(self.cfg.plan_approved_message)


# =============================================================================
# 7. Feishu Gateway (WebSocket long connection — no public IP required)
# =============================================================================

_UNSUPPORTED_MESSAGE = "(unsupported message type: {kind}) Please send a text message instead."

_SURFACE_REPL = "repl"
_SURFACE_CHAT = "chat"


class _CommandSpec(NamedTuple):
    """One catalogue entry: help wording, available surfaces and its handler."""

    usage: str  # e.g. "/model <name/idx>"; the dispatchable names are parsed from it
    description: str
    surfaces: Tuple[str, ...]
    handler: Callable[["Controller", str, str], str]  # (controller, arg, surface) -> text
    chat_usage: str = ""  # optional chat-only wording override
    chat_desc: str = ""
    takes_arg: bool = False  # True when the command accepts "<name> <arg>"


# --- Handlers: one per command, shared by both surfaces ----------------------


def _cmd_help(ctrl: "Controller", arg: str, surface: str) -> str:
    if surface == _SURFACE_CHAT:
        return _CHAT_COMMANDS_HELP
    print_help()
    return ""


def _cmd_exit(ctrl: "Controller", arg: str, surface: str) -> str:
    if surface == _SURFACE_CHAT:
        return "Not available in chat: the gateway keeps running. Send /new to start a fresh context."
    raise StopIteration  # the REPL loop reads this as "exit"


def _cmd_new(ctrl: "Controller", arg: str, surface: str) -> str:
    return _cmd_new_text(ctrl)


def _cmd_model(ctrl: "Controller", arg: str, surface: str) -> str:
    return _cmd_model_text(ctrl, arg)


def _cmd_context(ctrl: "Controller", arg: str, surface: str) -> str:
    if surface == _SURFACE_CHAT:
        return _context_summary_text(ctrl)  # the full payload is far too long for a chat
    return _cmd_context_text(ctrl)


def _cmd_history(ctrl: "Controller", arg: str, surface: str) -> str:
    if not ctrl.context:
        return "Context is empty."
    lines = ["--- Interaction History ---"]
    lines += [f"[{m.role.value.upper()}] {m.content}" for m in ctrl.context.messages]
    return "\n".join(lines)


def _cmd_plan(ctrl: "Controller", arg: str, surface: str) -> str:
    sub = arg.strip().lower() or None
    if sub is None:
        ctrl.set_plan_mode(not ctrl.plan_mode)
    elif sub in ("on", "enable", "true", "1"):
        ctrl.set_plan_mode(True)
    elif sub in ("off", "disable", "false", "0"):
        ctrl.set_plan_mode(False)
    elif sub == "status":
        return f"Plan mode: {'ON' if ctrl.plan_mode else 'OFF'}"
    else:
        return f"Unknown plan mode argument: {sub}. Use /plan [on/off/status]"
    return ""


def _cmd_skills(ctrl: "Controller", arg: str, surface: str) -> str:
    return _cmd_skills_text(ctrl)


def _cmd_tools(ctrl: "Controller", arg: str, surface: str) -> str:
    return _cmd_tools_text(ctrl)


def _cmd_mcp(ctrl: "Controller", arg: str, surface: str) -> str:
    mcp_tools = [t for t in ctrl.registry.list() if t.name().startswith("mcp_")]
    if not mcp_tools:
        return "No MCP tools found."
    return "Available MCP tools:\n" + "\n".join([f"  {t.name()}: {t.description()}" for t in mcp_tools])


def _cmd_info(ctrl: "Controller", arg: str, surface: str) -> str:
    # NOTE: never call ctrl.boot() here — it rebuilds the Context and would silently
    # discard the current conversation.
    if surface == _SURFACE_CHAT:
        return _context_summary_text(ctrl)
    _stdout(_context_summary_text(ctrl))
    print_help()
    return ""


def _cmd_gateway(ctrl: "Controller", arg: str, surface: str) -> str:
    if surface == _SURFACE_CHAT:
        return "This chat is already served by the Feishu gateway."
    run_feishu_gateway()
    return ""


_COMMAND_SPECS: List[_CommandSpec] = [
    _CommandSpec("/help, ?", "Show this help", (_SURFACE_REPL, _SURFACE_CHAT), _cmd_help),
    _CommandSpec("/new", "Start a new conversation (automatically saves current session)", (_SURFACE_REPL, _SURFACE_CHAT), _cmd_new),
    _CommandSpec("/clear", "Same as /new, clears conversation context", (_SURFACE_REPL,), _cmd_new),
    _CommandSpec("/model", "List all available providers", (_SURFACE_REPL, _SURFACE_CHAT), _cmd_model, "", "List providers; /model <name|index> switches this chat only"),
    _CommandSpec("/model <name/idx>", "Switch to the specified provider", (_SURFACE_REPL,), _cmd_model, takes_arg=True),
    _CommandSpec("/context", "Display current context and LLM request payload", (_SURFACE_REPL, _SURFACE_CHAT), _cmd_context, "", "Short context summary (messages, tokens, todos, tools)"),
    _CommandSpec("/history", "Display conversation history", (_SURFACE_REPL,), _cmd_history),
    _CommandSpec("/plan", "Enable plan mode (next request is planned before execution)", (_SURFACE_REPL,), _cmd_plan),
    _CommandSpec("/plan on/off", "Enable or disable plan mode", (_SURFACE_REPL,), _cmd_plan, takes_arg=True),
    _CommandSpec("/plan status", "Check current plan mode status", (_SURFACE_REPL,), _cmd_plan, takes_arg=True),
    _CommandSpec("/skills", "List available skills", (_SURFACE_REPL, _SURFACE_CHAT), _cmd_skills, "", "List available skills (trigger one by sending /<skill>)"),
    _CommandSpec("/tools", "List available tools (built-in and MCP)", (_SURFACE_REPL, _SURFACE_CHAT), _cmd_tools, "", "List registered built-in tools"),
    _CommandSpec("/mcp", "List all connected MCP servers and their tools", (_SURFACE_REPL,), _cmd_mcp),
    _CommandSpec("/info", "Display system status and help", (_SURFACE_REPL,), _cmd_info),
    _CommandSpec("/gateway", "Start the Feishu/Lark gateway (WebSocket long connection)", (_SURFACE_REPL,), _cmd_gateway),
    _CommandSpec("/exit, /quit", "Exit (automatically saves session)", (_SURFACE_REPL,), _cmd_exit),
    _CommandSpec("q", "Exit (same as /quit)", (_SURFACE_REPL,), _cmd_exit),
]

# Extra names that are accepted but intentionally left out of the help table.
_COMMAND_ALIASES: Dict[str, str] = {"gateway": "/gateway"}

_HELP_COLUMN = 18


def _render_command_table(surface: str, min_width: int = _HELP_COLUMN) -> str:
    """Render aligned `usage  description` lines for one surface."""
    rows: List[Tuple[str, str]] = []
    for spec in _COMMAND_SPECS:
        if surface not in spec.surfaces:
            continue
        usage, desc = spec.usage, spec.description
        if surface == _SURFACE_CHAT:
            usage, desc = spec.chat_usage or usage, spec.chat_desc or desc
        rows.append((usage, desc))
    width = max([len(u) for u, _ in rows] + [min_width])
    return "\n".join(f"  {u:<{width}}{d}" for u, d in rows)


def _command_names(usage: str) -> List[str]:
    """Dispatchable names of a catalogue row: "/help, ?" -> ["/help", "?"]."""
    names: List[str] = []
    for token in usage.split():
        token = token.rstrip(",")
        if not token or token.startswith("<") or "/" in token[1:]:
            continue
        if token.startswith("/") or token == "?" or (len(token) == 1 and token.isalpha()):
            names.append(token)
    return names


def _surface_commands(surface: str) -> Dict[str, "_CommandSpec"]:
    """name -> spec for one surface, derived from the same catalogue the help renders."""
    commands: Dict[str, "_CommandSpec"] = {}
    for spec in _COMMAND_SPECS:
        if surface not in spec.surfaces:
            continue
        for name in _command_names(spec.usage):
            commands.setdefault(name, spec)
    for alias, target in _COMMAND_ALIASES.items():
        if target in commands:
            commands.setdefault(alias, commands[target])
    return commands


def lookup_command(req: str, surface: str) -> Optional[Tuple["_CommandSpec", str]]:
    commands = _surface_commands(surface)
    head = req.split(None, 1)[0] if req.split() else ""
    for key in (req, head):
        if key in commands:
            return commands[key], key
    return None


def _command_arg(req: str) -> str:
    """Text after the command name: "/model deepseek" -> "deepseek"."""
    parts = req.split(None, 1)
    return parts[1].strip() if len(parts) > 1 else ""


def _cmd_new_text(ctrl: "Controller") -> str:
    """Save the current session, then start a fresh context."""
    sid = ctrl.save_session()
    ctrl.reset_context()
    return f"New context started. Previous session: --resume {sid}"


def _cmd_model_text(ctrl: "Controller", arg: str = "") -> str:
    """List the configured providers, or switch the controller to one of them."""
    if not arg.strip():
        return "\n".join(ctrl.list_providers())
    return ctrl.switch_provider(arg.strip())


def _cmd_context_text(ctrl: "Controller") -> str:
    """Full simulated LLM request payload (REPL only: far too long for a chat)."""
    if not ctrl.context:
        return "Context is empty."
    payload = {"model": ctrl.provider.entry.model if ctrl.provider else "default", "messages": ctrl.context.to_openai(), "tools": ctrl.registry.schemas(), "temperature": ctrl.cfg.agent.temperature, "todos": ctrl.context.todos}
    return json.dumps(payload, indent=2, ensure_ascii=False)


def _context_summary_text(ctrl: "Controller") -> str:
    """Short context summary, small enough to send as a chat message."""
    if not ctrl.context:
        return "Context is empty."
    entry = getattr(getattr(ctrl, "provider", None), "entry", None)
    name = getattr(entry, "name", "") or ""
    model = getattr(entry, "model", "") or "default"
    todos = ctrl.context.todos
    open_todos = [t for t in todos if t.get("status") in ("pending", "in_progress")]
    return f"Model: {name + '/' if name else ''}{model}\n" f"Messages: {len(ctrl.context.messages)}\n" f"Estimated tokens: {ctrl.context.estimate_tokens()}\n" f"Steps this session: {ctrl.step_count}\n" f"Todos: {len(todos)} ({len(open_todos)} open)\n" f"Tools: {len(ctrl.registry.list())}"


def _cmd_skills_text(ctrl: "Controller") -> str:
    skills = sorted(ctrl.cfg.enabled_skills(), key=lambda s: s.name)
    return "Available skills:\n" + "\n".join([f"/{s.name} — {s.description[:47] + '...' if len(s.description) > 50 else s.description}" for s in skills])


def _cmd_tools_text(ctrl: "Controller") -> str:
    builtins = [t for t in ctrl.registry.list() if not t.name().startswith("mcp_")]
    return "Available built-in tools:\n" + "\n".join([f"  {t.name()}" for t in builtins])


def _chat_commands_help() -> str:
    """The `/help` reply sent back into a chat (subset of the REPL commands)."""
    return f"Harness chat commands:\n{_render_command_table(_SURFACE_CHAT)}\n\nAny other text is sent to the model."


# Derived once at import time; the catalogue above is static.
_CHAT_COMMANDS_HELP = _chat_commands_help()


def _unknown_command_text(text: str) -> str:
    return f"Unknown command: {text.strip()}\n\n{_CHAT_COMMANDS_HELP}"


class _FeishuAskTool(AskTool):
    """`ask` in the gateway: post the question to the chat and wait for the reply."""

    def __init__(self, gateway: "FeishuGateway"):
        self._gateway = gateway

    async def __call__(self, ctx: Any, args: dict) -> str:
        return await self._gateway.ask(ctx, args.get("question", ""), list(args.get("options") or []))


class FeishuGateway:
    """Serve the agent over Feishu/Lark: long-connection events in, chat replies out.

    One workspace Controller is booted once; every chat then gets its own
    lightweight Controller (shared registry/provider/subagents, private context)
    so conversations stay isolated and are persisted per chat.
    """

    MAX_SEEN = 1000
    CHUNK_LIMIT = 4000

    def __init__(self, root: str = ".") -> None:
        self.root = os.path.abspath(root)
        self.cfg = Config.load_for_root(self.root)
        self.feishu: FeishuConfig = self.cfg.feishu
        self.base: Optional[Controller] = None
        self._client: Any = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._bot_open_id: str = ""
        self._sem: Optional[asyncio.Semaphore] = None
        self._chats: "OrderedDict[str, Controller]" = OrderedDict()
        self._locks: Dict[str, asyncio.Lock] = {}
        self._pending_asks: Dict[str, asyncio.Future] = {}
        self._seen: deque = deque()
        self._seen_set: set = set()

    async def run(self) -> None:
        app_id, app_secret = self.feishu.credentials
        if not app_id or not app_secret:
            _stdout("Feishu gateway is not configured.\nSet feishu.app_id / feishu.app_secret in config.yaml, or export FEISHU_APP_ID / FEISHU_APP_SECRET.")
            return
        try:
            import lark_oapi as lark
            import lark_oapi.ws.client  # noqa: F401  (the SDK keeps its event loop module-global)
            from lark_oapi.core.const import FEISHU_DOMAIN, LARK_DOMAIN
        except ImportError:
            _stdout("Feishu gateway requires the official SDK:\n  pip install lark-oapi")
            return

        self._loop = asyncio.get_running_loop()
        self._sem = asyncio.Semaphore(max(1, self.feishu.max_concurrent_turns))
        domain = LARK_DOMAIN if self.feishu.domain.strip().lower() == "lark" else FEISHU_DOMAIN
        self._client = lark.Client.builder().app_id(app_id).app_secret(app_secret).domain(domain).log_level(lark.LogLevel.INFO).build()

        self.base = Controller(workspace_root=self.root)
        await self.base.boot()
        self.base.registry.add(_FeishuAskTool(self))  # shared with every chat controller

        self._bot_open_id = await self._loop.run_in_executor(None, self._fetch_bot_open_id)
        if not self._bot_open_id:
            logger.warning("Feishu: bot open_id unavailable; @mention detection falls back to 'any mention'")

        handler = lark.EventDispatcherHandler.builder(self.feishu.encrypt_key or "", self.feishu.verification_token or "").register_p2_im_message_receive_v1(self._on_message_sync).build()
        ws_client = lark.ws.Client(app_id, app_secret, domain=domain, event_handler=handler, log_level=lark.LogLevel.INFO)
        self._start_ws_thread(ws_client)

        log_box(
            "boot",
            f"Feishu gateway online\nWorkspace: {self.root}\nDomain: {self.feishu.domain}\nGroup policy: {self.feishu.group_policy}\nAllow from: {self.feishu.allow_from or '[all]'}\nBot open_id: {self._bot_open_id or '(unknown)'}",
        )
        try:
            await asyncio.Event().wait()  # serve until the process is interrupted
        finally:
            await close_http_pool()

    def _start_ws_thread(self, ws_client: Any) -> None:
        def run() -> None:
            import lark_oapi.ws.client as lark_ws_client

            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            lark_ws_client.loop = loop
            try:
                ws_client.start()
            except Exception as e:
                logger.error(f"Feishu WebSocket terminated: {e}")

        threading.Thread(target=run, name="feishu-ws", daemon=True).start()

    def _fetch_bot_open_id(self) -> str:
        """GET /open-apis/bot/v3/info — needed for reliable @mention matching."""
        try:
            import lark_oapi as lark

            request = lark.BaseRequest.builder().http_method(lark.HttpMethod.GET).uri("/open-apis/bot/v3/info").token_types({lark.AccessTokenType.APP}).build()
            response = self._client.request(request)
            if not response.success():
                logger.warning(f"Feishu bot info failed: code={response.code} msg={response.msg}")
                return ""
            raw = getattr(getattr(response, "raw", None), "content", b"") or b""
            data = json.loads(raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw)
            return str((data.get("bot") or {}).get("open_id") or "")
        except Exception as e:
            logger.warning(f"Feishu bot info unavailable: {e}")
            return ""

    # ------------------------------------------------------------------
    # Inbound events (called from the WebSocket thread)
    # ------------------------------------------------------------------

    def _on_message_sync(self, data: Any) -> None:
        try:
            info = self._event_info(data)
        except Exception as e:
            logger.warning(f"Feishu event parse failed: {e}")
            return
        if info is None:
            return
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        asyncio.run_coroutine_threadsafe(self._handle_message(info), loop)

    def _event_info(self, data: Any) -> Optional[dict]:
        event = getattr(data, "event", None)
        message = getattr(event, "message", None)
        if message is None:
            return None
        sender = getattr(event, "sender", None)
        mentions = list(getattr(message, "mentions", None) or [])
        mention_ids = {getattr(getattr(m, "id", None), "open_id", "") or "" for m in mentions}
        message_type = getattr(message, "message_type", "") or ""
        text = self._extract_text(message_type, getattr(message, "content", "") or "")
        for mention in mentions:
            key = getattr(mention, "key", "") or ""
            if key:
                text = text.replace(key, "")
        mentioned = self._bot_open_id in mention_ids if self._bot_open_id else bool(mentions)
        return {"message_id": getattr(message, "message_id", "") or "", "chat_id": getattr(message, "chat_id", "") or "", "chat_type": getattr(message, "chat_type", "") or "", "message_type": message_type, "text": text.strip(), "open_id": getattr(getattr(sender, "sender_id", None), "open_id", "") or "", "mentioned": mentioned}

    @staticmethod
    def _extract_text(message_type: str, content: str) -> str:
        """Return the user-visible text of a message, or '' for unsupported types."""
        try:
            payload = json.loads(content or "{}")
        except (json.JSONDecodeError, TypeError):
            return ""
        if not isinstance(payload, dict):
            return ""
        if message_type == "text":
            return str(payload.get("text", ""))
        if message_type == "post":
            parts = [str(payload.get("title", ""))]
            for line in payload.get("content", []) or []:
                row = "".join(str(el.get("text", "")) for el in line if isinstance(el, dict))
                if row.strip():
                    parts.append(row)
            return "\n".join(p for p in parts if p).strip()
        return ""

    async def _handle_message(self, info: dict) -> None:
        message_id = info["message_id"]
        if not message_id or message_id in self._seen_set:
            return
        while len(self._seen) >= self.MAX_SEEN:
            self._seen_set.discard(self._seen.popleft())
        self._seen.append(message_id)
        self._seen_set.add(message_id)

        if self.feishu.allow_from and info["open_id"] not in self.feishu.allow_from:
            logger.warning(f"Feishu: dropped message from non-allowlisted sender {info['open_id'] or '(unknown)'}")
            return

        pending = self._pending_asks.get(info["chat_id"])
        if info["text"] and pending is not None and not pending.done() and not info["text"].lstrip().startswith("/"):
            logger.info(f"Feishu: answer for chat {info['chat_id']}: {info['text'][:200]}")
            pending.set_result(info["text"])
            return
        if info["chat_type"] == "group" and self.feishu.group_policy.strip().lower() != "all" and not info["mentioned"]:
            return
        if not info["text"]:
            if info["message_type"] not in ("text", "post"):
                await self._send(info["chat_id"], _UNSUPPORTED_MESSAGE.format(kind=info["message_type"] or "unknown"))
            return

        log_box("user", f"[feishu {info['chat_type']}] {info['open_id']}\n{info['text'][:500]}")
        try:
            answer = await self._answer(info["chat_id"], info["text"])
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("Feishu turn failed")
            answer = f"⚠️ run failed: {e}"
        if answer and answer.strip():
            await self._send(info["chat_id"], answer, message_id if self.feishu.reply_to_message else "")

    async def _answer(self, chat_id: str, text: str) -> str:
        assert self._sem is not None, "FeishuGateway.run() must be awaited before handling messages"
        async with self._sem:
            lock = self._locks.setdefault(chat_id, asyncio.Lock())
            async with lock:
                controller = self._controller_for(chat_id)
                if text.lstrip().startswith("/"):
                    reply = self._run_command(controller, text)
                    if reply is not None:
                        logger.info(f"Feishu: command for chat {chat_id}: {text.strip()}")
                        return reply
                    # Not a command: fall back to a skill trigger, exactly like the REPL.
                    skill_name, *skill_args = text.strip()[1:].split()
                    skill = controller.cfg.get_skill(skill_name) if skill_name else None
                    if skill is None:
                        return _unknown_command_text(text)
                    controller.context.add_user(f"Execute skill {skill_name} with args: {' '.join(skill_args)}\n\nSkill directory: {skill.path}\n\nSkill definition:\n{skill.body}")
                    text = "Proceed with this skill execution"
                answer = await controller.run(text)
                controller.save_session()
                error = getattr(controller, "_last_turn_error", "")
                if error:
                    entry = getattr(getattr(controller, "provider", None), "entry", None)
                    provider_name = getattr(entry, "name", "") or ""
                    model = getattr(entry, "model", "") or ""
                    logger.error(f"Feishu: turn error for chat {chat_id} ({provider_name}/{model}): {error}")
                    return self._friendly_error(error, provider_name, model)
                return answer

    @staticmethod
    def _friendly_error(error: str, provider_name: str = "", model: str = "") -> str:
        """Short, chat-safe failure notice; the full exception stays in the log."""
        low = (error or "").lower()
        if "timed out" in low or "timeout" in low:
            reason = "connection/response timeout"
        elif "rate limit" in low:
            reason = "rate limit exceeded"
        elif "authentication" in low or "api key" in low or "401" in low:
            reason = "authentication failed"
        elif "unavailable" in low or "overloaded" in low or "503" in low:
            reason = "service temporarily unavailable"
        else:
            reason = "unexpected error"
        target = "/".join(p for p in (provider_name, model) if p) or "current model"
        return f"⚠️ Model call failed: {reason} ({target}). The full error is in the log; please retry later."

    def _run_command(self, controller: Controller, text: str) -> Optional[str]:
        req = text.strip()
        found = lookup_command(req, _SURFACE_CHAT)
        if found is None:
            other = lookup_command(req, _SURFACE_REPL)
            if other is not None:
                return f"{other[1]} is only available in the terminal REPL, not in chat."
            return None
        spec, _name = found
        return spec.handler(controller, _command_arg(req), _SURFACE_CHAT)

    async def ask(self, controller: Controller, question: str, options: List[str]) -> str:
        """Post the question into the chat and suspend the turn until the user answers."""
        chat_id = getattr(controller, "chat_id", "") or getattr(getattr(controller, "context", None), "chat_id", "")
        if not chat_id:
            return "<model-assumption> No chat to ask in: pick the safest default, state the assumption, and continue."
        lines = [question] + [f"{i}. {o}" for i, o in enumerate(options, 1)]
        await self._send(chat_id, "\n".join(lines))
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending_asks[chat_id] = future
        try:
            answer = await asyncio.wait_for(future, timeout=max(1, self.feishu.ask_timeout_seconds))
        except asyncio.TimeoutError:
            return f"<model-assumption> No answer within {self.feishu.ask_timeout_seconds}s: pick the safest default, state the assumption, and continue."
        finally:
            self._pending_asks.pop(chat_id, None)
        return self._match_option(answer, options)

    @staticmethod
    def _match_option(answer: str, options: List[str]) -> str:
        """Turn "2" (or the option text) into the chosen option; anything else is a free answer."""
        text = (answer or "").strip()
        if text.isdigit() and 1 <= int(text) <= len(options):
            return options[int(text) - 1]
        for option in options:
            if text.lower() == option.strip().lower():
                return option
        return text

    def _controller_for(self, chat_id: str) -> Controller:
        """Return (or create) the chat's own Controller, sharing the booted registry."""
        assert self.base is not None, "FeishuGateway.run() must be awaited before handling messages"
        controller = self._chats.get(chat_id)
        if controller is not None:
            self._chats.move_to_end(chat_id)
            return controller

        controller = Controller(workspace_root=self.root)
        controller.cfg = self.base.cfg
        controller.registry = self.base.registry
        controller.perm_manager = self.base.perm_manager
        controller.subagent_manager = self.base.subagent_manager
        controller.provider = self.base.provider
        controller._plan_mode = False
        controller.chat_id = chat_id  # lets tools (e.g. `ask`) reply into this conversation
        # The session id is derived from the chat id, so an existing session log can be
        # found again after a restart without keeping any extra chat -> session state.
        controller.current_session_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"feishu:{chat_id}"))
        if not controller.load_session(controller.current_session_id):
            controller.reset_context()
        controller.context.chat_id = chat_id  # PermissionManager asks through the Context
        self._chats[chat_id] = controller

        while len(self._chats) > max(1, self.feishu.max_chats):
            self._chats.popitem(last=False)  # already saved after its last turn
        return controller

    # ------------------------------------------------------------------
    # Outbound replies
    # ------------------------------------------------------------------

    async def _send(self, chat_id: str, text: str, reply_to: str = "") -> None:
        first = True
        for chunk in self._chunks(text):
            await asyncio.get_running_loop().run_in_executor(None, self._send_chunk_sync, chat_id, chunk, reply_to if first else "")
            first = False

    def _send_chunk_sync(self, chat_id: str, text: str, reply_to: str = "") -> None:
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody, ReplyMessageRequest, ReplyMessageRequestBody

        body = json.dumps({"text": text}, ensure_ascii=False)
        try:
            if reply_to:
                request = ReplyMessageRequest.builder().message_id(reply_to).request_body(ReplyMessageRequestBody.builder().msg_type("text").content(body).build()).build()
                response = self._client.im.v1.message.reply(request)
                if response.success():
                    return
                logger.warning(f"Feishu reply failed ({response.code}: {response.msg}); falling back to chat message")
            request = CreateMessageRequest.builder().receive_id_type("chat_id").request_body(CreateMessageRequestBody.builder().receive_id(chat_id).msg_type("text").content(body).build()).build()
            response = self._client.im.v1.message.create(request)
            if not response.success():
                logger.warning(f"Feishu send failed ({response.code}: {response.msg})")
        except Exception as e:
            logger.error(f"Feishu send error: {e}")

    @classmethod
    def _chunks(cls, text: str) -> List[str]:
        text = (text or "").strip()
        chunks: List[str] = []
        while len(text) > cls.CHUNK_LIMIT:
            cut = text.rfind("\n", 0, cls.CHUNK_LIMIT)
            if cut <= 0:
                cut = cls.CHUNK_LIMIT
            chunks.append(text[:cut])
            text = text[cut:].lstrip("\n")
        if text or not chunks:
            chunks.append(text)
        return chunks


# =============================================================================
# 8. CLI Entry Point
# =============================================================================


async def _run_and_cleanup(ctrl, req: str) -> str:
    try:
        return await ctrl.run(req)
    finally:
        await close_http_pool()


def _read_input_auto(timeout: float = 0.08) -> str:
    """Read user input using prompt_toolkit. Supports multiline via Alt+Enter."""
    from prompt_toolkit import PromptSession
    from prompt_toolkit.key_binding import KeyBindings

    if not hasattr(_read_input_auto, "_session"):
        bindings = KeyBindings()

        @bindings.add("escape", "enter")
        def _newline(event):
            event.current_buffer.insert_text("\n")

        _read_input_auto._session = PromptSession(key_bindings=bindings, multiline=False)  # Enter submits; Alt+Enter for newline

    if not sys.stdin.isatty():
        # Non-interactive fallback
        line = sys.stdin.readline()
        if not line:
            raise EOFError()
        return line.rstrip("\n")

    session = _read_input_auto._session
    result = session.prompt("▶ ")
    return result


def run_feishu_gateway() -> None:
    """Serve the agent over Feishu/Lark — everything comes from the `feishu:` config block."""
    try:
        asyncio.run(FeishuGateway().run())
    except KeyboardInterrupt:
        _stdout("\nGateway stopped.")


def print_help() -> None:
    """Print the REPL help. The command list lives in `_COMMAND_SPECS`."""
    help_text = f"""
SYNOPSIS
  Harness Kernel [options] [request]

COMMANDS
{_render_command_table(_SURFACE_REPL)}

INTERRUPTS
  Ctrl-C            Cancel the current operation
  Ctrl-D            Exit the interactive session
"""
    log_box("help", help_text)


def main(argv=None) -> None:
    import argparse

    _load_dotenv()

    parser = argparse.ArgumentParser(description="Harness Kernel (Python)")
    parser.add_argument("--root", default=".", help="Workspace root")
    parser.add_argument("--model", default="", help="Override default model")
    parser.add_argument("--resume", nargs="?", const="AUTO_RESUME", default=None, help="Resume session ID")
    parser.add_argument("request", nargs="?", help="User request to execute")
    args = parser.parse_args(argv)
    ctrl = Controller(workspace_root=args.root)

    sid_to_load = None
    if args.resume == "AUTO_RESUME":
        sessions_dir = ctrl._sessions_dir()
        files = list(sessions_dir.glob("*.jsonl"))
        if files:
            latest_file = max(files, key=os.path.getmtime)
            sid_to_load = latest_file.stem
            logger.info(f"Auto-resuming latest session: {sid_to_load}")
        else:
            logger.warning("No sessions found to auto-resume.")
    elif args.resume:
        sid_to_load = args.resume

    if sid_to_load:
        register_all_builtins(ctrl.registry, ctrl.cfg, ctrl.root)
        asyncio.run(ctrl._register_mcp_tools())
        ctrl.registry.add(SpawnTool(ctrl.subagent_manager, ctrl.cfg.agent.subagents))
        if ctrl.load_session(sid_to_load):
            if not ctrl.cfg.providers:
                raise RuntimeError("No providers configured. Add at least one provider to config.yaml.")
            default = next((p for p in ctrl.cfg.providers if p.default), ctrl.cfg.providers[0])
            ctrl.provider = Provider(default)
            log_box("boot", f"Resumed session: {sid_to_load}\nWorkspace: {ctrl.root}\nMessages: {len(ctrl.context.messages)}")
        else:
            logger.info(f"Session '{sid_to_load}' not found. Starting fresh.")
            asyncio.run(ctrl.boot())
    else:
        asyncio.run(ctrl.boot())

    initial_req = args.request
    if not initial_req:
        print_help()
    one_shot = bool(args.request)

    # --- Command dispatch tables (generated from `_COMMAND_SPECS`) -------------
    # Both surfaces share one catalogue, so the REPL and the Feishu gateway expose
    # exactly the same commands; each spec's `surfaces` decides where it is offered.
    def _repl_command(spec: _CommandSpec):
        def run(req: str) -> None:
            text = spec.handler(ctrl, _command_arg(req), _SURFACE_REPL)
            if text:
                _stdout(text)

        return run

    COMMANDS = {name: _repl_command(spec) for name, spec in _surface_commands(_SURFACE_REPL).items()}
    # Commands that take an argument ("/model <name/idx>", "/plan on/off") also match
    # when the user types the argument right after the name.
    PREFIX_COMMANDS = [(name, _repl_command(spec)) for name, spec in _surface_commands(_SURFACE_REPL).items() if spec.takes_arg]

    while True:
        try:
            if initial_req:
                req = initial_req
                initial_req = None
                _stdout(f"\n=== Result ===")
            else:
                req = _read_input_auto()
                if not req:
                    continue
        except (EOFError, KeyboardInterrupt):
            _stdout("")
            break
        req = req.strip()
        if not req:
            if one_shot:
                break
            continue

        # Dispatch system commands
        handled = False
        stop = False
        if req.startswith("/"):
            # Check exact match
            if req in COMMANDS:
                try:
                    COMMANDS[req](req)
                    handled = True
                except StopIteration:
                    break
            else:
                # Check prefix match
                for prefix, func in PREFIX_COMMANDS:
                    if req.startswith(prefix):
                        func(req)
                        handled = True
                        break

            if not handled:
                _stdout(f"Unknown command: '{req}'.\nNote: Any input starting with '/' is interpreted as a system command. Please check your spelling or use a valid command.")
                continue
        elif req in COMMANDS:
            try:
                COMMANDS[req](req)
            except StopIteration:
                break
            handled = True
        else:
            for prefix, handler in PREFIX_COMMANDS:
                if req == prefix or req.startswith(prefix + " "):
                    try:
                        handler(req)
                    except StopIteration:
                        stop = True
                    handled = True
                    break
            if not handled and req.startswith("/") and ctrl.cfg.get_skill(req[1:].split()[0]):
                skill_name, *skill_args = req[1:].split()
                skill = ctrl.cfg.get_skill(skill_name)
                _stdout(f"Triggering skill: {skill_name} with args: {skill_args}")
                ctrl.context.add_user(f"Execute skill {skill_name} with args: {' '.join(skill_args)}\n\nSkill directory: {skill.path}\n\nSkill definition:\n{skill.body}")
                _stdout("")
                rich_print(asyncio.run(_run_and_cleanup(ctrl, "Proceed with this skill execution")))
                _stdout("")
                handled = True
        if stop:
            break

        if handled:
            if one_shot:
                break
            continue

        # Default: send to LLM
        try:
            _stdout("")
            rich_print(asyncio.run(_run_and_cleanup(ctrl, req)))
            _stdout("")
            if one_shot:
                break
        except KeyboardInterrupt:
            _stdout("\n⚠️  Cancelled (Ctrl+C). Resuming...")
            continue

    sid = ctrl.save_session()
    _stdout(f"\nSession saved. Resume with: --resume {sid}")


# Rebuild models to resolve forward references
Config.model_rebuild()
AgentConfig.model_rebuild()
SubagentProfileConfig.model_rebuild()
ToolsConfig.model_rebuild()

SandboxConfig.model_rebuild()
ShellConfig.model_rebuild()
ProviderEntry.model_rebuild()
SkillEntry.model_rebuild()

if __name__ == "__main__":
    import sys

    _load_dotenv()

    if "ipykernel" not in sys.argv[0]:
        main(sys.argv[1:])
