from __future__ import annotations

import fnmatch
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from sot_cli.constants import (
    VCS_DIRS,
    FALLBACK_SEARCH_DEFAULT_HEAD_LIMIT,
    FALLBACK_SEARCH_MAX_LINE_LENGTH,
    FALLBACK_SEARCH_TIMEOUT_SECONDS,
)
from sot_cli.tools.utils.path_helpers import resolve_path
from sot_cli.tools.utils.validators import _require_string

# Common extension-to-type mapping for the `type` filter when using the Python fallback.
_TYPE_EXTENSIONS: dict[str, tuple[str, ...]] = {
    "py": (".py", ".pyi"),
    "js": (".js", ".mjs", ".cjs"),
    "ts": (".ts", ".mts", ".cts", ".tsx"),
    "rust": (".rs",),
    "go": (".go",),
    "java": (".java",),
    "c": (".c", ".h"),
    "cpp": (".cpp", ".cc", ".cxx", ".hpp", ".hxx", ".h"),
    "cs": (".cs",),
    "rb": (".rb",),
    "php": (".php",),
    "swift": (".swift",),
    "kt": (".kt", ".kts"),
    "dart": (".dart",),
    "lua": (".lua",),
    "sh": (".sh", ".bash", ".zsh", ".bat", ".cmd"),
    "sql": (".sql",),
    "html": (".html", ".htm"),
    "css": (".css",),
    "json": (".json",),
    "yaml": (".yaml", ".yml"),
    "toml": (".toml",),
    "xml": (".xml",),
    "md": (".md", ".markdown"),
    "txt": (".txt",),
    "vb": (".vb", ".bas", ".frm", ".cls", ".vbs"),
    "pascal": (".pas",),
    "perl": (".pl",),
    "asm": (".asm", ".s", ".inc"),
    "v": (".v",),
    "gradle": (".gradle",),
    "plist": (".plist",),
    "xcode": (".xcworkspacedata", ".pbxproj"),
}


