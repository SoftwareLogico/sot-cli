from __future__ import annotations

import mimetypes
import os
import platform
import socket
import subprocess
import getpass
import sys
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Any

from sot_cli.config.prompts import AGENT_SYSTEM_PROMPT, JB_SYSTEM_PROMPT, SUB_AGENT_SYSTEM_PROMPT
from sot_cli.constants import SOT_MARKER
from sot_cli.utils.text import _count_lines


def build_system_prompt() -> str:
    return JB_SYSTEM_PROMPT.strip()


def build_orchestration_rules(is_sub_agent: bool = False) -> str:
    parts = []
    if is_sub_agent:
        parts.append(SUB_AGENT_SYSTEM_PROMPT.strip())
    else:
        parts.append(AGENT_SYSTEM_PROMPT.strip())
    # Append host environment block (best-effort) so payloads always include it
    host_context = build_host_environment_prompt()
    if host_context:
        parts.append(host_context)
    return "\n\n".join(parts)


def build_host_environment_prompt() -> str:
    """Build a compact host environment block (best-effort)."""
    def _safe_call(func, *a, **kw):
        try:
            return func(*a, **kw)
        except Exception:
            return None

    def _zoneinfo_utc_available() -> bool:
        try:
            ZoneInfo("UTC")
            return True
        except Exception:
            return False

    lines: list[str] = [
        "HOST ENVIRONMENT (best-effort; may be partial if some fields are unavailable)",
        "Use this information as practical operating context for paths, shell commands, and environment-sensitive decisions.",
    ]

    def add_line(label: str, value: str | None) -> None:
        if value:
            cleaned = str(value).strip()
            if cleaned:
                lines.append(f"- {label}: {cleaned}")

    add_line("Operating system", _normalize_os_name(_safe_call(platform.system)))
    add_line("OS release", _safe_call(platform.release))
    add_line("OS version", _safe_call(platform.version))
    add_line("Architecture", _normalize_arch(_safe_call(platform.machine)))
    add_line("Processor", _safe_call(platform.processor) or None)
    add_line("Hostname", _safe_call(socket.gethostname))

    try:
        local_now = datetime.now().astimezone()
        add_line("Current local date and time", local_now.isoformat(timespec="seconds"))
        add_line("Current local date", local_now.date().isoformat())
        add_line("Current local weekday", local_now.strftime("%A"))
        add_line("Current local ISO weekday", str(local_now.isoweekday()))
        if _zoneinfo_utc_available():
            utc_now = local_now.astimezone(ZoneInfo("UTC"))
            add_line("Current UTC date and time", utc_now.isoformat(timespec="seconds"))
            add_line("Current UTC date", utc_now.date().isoformat())
            add_line("Current UTC weekday", utc_now.strftime("%A"))
            add_line("Current UTC ISO weekday", str(utc_now.isoweekday()))
        else:
            utc_now = datetime.utcnow()
            add_line("Current UTC date and time", utc_now.isoformat(timespec="seconds"))
            add_line("Current UTC date", utc_now.date().isoformat())
            add_line("Current UTC weekday", utc_now.strftime("%A"))
            add_line("Current UTC ISO weekday", str(utc_now.isoweekday()))
        try:
            tzname = local_now.tzinfo.tzname(local_now) if local_now.tzinfo else None
            if tzname:
                add_line("Timezone", tzname)
        except Exception:
            pass
    except Exception:
        pass

    add_line("Username", _safe_call(getpass.getuser) or os.environ.get("USER") or os.environ.get("USERNAME"))
    add_line("User home directory", _safe_call(lambda: str(Path.home())))
    add_line("Current working directory", _safe_call(os.getcwd))
    add_line("Default shell", os.environ.get("SHELL"))
    add_line("Active shell", _detect_active_shell())
    add_line("Terminal", os.environ.get("TERM_PROGRAM") or os.environ.get("TERM"))
    add_line("Locale", os.environ.get("LANG"))
    add_line("Python executable", _safe_call(lambda: os.path.realpath(sys.executable)))

    return "\n".join(lines).strip()


