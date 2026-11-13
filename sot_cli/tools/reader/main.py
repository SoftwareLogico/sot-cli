from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any

from sot_cli.tools.core import ToolPayload
from sot_cli.constants import (
    AUDIO_EXTENSIONS,
    ARCHIVE_EXTENSIONS,
    ARCHIVE_HINTS,
    BINARY_EXTENSIONS,
    IMAGE_EXTENSIONS,
    NOTEBOOK_EXTENSIONS,
    PDF_EXTENSIONS,
    VIDEO_EXTENSIONS,
)
from sot_cli.tools.reader.image import read_image
from sot_cli.tools.reader.media import read_audio, read_video
from sot_cli.tools.reader.notebook import read_notebook
from sot_cli.tools.reader.pdf import read_pdf
from sot_cli.tools.utils.formatters import _file_mtime_ns
from sot_cli.tools.utils.path_helpers import _find_similar_file, _is_blocked_device
from sot_cli.tools.utils.validators import _normalize_pages_argument, _require_string


_KNOWN_BINARY_OR_MEDIA_EXTS: frozenset[str] = frozenset(
    {ext for grp in (
        BINARY_EXTENSIONS, IMAGE_EXTENSIONS, PDF_EXTENSIONS,
        NOTEBOOK_EXTENSIONS, AUDIO_EXTENSIONS, VIDEO_EXTENSIONS,
    ) for ext in grp}
)


def _is_media_or_binary(path: Path) -> bool:
    return path.suffix.lower().lstrip(".") in _KNOWN_BINARY_OR_MEDIA_EXTS


def _read_text_file(path: Path) -> tuple[str, int]:
    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(
            "Cannot decode file as UTF-8 text. The file may be binary. "
            "Use run_command with xxd or file to inspect it."
        ) from exc
    total_lines = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
    return content, total_lines


def execute_read_many_files(
    arguments: dict[str, Any],
    root_dir: Path,
    read_cache: dict,
    binary_check_size: int,
    supports_images: bool,
    supports_pdf: bool,
    supports_audio: bool,
    supports_video: bool,
    file_unchanged_stub: str,
    sot_state: Any = None,
    file_in_sot_stub: str | None = None,
    context_info: dict[str, Any] | None = None,
    max_readable_file_tokens: int = 64000,
) -> ToolPayload:
    files = arguments.get("files")
    if not isinstance(files, list) or not files:
        raise ValueError("The files parameter must be a non-empty array.")

    results: list[dict[str, Any]] = []
    supplemental_messages: list[dict[str, Any]] = []
    _context_warnings: list[str] = []
    success_count = 0
    error_count = 0

    for index, entry in enumerate(files):
        if not isinstance(entry, dict):
            results.append({
                "ok": False,
                "path": f"[index {index}]",
                "error": "Each files entry must be an object.",
            })
            error_count += 1
            continue

        raw_path = entry.get("path")
        path_label = raw_path if isinstance(raw_path, str) and raw_path.strip() else f"[index {index}]"

        try:
            force = bool(entry.get("force", False))
            if not force:
                from sot_cli.tools.utils.path_helpers import resolve_path
                check_path = resolve_path(raw_path, root_dir)
                if check_path.is_file() and not _is_media_or_binary(check_path):
                    content = check_path.read_text(encoding="utf-8")
                    from sot_cli.sot import _estimate_tokens
                    rough_tokens = _estimate_tokens(content)
                    if rough_tokens is None:
                        rough_tokens = len(content) // 4
                    remaining = context_info.get("estimated_remaining") if context_info else None
                    context_max = context_info.get("context_length") if context_info else None

                    if max_readable_file_tokens > 0 and rough_tokens > max_readable_file_tokens:
                        remaining_text = f" ({remaining:,} free)" if remaining is not None else ""
                        msg = (
                            f"File '{path_label}' is ~{rough_tokens:,} tokens, which is a large file"
                            f"{remaining_text}. Ask the user if they really want to read this entire file, "
                            f"use search_code for targeted exploration, or pass 'force': true in the "
                            f"read_files entry to read it anyway."
                        )
                        if remaining is not None and rough_tokens > remaining:
                            pct = int((rough_tokens / max(remaining + rough_tokens, 1)) * 100)
                            msg = (
                                f"File '{path_label}' is ~{rough_tokens:,} tokens. "
                                f"Your remaining context is ~{remaining:,} tokens"
                                + (f" (max: {context_max:,})" if context_max else "")
                                + f". This would consume ~{pct}% of available space. "
                                f"Use search_code to explore specific sections, ask the user for confirmation, or pass "
                                f"'force': true in the read_files entry to read it anyway."
                            )
                        _context_warnings.append(msg)
                        supplemental_messages.append({
                            "role": "user",
                            "content": f"[CONTEXT WARNING] {msg}",
                        })
                        # Don't read - file too large, warn instead
                        continue

            raw_result = execute_read_text_file(
                entry,
                root_dir=root_dir,
                read_cache=read_cache,
                binary_check_size=binary_check_size,
                supports_images=supports_images,
                supports_pdf=supports_pdf,
                supports_audio=supports_audio,
                supports_video=supports_video,
                file_unchanged_stub=file_unchanged_stub,
                sot_state=sot_state,
                file_in_sot_stub=file_in_sot_stub,
            )
        except Exception as exc:
            results.append({
                "ok": False,
                "path": path_label,
                "error": str(exc),
            })
            error_count += 1
            continue

        if isinstance(raw_result, ToolPayload):
            payload = dict(raw_result.payload)
            supplemental_messages.extend(raw_result.supplemental_messages)
        else:
            payload = dict(raw_result)

        if "type" not in payload:
            if "content" in payload:
                payload["type"] = "text"
            elif "warning" in payload:
                payload["type"] = "text"

        results.append({"ok": True, **payload})
        success_count += 1

    return ToolPayload(
        payload={
            "result_count": len(results),
            "success_count": success_count,
            "error_count": error_count,
            "results": results,
        },
        supplemental_messages=supplemental_messages,
        model_content=_build_read_result_content(results, error_count, _context_warnings),
    )


