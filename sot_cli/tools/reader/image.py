from __future__ import annotations

import base64
import io
import mimetypes
from pathlib import Path
from typing import Any

from sot_cli.tools.core import ToolPayload
from sot_cli.tools.utils.content_parts import _image_part, _text_part, _tool_meta_message


def read_image(path: Path, ext: str, size_bytes: int, supports_images: bool) -> ToolPayload:
    """Read image with normalization and supplemental multimodal blocks."""
    raw_bytes = path.read_bytes()
    mime = mimetypes.guess_type(path.name)[0] or f"image/{ext}"
    if ext == "jpg" or mime == "image/jpg":
        mime = "image/jpeg"

    original_width = None
    original_height = None
    display_width = None
    display_height = None
    output_bytes = raw_bytes

    try:
        from PIL import Image  # pyright: ignore[reportMissingImports]
        from PIL import ImageOps  # pyright: ignore[reportMissingImports]

        img = Image.open(path)
        original_width, original_height = img.size
        img = ImageOps.exif_transpose(img)
        display_width, display_height = img.size

        # Forzar el formato a JPEG o PNG para compatibilidad con APIs estrictas (LM Studio)
        if img.mode in ("RGBA", "P"):
            save_format = "PNG"
            mime = "image/png"
            save_kwargs: dict[str, Any] = {"optimize": True}
        else:
            if img.mode not in {"RGB", "L"}:
                img = img.convert("RGB")
            save_format = "JPEG"
            mime = "image/jpeg"
            save_kwargs: dict[str, Any] = {"quality": 92, "optimize": True}

        buffer = io.BytesIO()
        img.save(buffer, format=save_format, **save_kwargs)
        output_bytes = buffer.getvalue()
    except ImportError:
        pass  # No Pillow — send raw
    except Exception:
        pass  # Corrupt image or unsupported format — send raw

    b64 = base64.b64encode(output_bytes).decode("ascii")
    payload: dict[str, Any] = {
        "type": "image",
        "path": str(path),
        "mime_type": mime,
        "original_size_bytes": size_bytes,
        "processed_size_bytes": len(output_bytes),
    }
    if original_width is not None:
        payload["original_width"] = original_width
        payload["original_height"] = original_height
    if display_width is not None:
        payload["display_width"] = display_width
        payload["display_height"] = display_height

    supplemental_messages = []
    # Solo inyectar la imagen si el modelo realmente soporta visión
    if supports_images:
        supplemental_messages.append(
            _tool_meta_message(
                [
                    _text_part(f"Supplemental image content from read_text_file for {path}."),
                    _image_part(mime, b64),
                ]
            )
        )
    else:
        supplemental_messages.append(
            _tool_meta_message(
                [
                    _text_part(f"[Image file {path.name} omitted because the current model does not support vision.]"),
                ]
            )
        )

    return ToolPayload(payload=payload, supplemental_messages=supplemental_messages)
