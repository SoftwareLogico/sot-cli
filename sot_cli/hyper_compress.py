"""Hyper-compression for session histories — two distinct, on-request modes.

- ``compress`` (``--hypercompress`` with no value): collapses past tool
  chatter inside every closed turn. Each ``(assistant+tool_calls → tool)+``
  run becomes one compact ``SYSTEM MESSAGE:`` receipt. Chat-only turns and
  the rest of the conversation stay verbatim.

- ``truncate`` (``--hypercompress PERCENT``): removes the OLDEST PERCENT%
  of closed turns from the conversation entirely. A turn starts at a real
  user prompt and ends right before the next one; every removed turn (user
  prompts, assistant replies, tool calls and tool responses, in order) is
  appended to a Markdown archive inside the session folder:

      <session_dir>/hypercompress_archive.md

  and replaced in ``request.json`` by a single ``SYSTEM MESSAGE:`` marker so
  the original text is preserved for backup/audit purposes.

In both modes the system prompt, the first real user message (rules), the
SoT block, the CURRENT METADATA block and the active turn are never touched.

This is a ONE-SHOT operation — run once per session when the user explicitly
requests it.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from sot_cli.sot import is_orchestration_rules_content, is_sot_block_content


def _msg_char_count(msg: dict[str, Any]) -> int:
    """Approximate the wire size of a message (content + tool args + reasoning).

    Tool-call ``arguments`` are the heaviest part of a mutation round and were
    not counted before, which made ``chars_saved`` under-report (and even go
    negative on short synthetic histories) right after dropping them.
    """
    total = 0
    content = msg.get("content", "")
    if isinstance(content, str):
        total += len(content)
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                for val in block.values():
                    if isinstance(val, str):
                        total += len(val)
                    elif isinstance(val, dict):
                        total += len(str(val))
    else:
        total += len(str(content))

    tool_calls = msg.get("tool_calls")
    if isinstance(tool_calls, list):
        for call in tool_calls:
            if not isinstance(call, dict):
                continue
            func = call.get("function")
            if not isinstance(func, dict):
                continue
            name = func.get("name")
            args = func.get("arguments")
            if isinstance(name, str):
                total += len(name)
            if isinstance(args, str):
                total += len(args)
            elif args is not None:
                total += len(str(args))

    reasoning = msg.get("reasoning")
    if isinstance(reasoning, str):
        total += len(reasoning)
    details = msg.get("reasoning_details")
    if isinstance(details, list):
        total += len(json.dumps(details, ensure_ascii=False))

    return total


def _append_to_archive(archive_path: Path, turn_number: int, markdown: str) -> None:
    """Append one compressed turn to the session's cumulative archive .md."""
    header = (
        f"\n\n---\n\n### Turno {turn_number} — archivado por hipercompresión "
        f"({datetime.now().isoformat(timespec='seconds')})\n\n"
    )
    with open(archive_path, "a", encoding="utf-8") as f:
        f.write(header)
        f.write(markdown)
        f.write("\n")


def _format_block_as_markdown(block: list[dict[str, Any]]) -> str:
    """Render a whole turn (user/assistant/tool messages) as Markdown text."""
    parts: list[str] = []
    for msg in block:
        role = msg.get("role", "?")
        text = _get_content_text(msg).strip()
        if not text:
            continue
        parts.append(f"**{role}:**\n```\n{text}\n```\n")
    return "\n".join(parts)