def _detect_active_shell() -> str | None:
    """Detect the shell that will actually execute run_command commands."""
    system = platform.system().lower()

    if system == "windows":
        # Check if we're inside PowerShell by looking for its env vars
        if os.environ.get("PSModulePath"):
            ps_version = os.environ.get("PSVersion")
            if ps_version:
                return f"PowerShell {ps_version}"
            return "PowerShell"
        comspec = os.environ.get("COMSPEC", "")
        if comspec:
            return f"CMD ({comspec})"
        return "CMD"

    # Unix-like: check SHELL env var
    shell_path = os.environ.get("SHELL", "")
    if shell_path:
        shell_name = os.path.basename(shell_path)
        return shell_name  # e.g. "bash", "zsh", "fish"

    return None


def _normalize_arch(machine: str | None) -> str | None:
    if not machine:
        return None
    lowered = machine.strip().lower()
    if lowered in ("amd64", "x86_64"):
        return "x64"
    if lowered in ("x86", "i386", "i686"):
        return "x86"
    if lowered in ("arm64", "aarch64"):
        return "arm64"
    return machine.strip()


def _normalize_os_name(system_name: str | None) -> str | None:
    if not system_name:
        return None
    lowered = system_name.strip().lower()
    if lowered == "darwin":
        return "macOS"
    if lowered == "windows":
        return "Windows"
    if lowered == "linux":
        return "Linux"
    return system_name.strip()

    
def build_user_turn_message(user_prompt: str, source_index: str, source_contents: str = "") -> str:
    parts = ["USER REQUEST", user_prompt.strip(), "", source_index.strip()]
    if source_contents.strip():
        parts.extend(["", source_contents.strip()])
    return "\n".join(parts).strip()


def build_sot_user_message(
    tracked_files: dict[str, str],
    tracked_media: dict[str, list[dict[str, Any]]],
    media_file_count: int = 0,
) -> dict[str, Any]:
    """
    Build the SoT user message. Rebuilt after tool calls execute, before the model's next response.

    Returns a message dict with role=user. Content is either a string (text only)
    or a list of content parts (text + image_url + input_audio + video_url etc)
    when there's multimodal media tracked.

    tracked_files: {absolute_path: file_content_from_disk}
    tracked_media: {absolute_path: content parts from read_text_file}
    media_file_count: number of distinct media FILES (not parts)
    """
    text_sections = [SOT_MARKER]

    file_count = len(tracked_files) + media_file_count
    if tracked_files or tracked_media:
        text_sections.append(f"Files tracked: {file_count}")
        for fpath, content in tracked_files.items():
            line_count = _count_lines(content)
            size_bytes = len(content.encode("utf-8"))
            numbered_lines: list[str] = []
            for i, line in enumerate(content.split("\n"), 1):
                numbered_lines.append(f"{i:>6}|{line}")
            numbered_content = "\n".join(numbered_lines)
            text_sections.append(
                "\n".join(
                    [
                        f"--- FILE: {fpath} ({line_count} lines, {size_bytes} bytes) ---",
                        numbered_content,
                        f"--- END: {fpath} ---",
                    ]
                )
            )
    else:
        text_sections.append("No files tracked yet.")

    sot_text = "\n\n".join(text_sections)

    if not tracked_media:
        return {"role": "user", "content": sot_text}

    content_parts: list[dict[str, Any]] = [
        {"type": "text", "text": section}
        for section in text_sections
    ]
    content_parts.extend(_build_media_content_parts(tracked_media))
    content_parts.append({"type": "text", "text": "=== END SOURCE OF TRUTH ==="})
    return {"role": "user", "content": content_parts}


def _build_media_content_parts(tracked_media: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    content_parts: list[dict[str, Any]] = []
    for path, parts in tracked_media.items():
        if not parts:
            continue

        intro_text = _build_media_intro_text(path, parts)
        intro_replaced = False
        for part in parts:
            if not isinstance(part, dict):
                continue
            if not intro_replaced and part.get("type") == "text":
                content_parts.append({"type": "text", "text": intro_text})
                intro_replaced = True
                continue
            content_parts.append(part)

        if not intro_replaced:
            content_parts.append({"type": "text", "text": intro_text})

    return content_parts


def _build_media_intro_text(path: str, parts: list[dict[str, Any]]) -> str:
    original_text = next(
        (
            part.get("text", "")
            for part in parts
            if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str)
        ),
        f"Supplemental content from read_text_file for {path}.",
    )

    metadata: list[str] = []
    file_path = Path(path)
    mime_type, _ = mimetypes.guess_type(path)
    if mime_type:
        metadata.append(f"mime={mime_type}")
    try:
        metadata.append(f"size={file_path.stat().st_size} bytes")
    except OSError:
        pass

    payload_types = sorted(
        {
            str(part.get("type", "")).strip()
            for part in parts
            if isinstance(part, dict) and str(part.get("type", "")).strip() and part.get("type") != "text"
        }
    )
    if payload_types:
        metadata.append(f"parts={','.join(payload_types)}")

    if not metadata:
        return original_text
    return f"{original_text} meta: {'; '.join(metadata)}"


