from __future__ import annotations


def _file_mtime_ns(stat_result) -> int:
    if hasattr(stat_result, "st_mtime_ns"):
        return int(stat_result.st_mtime_ns)
    return int(stat_result.st_mtime * 1_000_000_000)
