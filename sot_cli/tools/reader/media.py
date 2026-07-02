from __future__ import annotations

import base64
import mimetypes
import subprocess
from pathlib import Path
from typing import Any

from sot_cli.tools.core import ToolPayload
from sot_cli.tools.utils.content_parts import (
    _audio_part,
    _text_part,
    _tool_meta_message,
    _video_part,
)


def _guess_mime_type(path: Path, fallback: str) -> str:
    mime_type = mimetypes.guess_type(path.name)[0]
    if isinstance(mime_type, str) and mime_type:
        return mime_type
    return fallback


def _guess_audio_format(ext: str, mime_type: str) -> str:
    normalized_ext = ext.lower().lstrip(".")
    if normalized_ext:
        return normalized_ext
    if "/" in mime_type:
        return mime_type.split("/", 1)[1].lower()
    return "wav"


def _probe_media_file(path: Path) -> dict[str, Any] | None:
    try:
        completed = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", str(path)],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if completed.returncode != 0 or not completed.stdout.strip():
            return None
        import json
        payload = json.loads(completed.stdout)
    except Exception:
        return None

    format_info = payload.get("format") if isinstance(payload.get("format"), dict) else {}
    streams = payload.get("streams") if isinstance(payload.get("streams"), list) else []
    return {
        "format_name": format_info.get("format_name"),
        "duration_seconds": format_info.get("duration"),
        "bit_rate": format_info.get("bit_rate"),
        "stream_count": len(streams),
        "streams": [
            {
                "codec_type": stream.get("codec_type"),
                "codec_name": stream.get("codec_name"),
                "width": stream.get("width"),
                "height": stream.get("height"),
                "sample_rate": stream.get("sample_rate"),
                "channels": stream.get("channels"),
            }
            for stream in streams[:8]
            if isinstance(stream, dict)
        ],
    }


def read_audio(path: Path, ext: str, size_bytes: int, supports_audio: bool) -> ToolPayload:
    mime_type = _guess_mime_type(path, f"audio/{ext}")
    raw_bytes = path.read_bytes()
    payload: dict[str, Any] = {
        "type": "audio",
        "path": str(path),
        "mime_type": mime_type,
        "size_bytes": size_bytes,
        "format": _guess_audio_format(ext, mime_type),
        "delivery": "native_audio" if supports_audio else "metadata_only",
    }
    media_probe = _probe_media_file(path)
    if media_probe:
        payload["probe"] = media_probe

    supplemental_messages: list[dict[str, Any]] = []
    supplemental_messages.append(
        _tool_meta_message(
            [
                _text_part(f"Supplemental audio content from read_text_file for {path}."),
                _audio_part(_guess_audio_format(ext, mime_type), base64.b64encode(raw_bytes).decode("ascii")),
            ]
        )
    )

    return ToolPayload(payload=payload, supplemental_messages=supplemental_messages)


def read_video(path: Path, ext: str, size_bytes: int, supports_video: bool) -> ToolPayload:
    mime_type = _guess_mime_type(path, f"video/{ext}")
    raw_bytes = path.read_bytes()
    payload: dict[str, Any] = {
        "type": "video",
        "path": str(path),
        "mime_type": mime_type,
        "size_bytes": size_bytes,
        "delivery": "native_video" if supports_video else "metadata_only",
    }
    media_probe = _probe_media_file(path)
    if media_probe:
        payload["probe"] = media_probe

    supplemental_messages: list[dict[str, Any]] = []
    supplemental_messages.append(
        _tool_meta_message(
            [
                _text_part(f"Supplemental video content from read_text_file for {path}."),
                _video_part(mime_type, base64.b64encode(raw_bytes).decode("ascii")),
            ]
        )
    )

    return ToolPayload(payload=payload, supplemental_messages=supplemental_messages)
