from __future__ import annotations

from typing import Any


def _text_part(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def _image_part(mime_type: str, base64_data: str) -> dict[str, Any]:
    return {
        "type": "image_url",
        "image_url": {"url": f"data:{mime_type};base64,{base64_data}"},
    }


def _audio_part(audio_format: str, base64_data: str) -> dict[str, Any]:
    return {
        "type": "input_audio",
        "input_audio": {
            "data": base64_data,
            "format": audio_format,
        },
    }


def _video_part(mime_type: str, base64_data: str) -> dict[str, Any]:
    return {
        "type": "video_url",
        "video_url": {"url": f"data:{mime_type};base64,{base64_data}"},
    }


def _file_part(filename: str, mime_type: str, base64_data: str) -> dict[str, Any]:
    return {
        "type": "file",
        "file": {
            "filename": filename,
            "file_data": f"data:{mime_type};base64,{base64_data}",
        },
    }


def _tool_meta_message(content: list[dict[str, Any]]) -> dict[str, Any]:
    return {"role": "user", "content": content}


def _append_text_part(parts: list[dict[str, Any]], text: str) -> None:
    if not text:
        return
    if parts and parts[-1].get("type") == "text":
        parts[-1]["text"] += "\n\n" + text
        return
    parts.append(_text_part(text))
