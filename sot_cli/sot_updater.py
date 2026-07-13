"""Sot-cli structure updater — surgically add missing keys from sot.example.toml.

Only APPENDS keys that exist in the example but are missing in the user file.
Never overwrites user values. Never removes user-only keys. Never rewrites
the entire file (preserves comments and formatting). Creates a backup only
when actual keys are being added.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
import shutil
from datetime import datetime


def _banner(text: str) -> str:
    return f"\n  {text}"


def _find_missing_keys(example: dict[str, Any], user: dict[str, Any], prefix: str = "") -> list[str]:
    """Return dotted key paths that exist in example but not in user."""
    missing: list[str] = []
    for key, example_value in example.items():
        path = f"{prefix}.{key}" if prefix else key
        if key not in user:
            missing.append(path)
        elif isinstance(example_value, dict) and isinstance(user.get(key), dict):
            missing.extend(_find_missing_keys(example_value, user[key], path))
    return missing


def _serialize_value(value: Any) -> str:
    """Serialize a TOML value for insertion into a file."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    if isinstance(value, list):
        items = ", ".join(_serialize_value(v) for v in value)
        return f"[{items}]"
    return f'"{value}"'


def _append_missing_keys(
    lines: list[str],
    example: dict[str, Any],
    user: dict[str, Any],
    section_prefix: str = "",
) -> int:
    """Append missing keys from example to lines. Returns count of keys added."""
    added = 0
    simple_to_add: list[tuple[str, Any]] = []
    nested_to_add: dict[str, dict[str, Any]] = {}

    for key, example_value in example.items():
        if key not in user:
            if isinstance(example_value, dict):
                nested_to_add[key] = example_value
            else:
                simple_to_add.append((key, example_value))
        elif isinstance(example_value, dict) and isinstance(user.get(key), dict):
            # Recurse into shared nested tables
            sub_prefix = f"{section_prefix}.{key}" if section_prefix else key
            # Find the existing [section] line or append
            added += _append_missing_keys(lines, example_value, user[key], sub_prefix)

    # Append simple keys to the right section
    if simple_to_add:
        section_header = f"[{section_prefix}]" if section_prefix else None
        insert_pos = len(lines)

        # Find the section header to append after its last key
        if section_header:
            in_section = False
            for i, line in enumerate(lines):
                stripped = line.strip()
                if stripped == section_header:
                    in_section = True
                    continue
                if in_section:
                    # Check if we've left the section
                    if stripped.startswith("[") and stripped.endswith("]"):
                        insert_pos = i
                        break
                    # Check if this is a key-value line (not blank/comment)
                    if stripped and not stripped.startswith("#"):
                        insert_pos = i + 1
            # If section not found, we'll append at end
        else:
            # Top-level keys — insert before first [section]
            for i, line in enumerate(lines):
                stripped = line.strip()
                if stripped.startswith("[") and stripped.endswith("]"):
                    insert_pos = i
                    break

        # Build insertion lines
        insert_lines = []
        for key, value in simple_to_add:
            insert_lines.append(f"{key} = {_serialize_value(value)}")
            added += 1

        for i, insert_line in enumerate(insert_lines):
            lines.insert(insert_pos + i, insert_line)

    # Append new nested sections at the end
    for key, nested_dict in nested_to_add.items():
        full_section = f"{section_prefix}.{key}" if section_prefix else key
        lines.append("")
        lines.append(f"[{full_section}]")
        for k, v in nested_dict.items():
            if isinstance(v, dict):
                # Sub-nested — will be handled as its own section
                sub_full = f"{full_section}.{k}"
                lines.append("")
                lines.append(f"[{sub_full}]")
                for sk, sv in v.items():
                    lines.append(f"{sk} = {_serialize_value(sv)}")
                    added += 1
            else:
                lines.append(f"{k} = {_serialize_value(v)}")
                added += 1

    return added


def update_sot_structure(
    sot_path: Path,
    example_path: Path,
    *,
    dry_run: bool = False,
    quiet: bool = False,
) -> bool:
    """Surgically add missing keys from example into sot.toml.

    Only appends keys that exist in example but not in user file.
    Never overwrites user values, never removes user-only keys,
    never rewrites the entire file (preserves comments and formatting).
    """
    import tomllib

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

    missing = _find_missing_keys(example_raw, user_raw)

    if not missing:
        if not quiet:
            print("  sot.toml is already up to date — nothing to do.")
        return False

    if not quiet:
        print(_banner("Keys that will be ADDED (using example defaults):"))
        for k in sorted(missing):
            print(f"    + {k}")

    if dry_run:
        if not quiet:
            print(_banner("Dry run — no changes written."))
        return True

    # ── Backup ──
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = sot_path.with_suffix(f".toml.bak.{timestamp}")
    shutil.copy2(sot_path, backup_path)

    # ── Surgically append missing keys ──
    lines = sot_path.read_text(encoding="utf-8").split("\n")
    keys_added = _append_missing_keys(lines, example_raw, user_raw)

    if keys_added > 0:
        sot_path.write_text("\n".join(lines), encoding="utf-8")

    if not quiet:
        print(_banner(f"Backup saved to {backup_path.name}"))
        print(_banner(f"sot.toml updated — {keys_added} key(s) added surgically."))

    return keys_added > 0
