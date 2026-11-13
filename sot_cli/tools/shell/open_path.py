from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from sot_cli.tools.utils.path_helpers import resolve_path
from sot_cli.tools.utils.validators import _require_string


def execute_open_path(arguments: dict[str, Any], root_dir: Path) -> dict[str, Any]:
    raw_path = _require_string(arguments, "path")
    path = resolve_path(raw_path, root_dir)
    if not path.exists():
        raise FileNotFoundError(f"Path does not exist: {path}")

    raw_application = arguments.get("application")
    application = _normalize_application(raw_application, root_dir)

    if os.name == "nt":
        return _open_on_windows(path, application)
    if sys.platform == "darwin":
        return _open_on_macos(path, application)
    return _open_on_linux(path, application)


def _normalize_application(value: Any, root_dir: Path) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("application must be a non-empty string when provided")

    normalized = value.strip()
    if any(sep in normalized for sep in ("/", "\\")) or normalized.startswith(".") or normalized.startswith("~"):
        return str(resolve_path(normalized, root_dir))
    return normalized


def _spawn(command: list[str]) -> int:
    kwargs: dict[str, Any] = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True

    process = subprocess.Popen(command, **kwargs)
    return process.pid


def _run_checked(command: list[str], error_message: str) -> None:
    completed = subprocess.run(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(error_message)


def _normalize_match_key(value: str) -> str:
    return "".join(char for char in value.casefold() if char.isalnum())


def _is_subsequence(query: str, target: str) -> bool:
    if not query:
        return False
    index = 0
    for char in target:
        if index < len(query) and char == query[index]:
            index += 1
            if index == len(query):
                return True
    return index == len(query)


def _looks_like_existing_executable(application: str) -> str | None:
    candidate = Path(application)
    if candidate.exists() and candidate.is_file():
        return str(candidate)
    executable = shutil.which(application)
    if executable:
        return executable
    return None


def _candidate_rank(query: str, candidate_name: str) -> tuple[int, int, str] | None:
    normalized_name = _normalize_match_key(candidate_name)
    if not normalized_name:
        return None
    if normalized_name == query:
        return (0, len(candidate_name), candidate_name.casefold())
    if query in normalized_name:
        return (1, len(candidate_name), candidate_name.casefold())
    if _is_subsequence(query, normalized_name):
        return (2, len(candidate_name), candidate_name.casefold())
    if normalized_name in query:
        return (3, len(candidate_name), candidate_name.casefold())
    return None


def _rank_candidates(query: str, candidates: list[tuple[str, str]]) -> list[tuple[str, str]]:
    ranked: list[tuple[tuple[int, int, str], str, str]] = []
    for display_name, value in candidates:
        rank = _candidate_rank(query, display_name)
        if rank is not None:
            ranked.append((rank, display_name, value))
    ranked.sort(key=lambda item: item[0])
    return [(display_name, value) for _, display_name, value in ranked]


def _macos_app_candidates() -> list[tuple[str, str]]:
    search_roots = [
        Path("/Applications"),
        Path("/System/Applications"),
        Path.home() / "Applications",
    ]
    candidates: list[tuple[str, str]] = []
    seen: set[str] = set()
    for root in search_roots:
        if not root.exists():
            continue
        # Limit search to depth 2 to avoid scanning inside .app bundles
        for app in list(root.glob("*.app")) + list(root.glob("*/*.app")):
            key = str(app)
            if key in seen:
                continue
            seen.add(key)
            candidates.append((app.stem, str(app)))
    return candidates


def _path_executable_candidates() -> list[tuple[str, str]]:
    candidates: list[tuple[str, str]] = []
    seen: set[str] = set()
    path_value = os.environ.get("PATH", "")
    for raw_dir in path_value.split(os.pathsep):
        if not raw_dir:
            continue
        directory = Path(raw_dir)
        if not directory.is_dir():
            continue
        try:
            for child in directory.iterdir():
                if not child.is_file():
                    continue
                key = child.name.casefold()
                if key in seen:
                    continue
                seen.add(key)
                candidates.append((child.name, str(child)))
        except OSError:
            continue
    return candidates


def _linux_desktop_candidates() -> list[tuple[str, str]]:
    roots = [
        Path.home() / ".local/share/applications",
        Path("/usr/share/applications"),
        Path("/usr/local/share/applications"),
    ]
    candidates: list[tuple[str, str]] = []
    seen: set[str] = set()
    for root in roots:
        if not root.exists():
            continue
        try:
            for entry in root.glob("*.desktop"):
                key = entry.stem.casefold()
                if key in seen:
                    continue
                seen.add(key)
                candidates.append((entry.stem, entry.stem))
        except OSError:
            continue
    return candidates


def _windows_program_candidates() -> list[tuple[str, str]]:
    roots = [
        os.environ.get("ProgramFiles"),
        os.environ.get("ProgramFiles(x86)"),
        str(Path.home() / "AppData/Local/Programs"),
    ]
    candidates: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw_root in roots:
        if not raw_root:
            continue
        root = Path(raw_root)
        if not root.exists():
            continue
        try:
            for child in root.iterdir():
                if child.is_dir():
                    key = child.name.casefold()
                    if key not in seen:
                        seen.add(key)
                        candidates.append((child.name, str(child)))
                    for exe in child.glob("*.exe"):
                        exe_key = exe.stem.casefold()
                        if exe_key in seen:
                            continue
                        seen.add(exe_key)
                        candidates.append((exe.stem, str(exe)))
        except OSError:
            continue
    return candidates


def _application_suggestions(application: str) -> list[str]:
    query = _normalize_match_key(application)
    if not query:
        return []

    candidates = _path_executable_candidates()
    if sys.platform == "darwin":
        candidates.extend(_macos_app_candidates())
    elif os.name == "nt":
        candidates.extend(_windows_program_candidates())
    else:
        candidates.extend(_linux_desktop_candidates())

    ranked = _rank_candidates(query, candidates)
    suggestions: list[str] = []
    seen: set[str] = set()
    for display_name, _ in ranked:
        key = display_name.casefold()
        if key in seen:
            continue
        seen.add(key)
        suggestions.append(display_name)
        if len(suggestions) >= 5:
            break
    return suggestions


def _missing_application_error(path: Path, application: str) -> RuntimeError:
    suggestions = _application_suggestions(application)
    if suggestions:
        return RuntimeError(
            f"Could not open {path} with application '{application}'. Similar installed applications: {', '.join(suggestions)}."
        )
    return RuntimeError(f"Could not open {path} with application '{application}'.")


def _find_macos_app_bundle(application: str) -> str | None:
    query = _normalize_match_key(application)
    if not query:
        return None

    ranked = _rank_candidates(query, _macos_app_candidates())
    if not ranked:
        return None
    return ranked[0][1]


def _open_on_macos(path: Path, application: str | None) -> dict[str, Any]:
    if application is None:
        _run_checked(["open", str(path)], f"Could not open {path} with the default application.")
        return {
            "path": str(path),
            "application": None,
            "launcher": "open",
            "used_default_application": True,
        }

    executable = _looks_like_existing_executable(application)
    if executable is not None:
        pid = _spawn([executable, str(path)])
        return {
            "path": str(path),
            "application": application,
            "launcher": executable,
            "pid": pid,
            "used_default_application": False,
        }

    app_bundle = _find_macos_app_bundle(application)
    if app_bundle is not None:
        _run_checked(
            ["open", "-a", app_bundle, str(path)],
            f"Could not open {path} with application '{application}'.",
        )
        return {
            "path": str(path),
            "application": application,
            "launcher": "open",
            "resolved_application": app_bundle,
            "used_default_application": False,
        }

    try:
        _run_checked(
            ["open", "-a", application, str(path)],
            f"Could not open {path} with application '{application}'.",
        )
    except RuntimeError as exc:
        raise _missing_application_error(path, application) from exc

    return {
        "path": str(path),
        "application": application,
        "launcher": "open",
        "used_default_application": False,
    }


def _open_on_linux(path: Path, application: str | None) -> dict[str, Any]:
    if application:
        executable = _looks_like_existing_executable(application)
        if executable is None:
            raise _missing_application_error(path, application)
        command = [executable, str(path)]
        launcher = executable
    else:
        executable = shutil.which("xdg-open")
        if not executable:
            raise RuntimeError("Could not find 'xdg-open' for default application launching on this system.")
        command = [executable, str(path)]
        launcher = executable

    pid = _spawn(command)
    return {
        "path": str(path),
        "application": application,
        "launcher": launcher,
        "pid": pid,
        "used_default_application": application is None,
    }


def _open_on_windows(path: Path, application: str | None) -> dict[str, Any]:
    if application is None:
        os.startfile(str(path))
        return {
            "path": str(path),
            "application": None,
            "launcher": "os.startfile",
            "used_default_application": True,
        }

    executable = _looks_like_existing_executable(application)
    if executable is None:
        raise _missing_application_error(path, application)
    command = [executable, str(path)]

    kwargs: dict[str, Any] = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    }

    process = subprocess.Popen(command, **kwargs)
    return {
        "path": str(path),
        "application": application,
        "launcher": executable,
        "pid": process.pid,
        "used_default_application": False,
    }
