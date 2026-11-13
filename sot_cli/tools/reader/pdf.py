from __future__ import annotations

import base64
import io
import subprocess
from pathlib import Path
from typing import Any

from sot_cli.tools.core import ToolPayload
from sot_cli.tools.utils.content_parts import (
    _file_part,
    _image_part,
    _text_part,
    _tool_meta_message,
)


def _parse_pdf_page_range(pages: str | None, page_count: int | None) -> tuple[int, int] | None:
    if pages is None:
        return None

    if "-" in pages:
        start_raw, end_raw = pages.split("-", 1)
    else:
        start_raw, end_raw = pages, pages

    try:
        start = int(start_raw)
        end = int(end_raw)
    except ValueError as exc:
        raise ValueError("pages must be like '3' or '1-5'") from exc

    if start <= 0 or end <= 0 or end < start:
        raise ValueError("pages must define a valid positive page range")
    if page_count is not None and end > page_count:
        raise ValueError(f"pages range exceeds PDF page count ({page_count})")
    return start, end


def _is_pdf_encrypted(path: Path) -> bool:
    try:
        from pypdf import PdfReader  # pyright: ignore[reportMissingImports]

        reader = PdfReader(str(path))
        return reader.is_encrypted
    except Exception:
        return False


def _unlock_pdf(path: Path, password: str) -> bool:
    """Return True if the password successfully decrypts the PDF."""
    try:
        from pypdf import PdfReader  # pyright: ignore[reportMissingImports]

        reader = PdfReader(str(path))
        if reader.is_encrypted:
            result = reader.decrypt(password)
            return result != 0  # 0 means wrong password
        return True
    except Exception:
        return False


def _get_pdf_page_count(path: Path, password: str | None = None) -> int | None:
    try:
        from pypdf import PdfReader  # pyright: ignore[reportMissingImports]

        reader = PdfReader(str(path))
        if reader.is_encrypted and password:
            reader.decrypt(password)
        return len(reader.pages)
    except Exception:
        pass

    try:
        completed = subprocess.run(
            ["pdfinfo", str(path)],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if completed.returncode != 0:
            return None
        for line in completed.stdout.splitlines():
            if line.lower().startswith("pages:"):
                return int(line.split(":", 1)[1].strip())
    except Exception:
        return None

    return None


def _extract_pdf_text(path: Path, page_range: tuple[int, int] | None, password: str | None = None) -> str | None:
    try:
        from pypdf import PdfReader  # pyright: ignore[reportMissingImports]

        reader = PdfReader(str(path))
        if reader.is_encrypted and password:
            reader.decrypt(password)
        if page_range is None:
            selected_pages = reader.pages
        else:
            start, end = page_range
            selected_pages = reader.pages[start - 1:end]
        fragments: list[str] = []
        for page in selected_pages:
            page_text = page.extract_text() or ""
            fragments.append(page_text)
        return "\n\n".join(fragments)
    except Exception:
        pass

    command = ["pdftotext", "-layout"]
    if page_range is not None:
        start, end = page_range
        command.extend(["-f", str(start), "-l", str(end)])
    command.extend([str(path), "-"])
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if completed.returncode == 0:
            return completed.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None

    return None


def _render_pdf_pages(path: Path, page_range: tuple[int, int] | None) -> list[dict[str, Any]]:
    try:
        import pypdfium2 as pdfium  # pyright: ignore[reportMissingImports]
        from PIL import ImageOps  # pyright: ignore[reportMissingImports]

        pdf = pdfium.PdfDocument(str(path))
        total_pages = len(pdf)
        if page_range is None:
            start_page = 1
            end_page = total_pages
        else:
            start_page, end_page = page_range

        rendered_pages: list[dict[str, Any]] = []
        for page_number in range(start_page, end_page + 1):
            page = pdf[page_number - 1]
            pil_image = page.render(scale=2).to_pil()
            pil_image = ImageOps.exif_transpose(pil_image)
            buffer = io.BytesIO()
            pil_image.save(buffer, format="JPEG", quality=88, optimize=True)
            rendered_pages.append(
                {
                    "page_number": page_number,
                    "mime_type": "image/jpeg",
                    "width": pil_image.width,
                    "height": pil_image.height,
                    "base64": base64.b64encode(buffer.getvalue()).decode("ascii"),
                }
            )
        return rendered_pages
    except Exception:
        return []


def read_pdf(
    path: Path,
    size_bytes: int,
    pages: str | None,
    password: str | None,
    supports_pdf: bool,
    supports_images: bool,
) -> ToolPayload:
    """Read PDF and attach rendered page images as supplemental multimodal context when available."""
    # Detect encryption before doing anything else so the model gets a clear error.
    if _is_pdf_encrypted(path):
        if not password:
            raise ValueError(
                "This PDF is password-protected. "
                "Ask the user for the password and retry using the 'password' parameter."
            )
        if not _unlock_pdf(path, password):
            raise ValueError(
                "The provided password is incorrect for this PDF. "
                "Ask the user for the correct password and retry."
            )

    page_count = _get_pdf_page_count(path, password)
    page_range = _parse_pdf_page_range(pages, page_count)

    if supports_pdf and page_range is None:
        raw_bytes = path.read_bytes()
        payload = {
            "type": "pdf",
            "path": str(path),
            "size_bytes": size_bytes,
            "page_count": page_count,
            "delivery": "native_pdf",
        }
        supplemental_parts = [
            _text_part(f"Supplemental PDF content from read_text_file for {path}."),
            _file_part(path.name, "application/pdf", base64.b64encode(raw_bytes).decode("ascii")),
        ]
        return ToolPayload(
            payload=payload,
            supplemental_messages=[_tool_meta_message(supplemental_parts)],
        )

    text = _extract_pdf_text(path, page_range, password)
    rendered_pages = _render_pdf_pages(path, page_range)

    payload = {
        "type": "pdf",
        "path": str(path),
        "size_bytes": size_bytes,
        "page_count": page_count,
        "rendered_pages": len(rendered_pages),
        "text_extracted": text is not None,
        "delivery": "fallback_text_and_images" if rendered_pages else "fallback_text",
    }
    if pages:
        payload["pages"] = pages
    if text is not None and not text.strip():
        payload["warning"] = "No extractable text was found in the requested PDF pages."
    if text is None and not rendered_pages:
        raise ValueError(
            "Could not extract readable PDF content. Install pypdf, pypdfium2, or external PDF tools for this file."
        )

    supplemental_parts: list[dict[str, Any]] = [
        _text_part(
            f"Supplemental PDF content from read_text_file for {path}."
            + (f" Requested pages: {pages}." if pages else "")
        )
    ]
    if text:
        supplemental_parts.append(_text_part(text))
    for rendered_page in rendered_pages:
        supplemental_parts.append(
            _text_part(f"PDF page {rendered_page['page_number']} rendered image.")
        )
        supplemental_parts.append(
            _image_part(rendered_page["mime_type"], rendered_page["base64"])
        )

    return ToolPayload(
        payload=payload,
        supplemental_messages=[_tool_meta_message(supplemental_parts)],
    )
