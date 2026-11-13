from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from sot_cli.tools.utils.path_helpers import resolve_path
from sot_cli.tools.utils.validators import _normalize_boolean, _require_string


def _path_kind_for_delete(path: Path) -> str:
    if path.is_symlink():
        return "symlink"
    if path.is_dir():
        return "directory"
    return "file"


def _safe_path_size(path: Path) -> int | None:
    try:
        if path.is_symlink():
            return path.lstat().st_size
        if path.is_file():
            return path.stat().st_size
        if path.is_dir():
            total = 0
            for child in path.rglob("*"):
                try:
                    if child.is_file() and not child.is_symlink():
                        total += child.stat().st_size
                except OSError:
                    continue
            return total
    except OSError:
        return None
    return None


def execute_delete_file(arguments: dict[str, Any], root_dir: Path) -> dict[str, Any]:
    raw_path = _require_string(arguments, "path")
    recursive = _normalize_boolean(arguments.get("recursive"), default=False, field_name="recursive")
    path = resolve_path(raw_path, root_dir)

    if not path.exists() and not path.is_symlink():
        raise FileNotFoundError(f"Path does not exist: {path}")

    kind = _path_kind_for_delete(path)
    size_bytes = _safe_path_size(path)

    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        if not recursive:
            raise ValueError("Deleting a directory requires recursive=true unless the path is a symlink.")
        shutil.rmtree(path)
    else:
        path.unlink()

    return {
        "path": str(path),
        "status": "success",
        "operation": "delete",
        "kind": kind,
        "recursive": recursive,
        "size_bytes": size_bytes,
    }
