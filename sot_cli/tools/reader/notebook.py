from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sot_cli.tools.core import ToolPayload
from sot_cli.tools.utils.content_parts import (
    _append_text_part,
    _image_part,
    _tool_meta_message,
)


def _extract_notebook_output_image(output: dict[str, Any]) -> dict[str, Any] | None:
    data = output.get("data")
    if not isinstance(data, dict):
        return None
    for mime_type in ("image/png", "image/jpeg", "image/webp", "image/gif"):
        image_data = data.get(mime_type)
        if isinstance(image_data, str):
            return {
                "mime_type": mime_type,
                "base64": "".join(image_data.split()),
            }
    return None


def _notebook_to_content_parts(
    notebook_path: str,
    cells: list[dict[str, Any]],
    supports_images: bool,
) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    _append_text_part(parts, f"Supplemental notebook content from read_text_file for {notebook_path}.")
    for cell in cells:
        header = f"Cell {cell['index']} [{cell['cell_type']}]"
        if cell.get("execution_count") is not None:
            header += f" execution_count={cell['execution_count']}"
        body = f"{header}\n{cell.get('source', '')}"
        _append_text_part(parts, body)
        for output in cell.get("outputs") or []:
            output_text = output.get("text")
            if isinstance(output_text, str) and output_text:
                _append_text_part(parts, f"Cell {cell['index']} output:\n{output_text}")
            image = output.get("image")
            if isinstance(image, dict):
                _append_text_part(parts, f"Cell {cell['index']} image output.")
                parts.append(_image_part(image["mime_type"], image["base64"]))
    return parts


def read_notebook(path: Path, size_bytes: int, supports_images: bool) -> ToolPayload:
    """Read Jupyter notebook and convert cells into rich supplemental content."""
    raw = path.read_text(encoding="utf-8")
    try:
        notebook = json.loads(raw)
    except json.JSONDecodeError:
        raise ValueError(f"Invalid JSON in notebook file: {path}")

    cells = notebook.get("cells", [])
    extracted = []
    rich_output_images = 0
    rich_output_text_blocks = 0
    language = str(((notebook.get("metadata") or {}).get("language_info") or {}).get("name") or "python")
    for i, cell in enumerate(cells):
        cell_type = cell.get("cell_type", "unknown")
        source = "".join(cell.get("source", []))
        outputs_raw = cell.get("outputs", [])
        outputs = []
        for out in outputs_raw:
            output_entry: dict[str, Any] = {"output_type": out.get("output_type", "unknown")}
            if "text" in out:
                text_output = "".join(out["text"])
                output_entry["text"] = text_output
                if text_output:
                    rich_output_text_blocks += 1
            elif "data" in out and "text/plain" in out["data"]:
                text_output = "".join(out["data"]["text/plain"])
                output_entry["text"] = text_output
                if text_output:
                    rich_output_text_blocks += 1
            image_payload = _extract_notebook_output_image(out)
            if image_payload is not None:
                output_entry["image"] = image_payload
                rich_output_images += 1
            if len(output_entry) > 1:
                outputs.append(output_entry)
        extracted.append({
            "index": i + 1,
            "cell_type": cell_type,
            "language": language if cell_type == "code" else "markdown",
            "execution_count": cell.get("execution_count"),
            "source": source,
            "outputs": outputs if outputs else None,
        })

    payload = {
        "type": "notebook",
        "path": str(path),
        "cell_count": len(extracted),
        "code_cell_count": sum(1 for cell in extracted if cell["cell_type"] == "code"),
        "markdown_cell_count": sum(1 for cell in extracted if cell["cell_type"] == "markdown"),
        "output_text_blocks": rich_output_text_blocks,
        "output_image_blocks": rich_output_images,
        "size_bytes": size_bytes,
    }

    supplemental_parts = _notebook_to_content_parts(str(path), extracted, supports_images)
    supplemental_messages = []
    if supplemental_parts:
        supplemental_messages.append(_tool_meta_message(supplemental_parts))

    return ToolPayload(payload=payload, supplemental_messages=supplemental_messages)