def hyper_compress_session(
    session_dir: Path,
    dry_run: bool = False,
    percent: int = 100,
    mode: str = "truncate",
) -> dict[str, Any]:
    """Hyper-compress a session's request.json.

    ``mode="compress"`` collapses past tool chatter inside every closed turn
    into receipts; chat-only turns are untouched.

    ``mode="truncate"`` removes the OLDEST ``percent``% of closed turns (a
    turn starts at a real user prompt and ends right before the next one),
    appends their full text to ``session_dir/hypercompress_archive.md`` and
    replaces each one by a single ``SYSTEM MESSAGE:`` marker. The active turn
    — the prompt the session will resume with — is never touched.

    Creates a timestamped backup before modifying the file.

    Args:
        session_dir: Path to the session directory (contains request.json).
        dry_run: If True, only report what would be compressed without writing.
        percent: Share (1-100) of the closed turns to truncate, oldest first.
        mode: ``"compress"`` (tool chatter) or ``"truncate"`` (turn removal).

    Returns:
        A dict with stats::

            {
                "mode": str,
                "blocks_compressed": int,
                "messages_before": int,
                "messages_after": int,
                "chars_saved": int,
                "backup_path": str | None,
                "percent": int,
                "turns_total": int,
                "turns_compressible": int,
                "turns_compressed": int,
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

    percent = max(0, min(100, int(percent)))
    mode = str(mode).strip().lower()
    if mode not in ("compress", "truncate"):
        mode = "truncate"
    spans = _split_turns(messages)
    # The last span is the ACTIVE turn: never compressed.
    closed_turns = max(0, len(spans) - 1)

    # Calculate original size
    chars_before = sum(_msg_char_count(m) for m in messages)
    msg_before = len(messages)

    archive_path = session_dir / "hypercompress_archive.md"
    compressed_messages, turns_compressed = _compress(
        messages,
        mode=mode,
        percent=percent,
        spans=spans,
        archive_path=archive_path,
        turns_compressible_total=closed_turns,
    )

    chars_after = sum(_msg_char_count(m) for m in compressed_messages)
    msg_after = len(compressed_messages)

    stats = {
        "mode": mode,
        "blocks_compressed": msg_before - msg_after,
        "messages_before": msg_before,
        "messages_after": msg_after,
        "chars_saved": chars_before - chars_after,
        "backup_path": None,
        "percent": percent,
        "turns_total": len(spans),
        "turns_compressible": closed_turns,
        "turns_compressed": turns_compressed,
    }

    if dry_run:
        return stats

    if not turns_compressed or msg_before == msg_after:
        # Nothing to rewrite — do not churn a backup for a no-op.
        return stats

    # Create timestamped backup
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = session_dir / f"request_backup_{timestamp}.json"
    shutil.copy2(request_path, backup_path)
    stats["backup_path"] = str(backup_path)

    # Write compressed version
    data["payload"]["messages"] = compressed_messages
    _text = json.dumps(data, ensure_ascii=False, indent=2)
    # Neutraliza surrogates sueltos (surrogateescape de consolas Windows) y
    # escribe atomico: si algo falla, el request.json anterior queda intacto.
    _text = _text.encode("utf-8", "backslashreplace").decode("utf-8")
    _tmp = request_path.with_suffix(request_path.suffix + ".tmp")
    _tmp.write_text(_text, encoding="utf-8")
    import os as _os

    _os.replace(_tmp, request_path)

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
        err = response[idx:idx + 80].strip()
        return "failed", err
    if "failed" in lower and "ok" not in lower[:20]:
        idx = lower.find("failed")
        snippet = response[max(0, idx - 20) : idx + 80].strip()
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


# Prefixes that identify ephemeral messages injected by the runtime. They are
# never turn boundaries and are always preserved verbatim.
_EPHEMERAL_USER_PREFIXES = ("=== CURRENT METADATA ===", "SYSTEM MESSAGE:")


def _is_real_user_message(msg: dict[str, Any]) -> bool:
    """True for a genuine user prompt — the only thing that starts a turn."""
    if msg.get("role") != "user":
        return False
    text = _get_content_text(msg).lstrip()
    if not text:
        return False
    if is_sot_block_content(msg.get("content")):
        return False
    if is_orchestration_rules_content(msg.get("content")):
        return False
    return not text.startswith(_EPHEMERAL_USER_PREFIXES)


def _split_turns(messages: list[dict[str, Any]]) -> list[tuple[int, int]]:
    """Split the payload into turn spans ``[(start, end), ...]``, end-exclusive.

    A turn starts at a real user prompt and ends right before the next one. The
    final span is the ACTIVE turn (the prompt the session resumes with).
    """
    starts = [
        index
        for index, message in enumerate(messages)
        if isinstance(message, dict) and _is_real_user_message(message)
    ]
    spans: list[tuple[int, int]] = []
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(messages)
        spans.append((start, end))
    return spans


def _turns_for_percent(total: int, percent: int) -> int:
    """How many turns ``percent`` covers. Half-up rounding, floor of 1."""
    if total <= 0 or percent <= 0:
        return 0
    if percent >= 100:
        return total
    return max(1, min(total, int(total * percent / 100.0 + 0.5)))


def _collapse_tool_runs(block: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse every ``(assistant+tool_calls → tool)+`` run inside ONE turn.

    Kept for backwards compatibility; the new default path archives the whole
    turn instead of only collapsing tool chatter.
    """
    result: list[dict[str, Any]] = []
    index = 0
    total = len(block)

    while index < total:
        message = block[index]
        if message.get("role") != "assistant" or not _has_tool_calls(message):
            result.append(message)
            index += 1
            continue

        cursor = index
        pairs: list[tuple[str, str, str]] = []
        complete = True
        while cursor < total:
            current = block[cursor]
            if current.get("role") != "assistant" or not _has_tool_calls(current):
                break
            if cursor + 1 >= total or block[cursor + 1].get("role") != "tool":
                complete = False
                break
            status, error = _parse_tool_status(str(block[cursor + 1].get("content", "")))
            for name in _get_tool_names(current):
                pairs.append((name, status, error))
            cursor += 2

        if not complete or not pairs:
            result.append(message)
            index += 1
            continue

        result.append({"role": "user", "content": _format_tool_summary(pairs)})
        index = cursor

    return result