def _normalize_boolean(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in ("true", "1", "yes")
    return bool(value)


def _normalize_int(value: Any, default: int | None) -> int | None:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_relative(line: str, root: str) -> str:
    """Convert absolute paths at the start of a ripgrep output line to relative paths."""
    colon_idx = line.find(":")
    if colon_idx > 0:
        file_part = line[:colon_idx]
        rest = line[colon_idx:]
        if file_part.startswith(root):
            file_part = file_part[len(root):].lstrip("/").lstrip("\\")
        return file_part + rest
    if line.startswith(root):
        return line[len(root):].lstrip("/").lstrip("\\")
    return line


# ---------------------------------------------------------------------------
# Python fallback when ripgrep is not installed
# ---------------------------------------------------------------------------

def _python_search(
    pattern: str,
    search_path: Path,
    *,
    glob_filter: str | None,
    file_type: str | None,
    output_mode: str,
    case_insensitive: bool,
    show_line_numbers: bool,
    context_before: int | None,
    context_after: int | None,
    context: int | None,
    multiline: bool,
    max_line_length: int,
) -> list[str]:
    """Pure-Python grep fallback. Returns raw output lines similar to ripgrep."""
    flags = re.IGNORECASE if case_insensitive else 0
    if multiline:
        flags |= re.DOTALL | re.MULTILINE

    try:
        regex = re.compile(pattern, flags)
    except re.error as exc:
        raise RuntimeError(f"Invalid regex pattern: {exc}") from exc

    # Build extension filter from glob/type
    allowed_extensions: set[str] | None = None
    glob_patterns: list[str] | None = None

    if file_type and file_type in _TYPE_EXTENSIONS:
        allowed_extensions = set(_TYPE_EXTENSIONS[file_type])
    if glob_filter:
        glob_patterns = []
        for token in glob_filter.split():
            if "{" in token and "}" in token:
                # Expand simple brace patterns like "*.{ts,tsx}"
                prefix, brace_content = token.split("{", 1)
                suffix_part = brace_content.rstrip("}")
                for ext in suffix_part.split(","):
                    glob_patterns.append(prefix + ext.strip())
            else:
                for sub in token.split(","):
                    sub = sub.strip()
                    if sub:
                        glob_patterns.append(sub)

    ctx_before = context if context is not None else (context_before or 0)
    ctx_after = context if context is not None else (context_after or 0)

    results: list[str] = []
    root_str = str(search_path)

    for dirpath, dirnames, filenames in os.walk(search_path):
        # Skip VCS directories
        dirnames[:] = [d for d in dirnames if d not in VCS_DIRS]

        for filename in filenames:
            filepath = os.path.join(dirpath, filename)

            # Extension filter
            if allowed_extensions:
                _, ext = os.path.splitext(filename)
                if ext.lower() not in allowed_extensions:
                    continue

            # Glob filter
            if glob_patterns:
                rel_path = os.path.relpath(filepath, search_path)
                if not any(fnmatch.fnmatch(filename, g) or fnmatch.fnmatch(rel_path, g) for g in glob_patterns):
                    continue

            try:
                with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
            except (OSError, PermissionError):
                continue

            rel_path = os.path.relpath(filepath, root_str)

            if multiline:
                # Multiline: just check if pattern matches anywhere
                if output_mode == "files_with_matches":
                    if regex.search(content):
                        results.append(rel_path)
                elif output_mode == "count":
                    count = len(regex.findall(content))
                    if count > 0:
                        results.append(f"{rel_path}:{count}")
                else:  # content
                    for m in regex.finditer(content):
                        line_num = content[:m.start()].count("\n") + 1
                        matched_line = content[content.rfind("\n", 0, m.start()) + 1:content.find("\n", m.end())]
                        if max_line_length > 0 and len(matched_line) > max_line_length:
                            matched_line = matched_line[:max_line_length]
                        if show_line_numbers:
                            results.append(f"{rel_path}:{line_num}:{matched_line}")
                        else:
                            results.append(f"{rel_path}:{matched_line}")
                continue

            # Line-by-line search
            lines = content.splitlines()

            if output_mode == "files_with_matches":
                if any(regex.search(line) for line in lines):
                    results.append(rel_path)
                continue

            if output_mode == "count":
                count = sum(1 for line in lines if regex.search(line))
                if count > 0:
                    results.append(f"{rel_path}:{count}")
                continue

            # Content mode with context
            match_indices = [i for i, line in enumerate(lines) if regex.search(line)]
            if not match_indices:
                continue

            # Build context ranges
            shown: set[int] = set()
            for idx in match_indices:
                for c in range(max(0, idx - ctx_before), min(len(lines), idx + ctx_after + 1)):
                    shown.add(c)

            prev_idx = -2
            for idx in sorted(shown):
                if idx > prev_idx + 1 and prev_idx >= 0:
                    results.append("--")  # separator like ripgrep
                line_text = lines[idx]
                if max_line_length > 0 and len(line_text) > max_line_length:
                    line_text = line_text[:max_line_length]
                if show_line_numbers:
                    sep = ":" if idx in match_indices else "-"
                    results.append(f"{rel_path}{sep}{idx + 1}{sep}{line_text}")
                else:
                    results.append(f"{rel_path}:{line_text}")
                prev_idx = idx

    return results


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def execute_search_code(
    arguments: dict[str, Any],
    root_dir: Path,
    *,
    default_head_limit: int = FALLBACK_SEARCH_DEFAULT_HEAD_LIMIT,
    max_line_length: int = FALLBACK_SEARCH_MAX_LINE_LENGTH,
    timeout_seconds: int = FALLBACK_SEARCH_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Search for text patterns across files using ripgrep (or Python fallback).

    The three keyword-only settings (`default_head_limit`, `max_line_length`,
    `timeout_seconds`) are authoritative values taken from `[tools]` in
    `sot.toml`; the `FALLBACK_SEARCH_*` constants are kept only for direct
    library usage or tests that bypass the registry.
    """
    pattern = _require_string(arguments, "pattern", strip=False)
    raw_path = arguments.get("path")
    glob_filter = arguments.get("glob")
    file_type = arguments.get("type")
    output_mode = arguments.get("output_mode", "files_with_matches")
    context_before = _normalize_int(arguments.get("context_before"), None)
    context_after = _normalize_int(arguments.get("context_after"), None)
    context = _normalize_int(arguments.get("context"), None)
    show_line_numbers = _normalize_boolean(arguments.get("show_line_numbers"), True)
    case_insensitive = _normalize_boolean(arguments.get("case_insensitive"), False)
    head_limit = _normalize_int(arguments.get("head_limit"), None)
    offset = _normalize_int(arguments.get("offset"), 0) or 0
    multiline = _normalize_boolean(arguments.get("multiline"), False)

    search_path = resolve_path(raw_path, root_dir) if raw_path else root_dir

    rg_bin = shutil.which("rg")

    if rg_bin is not None:
        raw_lines = _search_with_ripgrep(
            rg_bin, pattern, search_path,
            glob_filter=glob_filter, file_type=file_type, output_mode=output_mode,
            case_insensitive=case_insensitive, show_line_numbers=show_line_numbers,
            context_before=context_before, context_after=context_after,
            context=context, multiline=multiline,
            max_line_length=max_line_length, timeout_seconds=timeout_seconds,
        )
    else:
        raw_lines = _python_search(
            pattern, search_path,
            glob_filter=glob_filter, file_type=file_type, output_mode=output_mode,
            case_insensitive=case_insensitive, show_line_numbers=show_line_numbers,
            context_before=context_before, context_after=context_after,
            context=context, multiline=multiline,
            max_line_length=max_line_length,
        )

    root_str = str(search_path)
    if not root_str.endswith("/") and not root_str.endswith("\\"):
        root_str += "/"

    effective_limit = head_limit if head_limit is not None else default_head_limit
    if effective_limit == 0:
        limited = raw_lines[offset:]
        was_truncated = False
    else:
        limited = raw_lines[offset: offset + effective_limit]
        was_truncated = (len(raw_lines) - offset) > effective_limit

    relative_lines = [_to_relative(line, root_str) for line in limited]

    pagination: dict[str, Any] = {}
    if was_truncated:
        pagination["truncated"] = True
        pagination["head_limit"] = effective_limit
    if offset > 0:
        pagination["offset"] = offset

    if output_mode == "content":
        content_text = "\n".join(relative_lines)
        return {
            "mode": "content",
            "content": content_text,
            "line_count": len(relative_lines),
            "total_result_lines": len(raw_lines),
            **pagination,
        }

    if output_mode == "count":
        total_matches = 0
        file_count = 0
        for line in relative_lines:
            colon_idx = line.rfind(":")
            if colon_idx > 0:
                try:
                    total_matches += int(line[colon_idx + 1:])
                    file_count += 1
                except ValueError:
                    pass
        return {
            "mode": "count",
            "content": "\n".join(relative_lines),
            "match_count": total_matches,
            "file_count": file_count,
            **pagination,
        }

    # files_with_matches (default)
    return {
        "mode": "files_with_matches",
        "files": relative_lines,
        "file_count": len(relative_lines),
        "total_matches": len(raw_lines),
        **pagination,
    }


def _search_with_ripgrep(
    rg_bin: str,
    pattern: str,
    search_path: Path,
    *,
    glob_filter: str | None,
    file_type: str | None,
    output_mode: str,
    case_insensitive: bool,
    show_line_numbers: bool,
    context_before: int | None,
    context_after: int | None,
    context: int | None,
    multiline: bool,
    max_line_length: int,
    timeout_seconds: int,
) -> list[str]:
    """Run ripgrep and return raw output lines."""
    args: list[str] = [rg_bin, "--hidden"]

    for d in VCS_DIRS:
        args.extend(["--glob", f"!{d}"])


    if multiline:
        args.extend(["-U", "--multiline-dotall"])

    if case_insensitive:
        args.append("-i")

    if output_mode == "files_with_matches":
        args.append("-l")
    elif output_mode == "count":
        args.append("-c")

    if show_line_numbers and output_mode == "content":
        args.append("-n")

    if output_mode == "content":
        if context is not None:
            args.extend(["-C", str(context)])
        else:
            if context_before is not None:
                args.extend(["-B", str(context_before)])
            if context_after is not None:
                args.extend(["-A", str(context_after)])

    if pattern.startswith("-"):
        args.extend(["-e", pattern])
    else:
        args.append(pattern)

    if file_type:
        args.extend(["--type", file_type])

    if glob_filter:
        for token in glob_filter.split():
            if "{" in token and "}" in token:
                args.extend(["--glob", token])
            else:
                for sub in token.split(","):
                    sub = sub.strip()
                    if sub:
                        args.extend(["--glob", sub])

    args.append(str(search_path))

    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout_seconds,
        )
    except FileNotFoundError:
        raise RuntimeError("ripgrep binary not found at resolved path")
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"search timed out after {timeout_seconds}s. "
            "Try a narrower pattern, glob filter, or smaller search path."
        )

    if proc.returncode > 1:
        error_msg = proc.stderr.strip() or f"ripgrep exited with code {proc.returncode}"
        raise RuntimeError(error_msg)

    return proc.stdout.splitlines() if proc.stdout else []
