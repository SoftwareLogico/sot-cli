from __future__ import annotations

from pathlib import Path

from sot_cli.constants import BLOCKED_DEVICE_PATHS


def _is_blocked_device(file_path: str) -> bool:
    if file_path in BLOCKED_DEVICE_PATHS:
        return True
    if file_path.startswith("/proc/") and any(
        file_path.endswith(f"/fd/{n}") for n in ("0", "1", "2")
    ):
        return True
    return False


def _find_similar_file(file_path: str) -> str | None:
    """If file not found, look for a similar filename in the same directory."""
    parent = Path(file_path).parent
    name = Path(file_path).name.lower()
    if not parent.is_dir():
        return None
    try:
        for candidate in parent.iterdir():
            if candidate.name.lower() == name and candidate.name != Path(file_path).name:
                return str(candidate)
    except OSError:
        pass
    return None


def resolve_path(raw_path: str | Path, root_dir: Path) -> Path:
    candidate = Path(raw_path).expanduser()
    if candidate.is_absolute():
        return candidate.resolve()
    return (root_dir / candidate).resolve()