def _build_read_result_content(
    results: list[dict[str, Any]],
    error_count: int,
    context_warnings: list[str],
) -> str:
    parts: list[str] = []
    if context_warnings:
        parts.append("\n".join(context_warnings))
        parts.append("")
    success_count = sum(1 for r in results if r.get("ok"))
    total = len(results)
    parts.append(f"batch read {success_count}/{total} ok ({error_count} errors):")
    for r in results:
        if r.get("ok"):
            path = r.get("path", "?")
            lines = r.get("total_lines", "?")
            size = r.get("size_bytes", "?")
            parts.append(f"- read {path} ({lines} lines, {size} bytes) -> SoT")
        else:
            path = r.get("path", "?")
            err = r.get("error", "Unknown error")
            parts.append(f"- ERROR {path}: {err}")
    return "\n".join(parts)


def _raise_binary_error(ext: str, path: Path) -> None:
    hint = ARCHIVE_HINTS.get(ext)
    if hint is None and ext in ARCHIVE_EXTENSIONS:
        hint = f"Use run_command with appropriate archive tools for .{ext} files."
    if hint is not None:
        formatted = hint.replace("{path}", str(path))
        raise ValueError(
            f"Cannot read .{ext} archive directly. {formatted}"
        )
    raise ValueError(
        f"This tool cannot read binary files. The file appears to be a binary .{ext} file. "
        f"Use run_command with appropriate tools for binary file analysis."
    )


def _is_probably_binary(path: Path, binary_check_size: int) -> bool:
    mime_type, _ = mimetypes.guess_type(path.name)
    if mime_type:
        if mime_type.startswith("text/"):
            return False
        if mime_type in {
            "application/json",
            "application/xml",
            "application/javascript",
            "application/x-yaml",
            "application/toml",
            "application/x-sh",
            "application/x-httpd-php",
            "application/sql",
            "application/graphql",
            "application/ld+json",
            "application/x-perl",
            "application/x-ruby",
            "application/x-python",
            "application/x-lua",
        }:
            return False

    try:
        chunk = path.read_bytes()[:binary_check_size]
    except OSError:
        return False

    if not chunk:
        return False
    if b"\x00" in chunk:
        return True

    non_printable = 0
    for byte in chunk:
        if byte < 32 and byte not in {9, 10, 13}:
            non_printable += 1
    return non_printable / len(chunk) > 0.1


