from __future__ import annotations

from fnmatch import fnmatchcase
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sot_cli.tools.utils.path_helpers import resolve_path
from sot_cli.tools.utils.validators import _require_string


_SUPPORTED_KINDS = {
    "file",
    "directory",
    "symlink",
    "symlink_file",
    "symlink_directory",
}


def _normalize_optional_string(arguments: dict[str, Any], key: str) -> str | None:
    value = arguments.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    normalized = value.strip()
    return normalized or None


def _normalize_non_negative_int(value: Any, field_name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a non-negative integer")
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a non-negative integer") from exc
    if normalized < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")
    return normalized


def _normalize_extensions(value: Any) -> set[str] | None:
    if value is None:
        return None

    raw_values: list[str] = []
    if isinstance(value, str):
        raw_values = [part.strip() for part in value.split(",")]
    elif isinstance(value, list):
        for item in value:
            if not isinstance(item, str):
                raise ValueError("extensions must be a string or list of strings")
            raw_values.extend(part.strip() for part in item.split(","))
    else:
        raise ValueError("extensions must be a string or list of strings")

    normalized: set[str] = set()
    for item in raw_values:
        if not item:
            continue
        extension = item.lower()
        if not extension.startswith("."):
            extension = f".{extension}"
        normalized.add(extension)

    return normalized or None


def _normalize_kind(arguments: dict[str, Any]) -> str | None:
    value = arguments.get("kind")
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("kind must be a string")
    normalized = value.strip().lower()
    if normalized not in _SUPPORTED_KINDS:
        supported = ", ".join(sorted(_SUPPORTED_KINDS))
        raise ValueError(f"kind must be one of: {supported}")
    return normalized


def _iso_timestamp(timestamp: float | None) -> str | None:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def _matches_kind(entry_kind: str, kind_filter: str | None) -> bool:
    if kind_filter is None:
        return True
    if kind_filter == "symlink":
        return entry_kind.startswith("symlink")
    return entry_kind == kind_filter


def _matches_glob(value: str, pattern: str) -> bool:
    return fnmatchcase(value.lower(), pattern.lower())


def _split_keywords(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _looks_like_text_file(path: Path) -> bool:
    try:
        chunk = path.read_bytes()[:32768]
    except OSError:
        return False

    if not chunk:
        return True
    if b"\x00" in chunk:
        return False
    try:
        chunk.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _search_keywords_in_text_file(
    path: Path,
    keywords: list[str],
    *,
    case_sensitive: bool,
    max_bytes: int | None,
) -> tuple[list[str] | None, str | None]:
    try:
        size_bytes = path.stat().st_size
    except OSError:
        return None, "stat_error"

    if isinstance(max_bytes, int) and max_bytes > 0 and size_bytes > max_bytes:
        return None, "too_large"

    if not _looks_like_text_file(path):
        return None, "not_text"

    try:
        content = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None, "read_error"

    if case_sensitive:
        matched = [keyword for keyword in keywords if keyword in content]
    else:
        content_lower = content.lower()
        matched = [keyword for keyword in keywords if keyword.lower() in content_lower]

    return matched, None


def _matches_filters(entry: dict[str, Any], filters: dict[str, Any]) -> bool:
    name = str(entry["name"])
    relative_path = str(entry["relative_path"])
    absolute_path = str(entry["path"])
    extension = str(entry["extension"])
    kind = str(entry["kind"])
    size_bytes = entry.get("size_bytes")

    name_contains = filters.get("name_contains")
    if isinstance(name_contains, str):
        # Support comma-separated keywords and treat them as OR.
        keywords = [k.lower() for k in _split_keywords(name_contains)]
        if keywords and not any(kw in name.lower() for kw in keywords):
            return False

    path_contains = filters.get("path_contains")
    if isinstance(path_contains, str):
        path_contains_lower = path_contains.lower()
        if path_contains_lower not in relative_path.lower() and path_contains_lower not in absolute_path.lower():
            return False

    name_pattern = filters.get("name_pattern")
    if isinstance(name_pattern, str) and not _matches_glob(name, name_pattern):
        return False

    path_pattern = filters.get("path_pattern")
    if isinstance(path_pattern, str):
        if not _matches_glob(relative_path, path_pattern) and not _matches_glob(absolute_path, path_pattern):
            return False

    extensions = filters.get("extensions")
    if isinstance(extensions, set) and extension.lower() not in extensions:
        return False

    kind_filter = filters.get("kind")
    if isinstance(kind_filter, str) and not _matches_kind(kind, kind_filter):
        return False

    min_size_bytes = filters.get("min_size_bytes")
    if isinstance(min_size_bytes, int) and isinstance(size_bytes, int) and size_bytes < min_size_bytes:
        return False

    max_size_bytes = filters.get("max_size_bytes")
    if isinstance(max_size_bytes, int) and isinstance(size_bytes, int) and size_bytes > max_size_bytes:
        return False

    return True


def execute_list_dir(arguments: dict[str, Any], root_dir: Path) -> dict[str, Any]:
    path = resolve_path(_require_string(arguments, "path"), root_dir)
    recursive = arguments.get("recursive", False)
    follow_symlinks = bool(arguments.get("follow_symlinks", False))
    content_contains = _normalize_optional_string(arguments, "content_contains")
    content_case_sensitive = bool(arguments.get("content_case_sensitive", False))
    content_max_bytes = _normalize_non_negative_int(arguments.get("content_max_bytes"), "content_max_bytes")
    filters = {
        "name_contains": _normalize_optional_string(arguments, "name_contains"),
        "path_contains": _normalize_optional_string(arguments, "path_contains"),
        "name_pattern": _normalize_optional_string(arguments, "name_pattern"),
        "path_pattern": _normalize_optional_string(arguments, "path_pattern"),
        "extensions": _normalize_extensions(arguments.get("extensions")),
        "kind": _normalize_kind(arguments),
        "min_size_bytes": _normalize_non_negative_int(arguments.get("min_size_bytes"), "min_size_bytes"),
        "max_size_bytes": _normalize_non_negative_int(arguments.get("max_size_bytes"), "max_size_bytes"),
    }
    if not path.is_dir():
        raise NotADirectoryError(f"Not a directory: {path}")
    if (
        isinstance(filters["min_size_bytes"], int)
        and isinstance(filters["max_size_bytes"], int)
        and filters["min_size_bytes"] > filters["max_size_bytes"]
    ):
        raise ValueError("min_size_bytes cannot be greater than max_size_bytes")

    entries: list[dict[str, Any]] = []
    visited_dirs: set[Path] = set()
    scanned_entry_count = 0
    content_keywords = _split_keywords(content_contains) if isinstance(content_contains, str) else []
    content_scan_stats = {
        "enabled": bool(content_keywords),
        "searched_files": 0,
        "matched_files": 0,
        "skipped_non_text": 0,
        "skipped_too_large": 0,
        "skipped_errors": 0,
    }

    def classify(candidate: Path) -> str:
        if candidate.is_symlink():
            try:
                if candidate.resolve().is_dir():
                    return "symlink_directory"
            except OSError:
                return "symlink"
            return "symlink_file"
        return "directory" if candidate.is_dir() else "file"

    def collect(directory: Path, depth: int) -> None:
        resolved_directory = directory.resolve()
        if resolved_directory in visited_dirs:
            return
        visited_dirs.add(resolved_directory)

        try:
            children = list(directory.iterdir())
        except (PermissionError, OSError):
            # Silently skip directories we cannot read (e.g. .Trashes on macOS,
            # System Volume Information on Windows, sockets, broken mounts).
            return

        for child in sorted(children, key=lambda item: item.name.lower()):
            nonlocal scanned_entry_count
            scanned_entry_count += 1

            try:
                stat_result = (
                    os.stat(child, follow_symlinks=follow_symlinks)
                    if follow_symlinks
                    else os.lstat(child)
                )
                size_bytes = stat_result.st_size
                modified_at = _iso_timestamp(stat_result.st_mtime)
                accessed_at = _iso_timestamp(getattr(stat_result, "st_atime", None))
                created_timestamp = getattr(stat_result, "st_birthtime", None)
                if created_timestamp is None:
                    created_timestamp = getattr(stat_result, "st_ctime", None)
                created_at = _iso_timestamp(created_timestamp)
            except OSError:
                size_bytes = None
                modified_at = None
                accessed_at = None
                created_at = None

            kind = classify(child)
            extension = child.suffix.lower()

            # Probe directory readability up-front. If the OS will deny our
            # scandir() call, mark the entry so the LLM sees [Blocked by OS]
            # in the summary and we skip recursion further down.
            blocked_by_os = False
            if kind in {"directory", "symlink_directory"}:
                try:
                    with os.scandir(child):
                        pass
                except (PermissionError, OSError):
                    blocked_by_os = True

            entry = {
                "name": child.name,
                "relative_path": child.relative_to(path).as_posix(),
                "path": str(child),
                "kind": kind,
                "depth": depth,
                "extension": extension,
                "extension_name": extension.lstrip("."),
                "hidden": child.name.startswith("."),
                "is_hidden": child.name.startswith("."),
                "symlink": child.is_symlink(),
                "is_symlink": child.is_symlink(),
                "size_bytes": size_bytes,
                "modified": modified_at,
                "modified_at": modified_at,
                "accessed": accessed_at,
                "accessed_at": accessed_at,
                "created": created_at,
                "created_at": created_at,
                "blocked_by_os": blocked_by_os,
            }
            if child.is_symlink():
                try:
                    entry["symlink_target"] = str(child.resolve(strict=True))
                except OSError:
                    entry["symlink_target"] = None

            if _matches_filters(entry, filters):
                if content_keywords:
                    if entry["kind"] in {"file", "symlink_file"}:
                        content_scan_stats["searched_files"] += 1
                        matched_keywords, skip_reason = _search_keywords_in_text_file(
                            child,
                            content_keywords,
                            case_sensitive=content_case_sensitive,
                            max_bytes=content_max_bytes,
                        )
                        if skip_reason == "not_text":
                            content_scan_stats["skipped_non_text"] += 1
                        elif skip_reason == "too_large":
                            content_scan_stats["skipped_too_large"] += 1
                        elif skip_reason in {"stat_error", "read_error"}:
                            content_scan_stats["skipped_errors"] += 1

                        if isinstance(matched_keywords, list) and matched_keywords:
                            entry["content_match"] = True
                            entry["content_matched_keywords"] = matched_keywords
                            entries.append(entry)
                            content_scan_stats["matched_files"] += 1
                else:
                    entries.append(entry)

            should_recurse = child.is_dir()
            if child.is_symlink() and not follow_symlinks:
                should_recurse = False
            # Skip directories the OS already told us we can't read — saves a
            # redundant iterdir() that would just return [] anyway.
            if should_recurse and not blocked_by_os and recursive:
                collect(child, depth + 1)

    collect(path, 1)

    normalized_filters = {
        key: sorted(value) if isinstance(value, set) else value
        for key, value in filters.items()
        if value is not None
    }
    if content_contains:
        normalized_filters["content_contains"] = content_contains
        normalized_filters["content_case_sensitive"] = content_case_sensitive
    if isinstance(content_max_bytes, int):
        normalized_filters["content_max_bytes"] = content_max_bytes

    kind_counts: dict[str, int] = {}
    for entry in entries:
        kind = str(entry["kind"])
        kind_counts[kind] = kind_counts.get(kind, 0) + 1

    return {
        "path": str(path),
        "recursive": bool(recursive),
        "include_hidden": True,
        "follow_symlinks": follow_symlinks,
        "search_mode": bool(normalized_filters),
        "filters": normalized_filters,
        "scanned_entry_count": scanned_entry_count,
        "entry_count": len(entries),
        "kind_counts": kind_counts,
        "content_scan": content_scan_stats,
        "entries": entries,
    }
