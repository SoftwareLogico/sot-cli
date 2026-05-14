"""Hyper-compression: collapse tool-heavy chat blocks under user request.

Scans the messages array for blocks that follow the pattern:

    user prompt
    → assistant (with tool_calls) + tool response
    → assistant (with tool_calls) + tool response
    → ...
    → assistant (final text reply)

And replaces them with:

    user prompt
    user: "SYSTEM MESSAGE: used tools: <names>"
    assistant: (final text reply)

This is a ONE-SHOT operation — run once per session when the user or model
explicitly requests it.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any


def _msg_char_count(msg: dict[str, Any]) -> int:
    """Count characters in a message (handles both string and list content)."""
    content = msg.get("content", "")
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        total = 0
        for block in content:
            if isinstance(block, dict):
                for val in block.values():
                    if isinstance(val, str):
                        total += len(val)
                    elif isinstance(val, dict):
                        total += len(str(val))
        return total
    return len(str(content))


def hyper_compress_session(session_dir: Path, dry_run: bool = False) -> dict[str, Any]:
    """Compress tool-heavy blocks in a session's request.json.

    Creates a timestamped backup before modifying the file.

    Args:
        session_dir: Path to the session directory (contains request.json).
        dry_run: If True, only report what would be compressed without writing.

    Returns:
        A dict with stats::

            {
                "blocks_compressed": int,
                "messages_before": int,
                "messages_after": int,
                "chars_saved": int,
                "backup_path": str | None,
            }
    """
    request_path = session_dir / "request.json"
    if not request_path.exists():
        return {"error": f"request.json not found in {session_dir}"}

    # Read the full request
    with open(request_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    messages: list[dict[str, Any]] = data.get("payload", {}).get("messages", [])
    if not messages:
        return {"error": "No messages in request.json payload"}

    # Calculate original size
    chars_before = sum(_msg_char_count(m) for m in messages)
    msg_before = len(messages)

    # Process blocks
    compressed_messages = _compress(messages)

    chars_after = sum(_msg_char_count(m) for m in compressed_messages)
    msg_after = len(compressed_messages)

    stats = {
        "blocks_compressed": msg_before - msg_after,
        "messages_before": msg_before,
        "messages_after": msg_after,
        "chars_saved": chars_before - chars_after,
        "backup_path": None,
    }

    if dry_run:
        return stats

    # Create timestamped backup
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = session_dir / f"request_backup_{timestamp}.json"
    shutil.copy2(request_path, backup_path)
    stats["backup_path"] = str(backup_path)

    # Write compressed version
    data["payload"]["messages"] = compressed_messages
    with open(request_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    return stats


def _parse_tool_status(response: str) -> tuple[str, str]:
    """Determine tool success/failure from response content.

    Returns (status, error_message) where:
    - status is "success" or "failed"
    - error_message is empty on success, or brief description on failure
    """
    if not response:
        return "success", ""
    lower = response.lower()
    # Known failure patterns
    if "error:" in lower and "ok" not in lower[:20]:
        idx = lower.find("error:")
        err = response[idx:idx+80].strip()
        return "failed", err
    if "failed" in lower and "ok" not in lower[:20]:
        idx = lower.find("failed")
        snippet = response[max(0,idx-20):idx+80].strip()
        return "failed", snippet
    if "exit=1" in response.split("'")[-1] if "'" in response else False:
        return "failed", "command returned exit code 1"
    return "success", ""


def _format_tool_summary(pairs: list[tuple[str, str, str]]) -> str:
    """Build a SYSTEM MESSAGE summarizing what tools were used."""
    parts: list[str] = []
    seen_names: set[str] = set()
    for name, status, err in pairs:
        if name not in seen_names:
            seen_names.add(name)
            if status == "failed":
                parts.append(f"{name} ({status}: {err})")
            else:
                parts.append(f"{name} ({status})")
    return f"SYSTEM MESSAGE: Assistant requested tools this turn: {', '.join(parts)}. As stated in system instructions this is a summary of tool usage."


def _get_content_text(msg: dict[str, Any]) -> str:
    """Extract text content regardless of format (string or list of blocks)."""
    content = msg.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                texts.append(str(block.get("text", "")))
        return "\n".join(texts)
    return str(content)


def _has_tool_calls(msg: dict[str, Any]) -> bool:
    """Check if a message has tool calls (OpenAI format or Bedrock Converse format)."""
    if msg.get("tool_calls"):
        return True
    content = msg.get("content", [])
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and "toolUse" in block:
                return True
    return False


def _get_tool_names(msg: dict[str, Any]) -> list[str]:
    """Extract tool names from a message (both formats)."""
    names = []
    for tc in (msg.get("tool_calls") or []):
        func = tc.get("function", {})
        names.append(str(func.get("name", "?")))
    content = msg.get("content", [])
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and "toolUse" in block:
                names.append(str(block["toolUse"].get("name", "?")))
    return names


def _compress(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse tool blocks in a message list.

    The algorithm:

    1. Always keep the first ``system`` message verbatim.
    2. Scan forward from message ``i``.
    3. If ``messages[i]`` is a ``user``-role message, peek ahead:
       - If it's followed by zero or more pairs of
         ``(assistant+tool_calls → tool)`` and then an ``assistant``
         with text content → collapse the whole block.
       - Otherwise → keep the message as-is.
    4. All other message roles are kept verbatim.
    """
    result: list[dict[str, Any]] = []
    i = 0
    n = len(messages)

    # Phase 1: keep system message(s) verbatim
    while i < n and messages[i].get("role") == "system":
        result.append(messages[i])
        i += 1

    # Phase 2: scan the rest
    while i < n:
        msg = messages[i]

        # Only user messages can start a tool block
        if msg.get("role") != "user":
            result.append(msg)
            i += 1
            continue

        # Peek ahead to see if this is a tool block
        j = i + 1
        tool_names: list[str] = []
        tool_pairs: list[tuple[str, str, str]] = []

        while j < n:
            m = messages[j]
            role = m.get("role", "")

            if role == "assistant" and _has_tool_calls(m):
                # Collect tool names
                for name in _get_tool_names(m):
                    if name not in tool_names:
                        tool_names.append(name)
                j += 1

                # Next message must be a tool response
                if j < n and messages[j].get("role") == "tool":
                    resp_content = str(messages[j].get("content", ""))
                    status, err = _parse_tool_status(resp_content)
                    for name in _get_tool_names(m):
                        tool_pairs.append((name, status, err))
                    j += 1
                    continue
                # No matching tool response → not a valid block
                break

            elif role == "assistant" and not _has_tool_calls(m) and m.get("content") and str(m.get("content", "")).strip() not in ("", "None"):
                # Final assistant text response — this ends the block
                if tool_pairs:
                    # Collapse: keep user, insert SYSTEM MESSAGE, keep final text
                    result.append(msg)  # original user prompt
                    sys_msg = _format_tool_summary(tool_pairs)
                    result.append({"role": "user", "content": sys_msg})
                    result.append(m)  # final assistant text response
                    i = j + 1
                    break
                else:
                    # No tools were used — keep the user message as-is
                    result.append(msg)
                    i += 1
                    break

            elif role == "user":
                # A SYSTEM MESSAGE within a tool block is from auto-compression.
                # Skip it - it's not a real user prompt.
                content_text = str(m.get("content", ""))
                if content_text.startswith("SYSTEM MESSAGE:"):
                    j += 1
                    continue
                # Nested real user message — block was interrupted.
                if tool_pairs:
                    result.append(msg)
                    sys_msg = _format_tool_summary(tool_pairs)
                    result.append({"role": "user", "content": sys_msg})
                    i = j  # Continue from the interrupting user
                else:
                    result.append(msg)
                    i += 1
                break

            else:
                # Something unexpected — bail out and keep everything as-is
                result.append(msg)
                i += 1
                break
        else:
            # j reached end of messages without finding a final assistant
            if tool_pairs:
                result.append(msg)  # original user
                sys_msg = _format_tool_summary(tool_pairs)
                result.append({"role": "user", "content": sys_msg})
                i = j
            else:
                result.append(msg)
                i += 1

    return result


def reload_chat_history_from_request(session_dir: Path) -> list[dict[str, Any]] | None:
    """Load chat_history from a compressed request.json, filtering out ephemeral blocks.

    Strips the system prompt, SoT blocks, orchestration rules, and
    CURRENT METADATA blocks — same logic as cli.py's internal loader.
    """
    request_path = session_dir / "request.json"
    if not request_path.exists():
        return None

    try:
        request_data = json.loads(request_path.read_text(encoding="utf-8"))
        messages = request_data.get("payload", {}).get("messages", [])
    except (json.JSONDecodeError, KeyError, OSError):
        return None

    if not messages:
        return None

    chat: list[dict[str, Any]] = []
    for msg in messages:
        role = msg.get("role", "")
        if role == "system":
            continue
        content = msg.get("content", "")
        content_text = _get_content_text(msg)
        if role == "user" and _is_sot_block(content_text):
            continue
        if role == "user" and content_text.startswith("=== CURRENT METADATA ==="):
            continue
        chat.append(msg)

    return chat if chat else None


def _is_sot_block(content: str) -> bool:
    """Check if a user message is an SoT block."""
    return content.startswith("=== SOURCE OF TRUTH ===") or "=== END SOURCE OF TRUTH ===" in content