def execute_read_text_file(
    arguments: dict[str, Any],
    root_dir: Path,
    read_cache: dict,
    binary_check_size: int,
    supports_images: bool,
    supports_pdf: bool,
    supports_audio: bool,
    supports_video: bool,
    file_unchanged_stub: str,
    sot_state: Any = None,
    file_in_sot_stub: str | None = None,
) -> dict[str, Any]:
    from sot_cli.tools.utils.path_helpers import resolve_path

    raw_path = _require_string(arguments, "path")
    path = resolve_path(raw_path, root_dir)
    resolved = str(path)
    raw_pages = arguments.get("pages")
    pages = _normalize_pages_argument(raw_pages)
    password: str | None = arguments.get("password")

    # Block device paths that would hang
    if _is_blocked_device(resolved):
        raise ValueError(f"Cannot read '{raw_path}': this device file would block or produce infinite output.")

    # Check existence with similar-file suggestion
    if not path.exists():
        similar = _find_similar_file(resolved)
        msg = f"File does not exist. Note: file_path should be an absolute path. Current cwd is {root_dir}."
        if similar:
            msg += f" Did you mean {similar}?"
        raise FileNotFoundError(msg)

    if path.is_dir():
        raise IsADirectoryError(f"Path is a directory, not a file: {path}. Use list_dir instead.")

    ext = path.suffix.lower().lstrip(".")
    stat = path.stat()
    size_bytes = stat.st_size
    modified_ns = _file_mtime_ns(stat)

    if pages and ext not in PDF_EXTENSIONS:
        raise ValueError("The pages parameter is only valid for PDF files.")

    # ── SoT check (cross-round) ──
    # If the file is already tracked in the SoT and its disk mtime hasn't
    # changed, the model already sees it in the '=== SOURCE OF TRUTH ===' block.
    # Return a pointer stub instead of re-reading. Only applies to plain text
    # files (not PDF pages).
    if (
        sot_state is not None
        and file_in_sot_stub is not None
        and not pages
    ):
        tracked_mtimes = getattr(sot_state, "tracked_file_mtimes", None)
        tracked_files = getattr(sot_state, "tracked_files", None)
        if (
            isinstance(tracked_files, dict)
            and isinstance(tracked_mtimes, dict)
            and resolved in tracked_files
            and tracked_mtimes.get(resolved) == modified_ns
        ):
            return {
                "type": "file_in_sot",
                "path": resolved,
                "message": file_in_sot_stub,
            }

    # ── Cache check (same-round) ──
    cached = read_cache.get((resolved, pages))
    if cached is not None:
        cached_modified_ns, _ = cached
        if cached_modified_ns == modified_ns:
            return {
                "type": "file_unchanged",
                "path": resolved,
                "message": file_unchanged_stub,
            }

    def _store(result: dict[str, Any]) -> dict[str, Any]:
        read_cache[(resolved, pages)] = (modified_ns, result)
        return result

    # ── Image ──
    if ext in IMAGE_EXTENSIONS:
        return _store(read_image(path, ext, size_bytes, supports_images))

    # ── PDF ──
    if ext in PDF_EXTENSIONS:
        return _store(read_pdf(path, size_bytes, pages, password, supports_pdf, supports_images))

    # ── Notebook ──
    if ext in NOTEBOOK_EXTENSIONS:
        return _store(read_notebook(path, size_bytes, supports_images))

    # ── Audio ──
    if ext in AUDIO_EXTENSIONS:
        return _store(read_audio(path, ext, size_bytes, supports_audio))

    # ── Video ──
    if ext in VIDEO_EXTENSIONS:
        return _store(read_video(path, ext, size_bytes, supports_video))

    # ── Binary rejection ──
    if ext in BINARY_EXTENSIONS or _is_probably_binary(path, binary_check_size):
        _raise_binary_error(ext, path)

    content, total_lines = _read_text_file(path)

    if not content:
        return _store({
            "path": resolved,
            "warning": "The file exists but the contents are empty.",
            "content": "",
            "size_bytes": 0,
            "total_lines": total_lines,
            "modified_ns": modified_ns,
        })

    return _store({
        "path": resolved,
        "content": content,
        "size_bytes": size_bytes,
        "total_lines": total_lines,
        "modified_ns": modified_ns,
    })
