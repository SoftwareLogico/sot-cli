from __future__ import annotations
from pathlib import Path
from typing import Any
import time
from sot_cli.tools.utils.validators import _require_string


def execute_list_tasks(arguments: dict[str, Any], sessions_dir: Path, session_id: str) -> dict[str, Any]:
    parent_session_dir = sessions_dir / session_id
    agents_dir = parent_session_dir / "agents"

    tasks: list[dict[str, Any]] = []
    if agents_dir.exists():
        for agent_folder in sorted(agents_dir.iterdir()):
            if not agent_folder.is_dir():
                continue
            agent_id = agent_folder.name
            report = agent_folder / "response.md"
            tasks.append({
                "agent_id": agent_id,
                "status": "COMPLETED" if report.exists() else "RUNNING",
            })

    return {"tasks": tasks}


def execute_wait_task(arguments: dict[str, Any], sessions_dir: Path, session_id: str) -> dict[str, Any]:
    agent_id = _require_string(arguments, "agent_id")
    timeout_seconds = arguments.get("timeout_seconds")
    
    parent_session_dir = sessions_dir / session_id
    report_path = parent_session_dir / "agents" / agent_id / "response.md"
    
    start_time = time.time()
    while True:
        if report_path.exists():
            content = report_path.read_text(encoding="utf-8")
            return {
                "agent_id": agent_id,
                "status": "COMPLETED",
                "report": content
            }
        
        if timeout_seconds is not None and (time.time() - start_time) >= timeout_seconds:
            return {"agent_id": agent_id, "status": "RUNNING", "timed_out": True}
        
        time.sleep(1.0)