def detect_launch_context() -> dict[str, Any]:
    """Best-effort detection of how sot-cli was launched.

    Returns a dict with:
      - argv: list[str] - raw sys.argv (script + args)
      - runner: str | None - inferred runner name (uv, conda, poetry, pipenv, venv)
      - runner_detail: str | None - short human description (e.g., "uv run", "conda env 'sot'")
      - parent_cmdline: str | None - parent process command line (Unix only; skipped on Windows to avoid latency)
      - python_executable: str - resolved path to the running python interpreter
    """
    argv = list(sys.argv)

    runner: str | None = None
    runner_detail: str | None = None

    if os.environ.get("UV") or os.environ.get("UV_PROJECT_ROOT") or os.environ.get("UV_PROJECT_ENVIRONMENT"):
        runner = "uv"
        runner_detail = "uv run"
    elif os.environ.get("POETRY_ACTIVE") == "1":
        runner = "poetry"
        runner_detail = "poetry run"
    elif os.environ.get("PIPENV_ACTIVE") == "1":
        runner = "pipenv"
        runner_detail = "pipenv run"
    elif os.environ.get("CONDA_DEFAULT_ENV"):
        env_name = os.environ.get("CONDA_DEFAULT_ENV") or ""
        runner = "conda"
        runner_detail = f"conda env '{env_name}'" if env_name else "conda"
    elif os.environ.get("VIRTUAL_ENV"):
        runner = "venv"
        venv_path = os.environ.get("VIRTUAL_ENV") or ""
        runner_detail = f"venv ({venv_path})" if venv_path else "venv"

    parent_cmdline: str | None = None
    system_name = platform.system().lower()
    if system_name != "windows":
        try:
            ppid = os.getppid()
            completed = subprocess.run(
                ["ps", "-o", "args=", "-p", str(ppid)],
                capture_output=True,
                text=True,
                timeout=1.0,
            )
            if completed.returncode == 0:
                line = completed.stdout.strip()
                if line:
                    parent_cmdline = line
        except Exception:
            parent_cmdline = None

    try:
        python_executable = os.path.realpath(sys.executable)
    except Exception:
        python_executable = sys.executable or ""

    return {
        "argv": argv,
        "runner": runner,
        "runner_detail": runner_detail,
        "parent_cmdline": parent_cmdline,
        "python_executable": python_executable,
    }


def build_previous_turn_metadata_message(metadata: dict[str, Any]) -> dict[str, Any] | None:
    """Build a single-line CURRENT METADATA user message from the previous turn's stats.

    Input metadata is a flat dict of label -> scalar value, plus an optional
    'launch_context' dict (as produced by detect_launch_context). Returns a
    message dict {"role": "user", "content": "..."} or None if nothing useful.
    """
    if not metadata:
        return None

    pairs: list[str] = []
    for key, value in metadata.items():
        if key == "launch_context":
            continue
        if value is None or value == "":
            continue
        pairs.append(f"{key}: {value}")

    launch = metadata.get("launch_context")
    if isinstance(launch, dict):
        argv = launch.get("argv") or []
        argv_str = " ".join(str(a) for a in argv) if argv else ""
        runner_detail = launch.get("runner_detail")
        parent = launch.get("parent_cmdline")
        pyexe = launch.get("python_executable")

        if parent:
            pairs.append(f"Launch: {parent}")
        elif runner_detail and argv_str:
            pairs.append(f"Launch: {runner_detail} -> {argv_str}")
        elif runner_detail:
            pairs.append(f"Launch: {runner_detail}")
        elif argv_str:
            pairs.append(f"Launch: {argv_str}")

        if pyexe:
            pairs.append(f"Python: {pyexe}")

    if not pairs:
        return None

    content = "=== CURRENT METADATA ===\n" + "; ".join(pairs) + "\n=== END CURRENT METADATA ==="
    return {"role": "user", "content": content}