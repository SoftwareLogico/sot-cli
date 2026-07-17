from __future__ import annotations

from typing import Any



def _require_string(arguments: dict[str, Any], key: str, strip: bool = True) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"\n{'='*70}\n"
            f"❌ ERROR: '{key}' must be a non-empty string\n"
            f"{'='*70}\n\n"
            f"💡 WHAT DOES THIS MEAN?\n"
            f"   The parameter '{key}' is required and must be non-empty text.\n\n"
            f"✅ HOW TO FIX:\n"
            f'   - Make sure to include "{key}": "value" in your call\n'
            f'   - Example: {{"path": "/path/to/file.md", "edits": [...]}}\n\n'
            f"{'='*70}\n"
        )
    return value.strip() if strip else value


def _require_string_allow_empty(arguments: dict[str, Any], key: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str):
        raise ValueError(
            f"\n{'='*70}\n"
            f"❌ ERROR: '{key}' must be a string (can be empty)\n"
            f"{'='*70}\n\n"
            f"💡 WHAT DOES THIS MEAN?\n"
            f"   The parameter '{key}' must be text, but can be empty.\n\n"
            f"✅ HOW TO FIX:\n"
            f'   - Use "{key}": "" for empty text\n'
            f'   - Use "{key}": "your text" for content\n\n'
            f"{'='*70}\n"
        )
    return value


def _ensure_no_arguments(arguments: dict[str, Any]) -> None:
    if arguments:
        raise ValueError("This tool does not accept arguments")


def _normalize_boolean(value: Any, default: bool, field_name: str) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
    raise ValueError(
        f"\n{'='*70}\n"
        f"❌ ERROR: '{field_name}' must be a boolean\n"
        f"{'='*70}\n\n"
        f"💡 WHAT DOES THIS MEAN?\n"
        f"   The parameter '{field_name}' must be true or false.\n\n"
        f"✅ ACCEPTED VALUES:\n"
        f"   - true, false (recommended)\n"
        f'   - "true", "false" (strings)\n'
        f"   - 1, 0 (numbers)\n\n"
        f"❌ RECEIVED VALUE: {value}\n\n"
        f"{'='*70}\n"
    )


def _normalize_float(value: Any, field_name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(
            f"\n{'='*70}\n"
            f"❌ ERROR: '{field_name}' must be a number (not boolean)\n"
            f"{'='*70}\n\n"
            f"💡 WHAT DOES THIS MEAN?\n"
            f"   You used true/false but a decimal number is required.\n\n"
            f"✅ CORRECT EXAMPLE:\n"
            f'   {{"{field_name}": 0.7}}\n\n'
            f"❌ WRONG:\n"
            f'   {{"{field_name}": true}} ← do NOT use booleans\n\n'
            f"{'='*70}\n"
        )
    try:
        normalized = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"\n{'='*70}\n"
            f"❌ ERROR: '{field_name}' must be a number\n"
            f"{'='*70}\n\n"
            f"💡 WHAT DOES THIS MEAN?\n"
            f"   The parameter '{field_name}' must be a valid number.\n\n"
            f"✅ CORRECT EXAMPLE:\n"
            f'   {{"{field_name}": 0.7}}\n\n'
            f"❌ WRONG:\n"
            f'   {{"{field_name}": \"hello\"}} ← do NOT use strings\n\n'
            f"{'='*70}\n"
        ) from exc
    if not (0.0 <= normalized <= 2.0):
        raise ValueError(
            f"\n{'='*70}\n"
            f"❌ ERROR: '{field_name}' must be between 0.0 and 2.0 (got {normalized})\n"
            f"{'='*70}\n\n"
            f"💡 WHAT DOES THIS MEAN?\n"
            f"   Temperature/sampling values must be in range [0.0, 2.0].\n\n"
            f"✅ CORRECT EXAMPLE:\n"
            f'   {{"{field_name}": 0.7}}\n\n'
            f"❌ WRONG:\n"
            f'   {{"{field_name}": 5.0}} ← out of range\n\n'
            f"{'='*70}\n"
        )
    return normalized


