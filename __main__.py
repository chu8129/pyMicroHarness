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
import subprocess
import signal
import asyncio

if sys.platform != "win32":
    import readline

import urllib.request
import yaml
from loguru import logger
from abc import ABC, abstractmethod
from typing import Any, Optional, List, Dict
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


class Config(BaseModel):
    """Root configuration model — loaded from YAML."""

    default_model: str = ""
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

_LOG_STYLES = {
    "user": ("📝 USER", "│"),
    "model": ("🤖 MODEL", "│"),
    "tool": ("🔧 TOOL", "│"),
    "mcp": ("🌐 MCP", "│"),
    "boot": ("🚀 SYSTEM STARTUP", "║"),
    "help": ("ℹ️ HELP", "│"),
}


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

_http_pool: "httpx.AsyncClient | None" = None
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
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": self.schema(),
            },
        }


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
                system_prompt = cfg.resolve_system_prompt(parent_controller.root) + "\n\n" + extra_prompt

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

        ask_tool = AskTool()
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
            except Exception:
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
        except:
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
        return {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "The task for the subagent. Be specific and include all necessary context."},
                "agent": {"type": "string", "description": "Optional named subagent profile from config (e.g. 'researcher', 'coder'). Uses default if omitted."},
                "label": {"type": "string", "description": "Optional short label for display."},
            },
            "required": ["task"],
        }

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
    """Extract a starter task list from an approved plan using robust regex.

    Matches various Markdown list styles:
    - "1. Task"
    - "- Task"
    - "* Task"
    - "+ Task"
    Ignores indentation for base-level extraction, handles Markdown bold/code.
    """
    import re

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

        kwargs = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "timeout": self.entry.request_timeout,
            "api_key": self.api_key,
        }
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
            return {"content": "Error: API Rate Limit Exceeded. Please wait a moment and try again.", "tool_calls": [], "finish_reason": "error"}
        except exceptions.AuthenticationError as e:
            logger.error(f"Authentication failed: {e}")
            return {"content": "Error: Authentication failed. Check your API key.", "tool_calls": [], "finish_reason": "error"}
        except exceptions.ServiceUnavailableError as e:
            logger.error(f"Service Unavailable: {e}")
            return {"content": "Error: Service is currently unavailable (e.g. high load). Please try again in a few moments.", "tool_calls": [], "finish_reason": "error"}
        except Exception as e:
            logger.error(f"LLM call error: {e}")
            return {"content": f"Error: {e}", "tool_calls": [], "finish_reason": "error"}

        choice = response.choices[0]
        msg = choice.message
        tool_calls = []
        if msg.tool_calls:
            tool_calls = [{"id": tc.id, "type": "function", "function": {"name": tc.function.name, "arguments": tc.function.arguments}} for tc in msg.tool_calls]

        usage = response.usage
        usage_data = {}
        if usage:
            usage_data = {
                "input": usage.prompt_tokens,
                "output": usage.completion_tokens,
            }
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
    代表一个 MCP 服务端暴露的单个远程工具。
    - discover_all(): 类方法，连接服务端发现所有工具，返回实例列表
    - __call__(): 每次调用时建立新连接执行 tools/call
    """

    def __init__(self, server_url: str, tool_name: str, tool_description: str, tool_schema: dict):
        self._server_url = server_url
        self._tool_name = tool_name
        self._tool_description = tool_description
        self._tool_schema = tool_schema

    @classmethod
    async def discover_all(cls, server_name: str, url: str) -> list:
        """连接 MCP 服务端，发现所有工具，返回 _MCPRemoteTool 实例列表"""
        from mcp import ClientSession
        from mcp.client.sse import sse_client

        tools = []
        try:
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
            logger.error(f"MCP Discovery failed for '{server_name}' ({url}): {e}")
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
        # Subagent support
        self.subagent_manager: SubagentManager = SubagentManager(max_concurrent=4)
        self._pending_queue: Optional[asyncio.Queue] = None

    def set_plan_mode(self, on: bool) -> None:
        self._plan_mode = on
        self.registry.plan_mode = on
        _stdout(f"Plan mode: {'ON' if on else 'OFF'}")

    @property
    def plan_mode(self) -> bool:
        return self._plan_mode

    def _sessions_dir(self) -> Path:
        d = Path.home() / ".reasonix" / "sessions"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def save_session(self) -> str:
        import time, hashlib

        if self.current_session_id:
            sid = self.current_session_id
        else:
            ts = time.strftime("%Y%m%d_%H%M%S")
            rand = hashlib.sha256(str(time.time()).encode()).hexdigest()[:6]
            sid = f"{ts}_{rand}"
            self.current_session_id = sid

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
        self.current_session_id = sid
        if ctx_data:
            self.context = Context.from_dict(ctx_data, self.cfg.agent)
        return True
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
        self.context = Context(system_prompt, self.cfg.agent, self.perm_manager)
        self.step_count = 0

    async def _register_mcp_tools(self):
        """连接所有配置的 MCP 服务端，发现并注册其暴露的工具。"""
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
        system_prompt = self.cfg.resolve_system_prompt(self.root)
        self.context = Context(system_prompt, self.cfg.agent, self.perm_manager)

        _cmd = " ".join(getattr(sys, "orig_argv", sys.argv))
        import datetime

        now = datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
        system_info = f"\nSystem Info: Platform={sys.platform}, Python={sys.version.split()[0]}, Time={now}"
        self.context.add_user(f"System initialized. Service started with command: `{_cmd}`{system_info}")

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
        """Show the plan proposal and ask the user to approve or reject.

        Returns True on approval. Mirrors Harness requestApproval called with
        planApprovalTool after a plan-mode turn finishes.
        """
        _stdout("\n" + "\u2550" * 60)
        _stdout("\U0001f4cb  PLAN MODE \u2014 proposed plan:")
        _stdout("\u2550" * 60)
        _stdout(proposal)
        _stdout("\u2550" * 60)
        return True

    @tracer.start_as_current_span("run_turn")
    async def _run_turn(self, composed_input: str) -> str:
        """Run one model turn (tool loop) and return the last assistant text.

        Separated from run() so the plan approval flow can call it for the
        follow-up execution turn without re-applying _compose. Uses
        registry.execute_gated so the plan-mode gate is enforced on every
        tool call inside this turn too.

        Subagent integration: A pending queue is created for this turn.
        Between tool iterations, completed subagent results are drained
        and injected as user messages so the model can incorporate them.
        """
        self.context.add_user(composed_input)
        turn_assistant_contents = []  # Track assistant content independently of compaction
        max_steps = self.cfg.agent.max_steps or 10**8

        # Initialize pending queue for subagent result injection
        self._pending_queue = asyncio.Queue()

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
        """Run a user request, honouring plan mode.

        Plan-mode flow (mirrors Harness runTurnWithRawDisplay):
          1. Prepend PlanModeMarker and run a read-only research/planning turn.
          2. Present the proposal to the user for approval.
          3a. Approved  -> exit plan mode, seed todos, run execution turn.
          3b. Rejected  -> stay in plan mode; user can revise and re-submit.

        Normal flow: just run the tool loop.
        """
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
# 7. CLI Entry Point
# =============================================================================


async def _run_and_cleanup(ctrl, req: str) -> str:
    """Run a turn and close the http pool before returning.

    Each asyncio.run() creates a new event loop that is closed on exit.
    The global httpx.AsyncClient is bound to the loop that created it,
    so it must be destroyed before the loop closes to avoid
    'Event loop is closed' errors on the next asyncio.run() call.
    """
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


def print_help():
    help_text = """
