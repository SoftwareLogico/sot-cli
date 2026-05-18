from __future__ import annotations

import subprocess
import sys
import os
from pathlib import Path
from typing import Any

from sot_cli.config import KNOWN_PROVIDERS
from sot_cli.config.prompts import DELEGATED_TASK_WRAPPER
from sot_cli.runtime import AppRuntime
from sot_cli.tools.utils.validators import _require_string


def _write_response_md(
    parent_session_dir: Path,
    child_session_id: str,
    status: str,
    task_prompt: str,
    result: str,
    usage: dict[str, Any],
    error: str,
) -> Path:
    """Write a standardized `response.md` under
    `<sessions>/<parent>/agents/<child>/response.md`.
    """
    child_dir = parent_session_dir / "agents" / child_session_id
    child_dir.mkdir(parents=True, exist_ok=True)
    md_path = child_dir / "response.md"

    formatted_prompt = task_prompt.replace("\n", "\n> ")
    from datetime import datetime
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    lines: list[str] = [
        f"# Delegated Task Report: {child_session_id}",
        f"**Status:** {status.upper()}",
        f"**Completed At:** {now_str}",
        "",
        "## Usage",
        f"- Total Tokens: {usage.get('total_tokens', 0)}",
        f"- Cost: ${usage.get('cost', 0.0)}",
        "",
        "## Result",
        result if result else (error or "No result provided."),
    ]

    if error and result:
        lines.extend(["", "## Error Log", error])

    md_path.write_text("\n".join(lines), encoding="utf-8")
    return md_path


def _resolve_delegate_runtime(runtime: AppRuntime, parent_session_id: str, provider_override: Any) -> tuple[str, str, float | None, int | None]:
    parent_session = runtime.sessions.load(parent_session_id)
    if provider_override is None:
        return (
            parent_session.provider,
            parent_session.model,
            parent_session.temperature,
            parent_session.max_output_tokens,
        )

    if not isinstance(provider_override, str) or provider_override not in KNOWN_PROVIDERS:
        raise ValueError(f"provider must be one of: {', '.join(KNOWN_PROVIDERS)}")

    provider_config = runtime.config.provider(provider_override)
    if not provider_config.enabled:
        raise ValueError(f"Provider is not configured: {provider_override}")
    if not provider_config.model:
        raise ValueError(f"Provider {provider_override} has no default model configured.")

    return (
        provider_override,
        provider_config.model,
        provider_config.temperature,
        provider_config.max_output_tokens,
    )


def execute_delegate_task(arguments: dict[str, Any], runtime: AppRuntime, parent_session_id: str) -> dict[str, Any]:
    task_prompt = _require_string(arguments, "task_prompt")
    attempts = int(arguments.get("attempts", 2))
    background = bool(arguments.get("background", False))
    wrapped_task_prompt = DELEGATED_TASK_WRAPPER.replace("{attempts}", str(attempts)) + task_prompt.strip()


    agents_dir = runtime.paths.sessions_dir / parent_session_id / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)


    existing_nums: list[int] = []
    for d in agents_dir.iterdir():
        if d.is_dir() and d.name.startswith("agent_"):
            try:
                existing_nums.append(int(d.name.split("_")[1]))
            except (ValueError, IndexError):
                pass
    next_num = max(existing_nums) + 1 if existing_nums else 1
    agent_id = f"agent_{next_num}"

    parent_session = runtime.sessions.load(parent_session_id)

    # ── Sub-agent model: session subagent_model > provider config subagent_model > main model ──
    resolved_model = parent_session.subagent_model
    if not resolved_model:
        provider_config = runtime.config.provider(parent_session.provider)
        resolved_model = provider_config.subagent_model
    if not resolved_model:
        resolved_model = parent_session.model

    resolved_provider = parent_session.provider

    original_sessions_dir = runtime.sessions.sessions_dir
    runtime.sessions.sessions_dir = agents_dir
    try:
        temp_session = runtime.sessions.create_session(
            title=f"delegate {parent_session_id}",
            provider=resolved_provider,
            model=resolved_model,
        )
        old_dir = agents_dir / temp_session.id
        temp_session.id = agent_id
        runtime.sessions.save(temp_session)

        import shutil
        try:
            if old_dir.exists():
                shutil.rmtree(old_dir)
        except Exception:
            pass
        new_agent_dir = agents_dir / agent_id
    finally:
        runtime.sessions.sessions_dir = original_sessions_dir

    # 4. Preparar comando y entorno
    env = os.environ.copy()
    env["SOT_SESSIONS_DIR"] = str(agents_dir)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"

    command = [
        sys.executable, "-X", "utf8", "-m", "sot_cli",
        "--config", str(runtime.paths.config_file),
        "--run_task", agent_id, wrapped_task_prompt,
    ]

    new_agent_dir.mkdir(parents=True, exist_ok=True)

    popen_kwargs: dict[str, Any] = {
        "cwd": str(runtime.paths.root_dir),
        "env": env,
        "stderr": subprocess.STDOUT,
        "stdout": subprocess.PIPE,
    }

    if os.name == "nt":
        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        popen_kwargs["start_new_session"] = True

    if background:
        proc = subprocess.Popen(command, **popen_kwargs)
        # Drain stdout in a daemon thread so the pipe never blocks
        import threading
        log_path = new_agent_dir / "agent.log"

        def _drain(p: subprocess.Popen, path: Path) -> None:  # type: ignore[type-arg]
            try:
                with open(path, "wb") as lf:
                    assert p.stdout is not None
                    for chunk in iter(lambda: p.stdout.read(4096), b""):
                        lf.write(chunk)
            except Exception:
                pass

        threading.Thread(target=_drain, args=(proc, log_path), daemon=True).start()
        return {"status": "started", "agent_id": agent_id}

    # Foreground: run to completion, stream output to log
    log_path = new_agent_dir / "agent.log"
    proc = subprocess.Popen(command, **popen_kwargs)
    assert proc.stdout is not None
    with open(log_path, "wb") as lf:
        for chunk in iter(lambda: proc.stdout.read(4096), b""):
            lf.write(chunk)
    proc.wait()

    return {"status": "completed", "agent_id": agent_id}