def _normalize_positive_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(
            f"\n{'='*70}\n"
            f"❌ ERROR: '{field_name}' must be a positive integer (not boolean)\n"
            f"{'='*70}\n\n"
            f"💡 WHAT DOES THIS MEAN?\n"
            f"   You used true/false but a positive integer is required.\n\n"
            f"✅ EXAMPLE:\n"
            f'   {{"start_line": 10, "end_line": 15}}\n\n'
            f"❌ WRONG:\n"
            f'   {{"start_line": true}} ← do NOT use booleans\n\n'
            f"{'='*70}\n"
        )
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"\n{'='*70}\n"
            f"❌ ERROR: '{field_name}' must be a positive integer\n"
            f"{'='*70}\n\n"
            f"💡 WHAT DOES THIS MEAN?\n"
            f"   The parameter '{field_name}' must be an integer greater than 0.\n\n"
            f"✅ CORRECT EXAMPLES:\n"
            f'   {{"start_line": 10, "end_line": 15}}\n'
            f'   {{"insert_line": 5, "position": "after"}}\n\n'
            f"❌ WRONG:\n"
            f'   {{"start_line": "10"}} ← do NOT use strings\n'
            f'   {{"start_line": 0}} ← do NOT use 0 or negatives\n\n'
            f"{'='*70}\n"
        ) from exc
    if normalized <= 0:
        raise ValueError(
            f"\n{'='*70}\n"
            f"❌ ERROR: '{field_name}' must be > 0 (got {normalized})\n"
            f"{'='*70}\n\n"
            f"💡 WHAT DOES THIS MEAN?\n"
            f"   Line numbers must start from 1, not from 0.\n\n"
            f"✅ EXAMPLE:\n"
            f'   {{"start_line": 1, "end_line": 5}} ← lines 1 through 5\n\n'
            f"❌ WRONG:\n"
            f'   {{"start_line": 0}} ← there is no line 0\n\n'
            f"{'='*70}\n"
        )
    return normalized


def _normalize_pages_argument(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"\n{'='*70}\n"
            f"❌ ERROR: pages must be a non-empty string\n"
            f"{'='*70}\n\n"
            f"💡 WHAT DOES THIS MEAN?\n"
            f"   The 'pages' parameter must be a page range string.\n\n"
            f"✅ CORRECT EXAMPLES:\n"
            f'   {{"pages": "1-5"}} ← pages 1 through 5\n'
            f'   {{"pages": "3"}} ← just page 3\n\n'
            f"❌ WRONG:\n"
            f'   {{"pages": ""}} ← empty string\n'
            f'   {{"pages": 5}} ← must be a string, not a number\n\n'
            f"{'='*70}\n"
        )
    return value.strip()
def _normalize_timeout_seconds(value: Any, default_timeout_seconds: int) -> int | None:
    if value is None:
        return default_timeout_seconds
    if isinstance(value, bool):
        raise ValueError(
            f"\n{'='*70}\n"
            f"❌ ERROR: timeout_seconds must be an integer number of seconds (not boolean)\n"
            f"{'='*70}\n\n"
            f"💡 WHAT DOES THIS MEAN?\n"
            f"   You used true/false but a number of seconds is required.\n\n"
            f"✅ CORRECT EXAMPLES:\n"
            f'   {{"timeout_seconds": 60}} ← 60 seconds\n'
            f'   {{"timeout_seconds": 0}} ← no timeout\n\n'
            f"❌ WRONG:\n"
            f'   {{"timeout_seconds": true}} ← do NOT use booleans\n\n'
            f"{'='*70}\n"
        )
    try:
        timeout_seconds = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"\n{'='*70}\n"
            f"❌ ERROR: timeout_seconds must be an integer number of seconds\n"
            f"{'='*70}\n\n"
            f"💡 WHAT DOES THIS MEAN?\n"
            f"   The 'timeout_seconds' parameter must be a whole number.\n\n"
            f"✅ CORRECT EXAMPLES:\n"
            f'   {{"timeout_seconds": 60}} ← 60 seconds\n'
            f'   {{"timeout_seconds": 0}} ← no timeout\n\n'
            f"❌ WRONG:\n"
            f'   {{"timeout_seconds": \"60\"}} ← do NOT use strings\n\n'
            f"{'='*70}\n"
        ) from exc
    if timeout_seconds < 0:
        raise ValueError(
            f"\n{'='*70}\n"
            f"❌ ERROR: timeout_seconds must be >= 0 (got {timeout_seconds})\n"
            f"{'='*70}\n\n"
            f"💡 WHAT DOES THIS MEAN?\n"
            f"   Timeout cannot be negative.\n\n"
            f"✅ CORRECT EXAMPLES:\n"
            f'   {{"timeout_seconds": 60}} ← 60 seconds\n'
            f'   {{"timeout_seconds": 0}} ← no timeout (infinite)\n\n'
            f"{'='*70}\n"
        )
    if timeout_seconds == 0:
        return None
    return timeout_seconds
