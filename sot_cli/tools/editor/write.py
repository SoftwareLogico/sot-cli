from __future__ import annotations

from pathlib import Path
from typing import Any

from sot_cli.utils.text import _count_lines
from sot_cli.tools.utils.path_helpers import resolve_path
from sot_cli.tools.utils.validators import _require_string, _require_string_allow_empty
from sot_cli.tools.editor.text_utils import _match_line_endings


def execute_write_file(arguments: dict[str, Any], root_dir: Path) -> dict[str, Any]:
    raw_path = _require_string(arguments, "path")
    content = _require_string_allow_empty(arguments, "content")
    path = resolve_path(raw_path, root_dir)

    if path.exists() and path.is_dir():
        raise IsADirectoryError(f"Path is a directory, not a file: {path}.")

    operation = "create"
    previous_text_decodable = True
    previous_size_bytes = 0
    if path.exists():
        operation = "update"
        previous_size_bytes = path.stat().st_size
        try:

            with path.open("r", encoding="utf-8", newline="") as fh:
                previous_content = fh.read()
            if previous_content == content:
                return {
                    "path": str(path),
                    "status": "success",
                    "operation": "unchanged",
                    "size_bytes": previous_size_bytes,
                    "line_count": _count_lines(content),
                }
        
            content = _match_line_endings(previous_content, content)
        except UnicodeDecodeError:
            previous_text_decodable = False

    path.parent.mkdir(parents=True, exist_ok=True)

  
    with path.open("w", encoding="utf-8", newline="") as fh:
        fh.write(content)

    size_bytes = path.stat().st_size
    return {
        "path": str(path),
        "status": "success",
        "operation": operation,
        "size_bytes": size_bytes,
        "line_count": _count_lines(content),
        "previous_size_bytes": previous_size_bytes,
        "previous_text_decodable": previous_text_decodable,
    }