SYNOPSIS
  Harness Kernel [options] [request]

COMMANDS
  /skills           List available skills
  /tools            List available tools (built-in and MCP)
  /mcp              List all connected MCP servers and their tools
  /info             Display system status and help
  /new              Start a new conversation (automatically saves current session)
  /clear            Same as /new, clears conversation context
  /model            List all available providers
  /model <name/idx> Switch to the specified provider
  /plan             Enable plan mode (next request is planned before execution)
  /plan on/off    Enable or disable plan mode
  /plan status      Check current plan mode status
  /context          Display current context and LLM request payload
  /history          Display conversation history
  /exit, /quit      Exit (automatically saves session)
  q                 Exit (same as /quit)

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
        files = list(sessions_dir.glob("*.json"))
        if files:
            latest_file = max(files, key=os.path.getmtime)
            sid_to_load = latest_file.stem
            logger.info(f"Auto-resuming latest session: {sid_to_load}")
        else:
            logger.warning("No sessions found to auto-resume.")
    elif args.resume:
        sid_to_load = args.resume

    if sid_to_load:
        if ctrl.load_session(sid_to_load):
            register_all_builtins(ctrl.registry, ctrl.cfg, ctrl.root)
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

    # --- Command dispatch table ---
    def _cmd_help(req):
        print_help()

    def _cmd_exit(req):
        raise StopIteration

    def _cmd_new(req):
        sid = ctrl.save_session()
        ctrl.reset_context()
        _stdout(f"New context started. Previous session: --resume {sid}")

    def _cmd_model(req):
        parts = req.split(None, 1)
        if len(parts) == 1:
            _stdout("\n".join(ctrl.list_providers()))
        else:
            _stdout(ctrl.switch_provider(parts[1]))

    def _cmd_context(req):
        if ctrl.context:
            messages = ctrl.context.to_openai()
            tools = ctrl.registry.schemas()
            payload = {
                "model": ctrl.provider.entry.model if ctrl.provider else "default",
                "messages": messages,
                "tools": tools,
                "temperature": ctrl.cfg.agent.temperature,
                "todos": ctrl.context.todos,
            }
            _stdout("\n--- Simulated LLM Request Payload ---")
            _stdout(json.dumps(payload, indent=2, ensure_ascii=False))
            _stdout("-------------------------------------\n")
        else:
            _stdout("Context is empty.")

    def _cmd_skills(req):
        skills = sorted(ctrl.cfg.enabled_skills(), key=lambda s: s.name)
        _stdout("Available skills:\n" + "\n".join([f"/{s.name} — {s.description[:47] + '...' if len(s.description) > 50 else s.description}" for s in skills]))

    def _cmd_tools(req):
        builtins = [t for t in ctrl.registry.list() if not t.name().startswith("mcp_")]
        _stdout("Available built-in tools:\n" + "\n".join([f"  {t.name()}" for t in builtins]))

    def _cmd_mcp(req):
        mcp_tools = [t for t in ctrl.registry.list() if t.name().startswith("mcp_")]
        _stdout("Available MCP tools:\n" + "\n".join([f"  {t.name()} — {t.description()}" for t in mcp_tools]))

    def _cmd_info(req):
        asyncio.run(ctrl.boot())
        print_help()

    def _cmd_history(req):
        if ctrl.context:
            _stdout("--- Interaction History ---")
            for msg in ctrl.context.messages:
                _stdout(f"[{msg.role.value.upper()}] {msg.content}")
        else:
            _stdout("Context is empty.")

    def _cmd_plan(req):
        parts = req.split(None, 1)
        sub = parts[1].strip().lower() if len(parts) > 1 else None
        if sub is None:
            ctrl.set_plan_mode(not ctrl.plan_mode)
        elif sub in ("on", "enable", "true", "1"):
            ctrl.set_plan_mode(True)
        elif sub in ("off", "disable", "false", "0"):
            ctrl.set_plan_mode(False)
        elif sub in ("status",):
            _stdout(f"Plan mode: {'ON' if ctrl.plan_mode else 'OFF'}")
        else:
            _stdout(f"Unknown plan mode argument: {sub}. Use /plan [on/off/status]")

    def _cmd_mcp(req):
        if not ctrl.registry:
            _stdout("No tools registered.")
            return

        mcp_tools = [t for t in ctrl.registry.list() if "mcp" in t.name().lower()]
        if not mcp_tools:
            _stdout("No MCP tools found.")
        else:
            _stdout("Available MCP tools:")
            for t in mcp_tools:
                _stdout(f"  {t.name()}: {t.description()}")

    # Exact-match commands
    COMMANDS = {
        "/help": _cmd_help,
        "?": _cmd_help,
        "/exit": _cmd_exit,
        "/quit": _cmd_exit,
        "q": _cmd_exit,
        "/new": _cmd_new,
        "/clear": _cmd_new,
        "/context": _cmd_context,
        "/skills": _cmd_skills,
        "/tools": _cmd_tools,
        "/mcp": _cmd_mcp,
        "/mcp": _cmd_mcp,
        "/info": _cmd_info,
        "/history": _cmd_history,
    }
    # Prefix-match commands (checked in order)
    PREFIX_COMMANDS = [
        ("/model", _cmd_model),
        ("/plan", _cmd_plan),
    ]

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
            if not handled:
                # Skill dispatch
                if req.startswith("/") and ctrl.cfg.get_skill(req[1:].split()[0]):
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