def _compress(
    messages: list[dict[str, Any]],
    mode: str = "truncate",
    percent: int = 100,
    spans: list[tuple[int, int]] | None = None,
    archive_path: Path | None = None,
    turns_compressible_total: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """Rebuild the message list according to ``mode``.

    ``compress``: every ``(assistant+tool_calls → tool)+`` run inside CLOSED
    turns is collapsed into one receipt; chat-only turns stay verbatim.

    ``truncate``: the OLDEST ``percent``% of closed turns is removed from the
    conversation, archived to ``archive_path`` (when provided) and replaced by
    a single ``SYSTEM MESSAGE:`` marker.

    System messages, real user prompts, final assistant answers, ephemeral
    blocks and the active turn are always preserved.

    Returns the rebuilt message list and the number of turns compressed.
    """
    spans = _split_turns(messages) if spans is None else spans
    closed_turns = max(0, len(spans) - 1)

    result: list[dict[str, Any]] = []
    cursor = 0

    if mode == "compress":
        turns_compressed = 0
        for position, (start, end) in enumerate(spans):
            result.extend(messages[cursor:start])
            block = messages[start:end]
            if position < closed_turns:
                collapsed = _collapse_tool_runs(block)
                if len(collapsed) != len(block):
                    turns_compressed += 1
                result.extend(collapsed)
            else:
                result.extend(block)
            cursor = end
        result.extend(messages[cursor:])
        return result, turns_compressed

    turns_to_compress = _turns_for_percent(closed_turns, percent)
    for position, (start, end) in enumerate(spans):
        result.extend(messages[cursor:start])
        block = messages[start:end]
        if position < turns_to_compress:
            markdown = _format_block_as_markdown(block)
            if archive_path is not None:
                _append_to_archive(archive_path, position + 1, markdown)
            result.append(
                {
                    "role": "user",
                    "content": (
                        f"SYSTEM MESSAGE: [hiper-comprimido: turno {position + 1} de "
                        f"{turns_compressible_total} archivado en "
                        f"{archive_path.name if archive_path else 'hypercompress_archive.md'}]"
                    ),
                }
            )
        else:
            result.extend(block)
        cursor = end
    result.extend(messages[cursor:])
    return result, turns_to_compress


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
