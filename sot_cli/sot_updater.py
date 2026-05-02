"""Sot-cli structure updater — merge sot.toml with sot.example.toml safely.

Adds new keys from the example, removes keys that no longer exist in the
example, and **never** overwrites user values. Always creates a timestamped
backup before writing.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
import shutil
from datetime import datetime


def _deep_merge_structure(example: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
    """Merge two TOML dicts: example provides the structure, user provides values.

    - Keys in example that exist in user → keep user's value.
    - Keys in example that are NEW → use example's default.
    - Keys ONLY in user (not in example) → PRESERVED (never dropped).
    - Nested dicts are merged recursively.
    """
    result: dict[str, Any] = dict(user)  # Start with all user keys preserved
    for key, example_value in example.items():
        if key in result:
            user_value = result[key]
            if isinstance(example_value, dict) and isinstance(user_value, dict):
                result[key] = _deep_merge_structure(example_value, user_value)
            # else: keep user's scalar value — don't overwrite
        else:
            # New key from example → add with example default
            result[key] = example_value
    return result


def _toml_value(value: Any) -> str:
    """Serialize a single TOML value."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        # Escape backslashes and quotes
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    if isinstance(value, list):
        items = ", ".join(_toml_value(v) for v in value)
        return f"[{items}]"
    return f'"{value}"'


def _write_table(lines: list[str], key_path: str, data: dict[str, Any], indent: str = "") -> None:
    """Append a [table] header and its key-value pairs to lines."""
    # Separate simple values from nested tables
    simple: dict[str, Any] = {}
    nested: dict[str, dict[str, Any]] = {}
    for k, v in data.items():
        if isinstance(v, dict):
            nested[k] = v
        else:
            simple[k] = v

    if simple:
        lines.append(f"\n[{key_path}]")
        for k, v in simple.items():
            lines.append(f"{k} = {_toml_value(v)}")

    for k, v in nested.items():
        _write_table(lines, f"{key_path}.{k}", v)


def _toml_dumps(data: dict[str, Any]) -> str:
    """Serialize a nested dict to a TOML string."""
    lines: list[str] = []

    # Top-level simple keys
    top_simple: dict[str, Any] = {}
    top_tables: dict[str, dict[str, Any]] = {}
    for k, v in data.items():
        if isinstance(v, dict):
            top_tables[k] = v
        else:
            top_simple[k] = v

    for k, v in top_simple.items():
        lines.append(f"{k} = {_toml_value(v)}")

    for k, v in top_tables.items():
        _write_table(lines, k, v)

    return "\n".join(lines) + "\n"


def _diff_keys(old: dict[str, Any], new: dict[str, Any], prefix: str = "") -> tuple[list[str], list[str]]:
    """Return (added_keys, removed_keys) between two dicts."""
    added: list[str] = []
    removed: list[str] = []
    old_keys = set(old.keys())
    new_keys = set(new.keys())

    for k in new_keys - old_keys:
        path = f"{prefix}.{k}" if prefix else k
        added.append(path)

    for k in old_keys - new_keys:
        path = f"{prefix}.{k}" if prefix else k
        removed.append(path)

    # Recurse into shared dict keys
    for k in old_keys & new_keys:
        old_v = old[k]
        new_v = new[k]
        if isinstance(old_v, dict) and isinstance(new_v, dict):
            sub_added, sub_removed = _diff_keys(old_v, new_v, f"{prefix}.{k}" if prefix else k)
            added.extend(sub_added)
            removed.extend(sub_removed)

    return added, removed


def _banner(text: str) -> str:
    return f"\n  {text}"


def update_sot_structure(
    sot_path: Path,
    example_path: Path,
    *,
    dry_run: bool = False,
    quiet: bool = False,
) -> bool:
    """Update sot.toml structure to match the example.

    Returns True if changes were made (or would be made in dry_run mode).
    """
    if sys.version_info >= (3, 11):
        import tomllib
    else:
        import tomli as tomllib

    if not sot_path.exists():
        if not quiet:
            print(f"  sot.toml not found at {sot_path}")
        return False

    if not example_path.exists():
        if not quiet:
            print(f"  sot.example.toml not found at {example_path}")
        return False

    user_raw = tomllib.loads(sot_path.read_text(encoding="utf-8"))
    example_raw = tomllib.loads(example_path.read_text(encoding="utf-8"))

    merged = _deep_merge_structure(example_raw, user_raw)

    added, removed = _diff_keys(user_raw, merged)

    if not added and not removed:
        if not quiet:
            print("  sot.toml is already up to date — nothing to do.")
        return False

    if not quiet:
        if added:
            print(_banner("Keys that will be ADDED (using example defaults):"))
            for k in sorted(added):
                print(f"    + {k}")
        if removed:
            print(_banner("Keys that will be REMOVED (no longer in example):"))
            for k in sorted(removed):
                print(f"    - {k}")

    if dry_run:
        if not quiet:
            print(_banner("Dry run — no changes written."))
        return True

    # ── Backup ──
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = sot_path.with_suffix(f".toml.bak.{timestamp}")
    shutil.copy2(sot_path, backup_path)

    # ── Write ──
    header = (
        "# ─────────────────────────────────────────────────────────────────────────────\n"
        "#  sot-cli  —  Your personal configuration\n"
        "# ─────────────────────────────────────────────────────────────────────────────\n"
        "#  API keys live in `sot.keys.toml` (separate file, never committed).\n"
        "#  Updated structure on " + datetime.now().strftime("%Y-%m-%d %H:%M:%S") + "\n"
        "# ─────────────────────────────────────────────────────────────────────────────\n"
    )
    sot_path.write_text(header + _toml_dumps(merged), encoding="utf-8")

    if not quiet:
        print(_banner(f"Backup saved to {backup_path.name}"))
        print(_banner("sot.toml updated."))

    return True
