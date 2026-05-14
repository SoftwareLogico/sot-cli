from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import httpx

from sot_cli.constants import COMPRESSED_TOOLS
from sot_cli.message_builder import build_user_turn_message
from sot_cli.providers.base import ProviderCapability, ProviderCompletion, ProviderEvent, ProviderRequest


# Word-boundary scan used by ``_is_successful_tool_response``. Matches any
# inflection of ``fail`` (``fail``, ``fails``, ``failed``, ``failing``,
# ``failure``, ``failures``) and ``error`` (``error``, ``errors``) as a
# whole word, case-insensitive. The boundaries avoid false positives on
# paths and filenames that happen to embed those substrings (e.g. an
# edit applied to ``/tmp/error.log`` or ``fail-tests.txt`` would still
# be classified as successful because the word lives inside a path
# token, not as a free-standing word). Compiled once at module load
# time so the hot sanitizer loop does not pay the regex compile cost
# per message.
_FAILURE_WORD_PATTERN = re.compile(r"\b(fail\w*|error\w*)\b", re.IGNORECASE)


# ─── Outbound message sanitizer ──────────────────────────────────────────
#
# This module is the LAST transformation before the chat payload reaches
# the network. It enforces three independent invariants:
#
#   1. Schema strictness — drop empty husks that strict providers reject.
#   2. Tool-call ↔ tool-message single-use pairing (orphan/duplicate cleanup).
#   3. SoT-aware compression of OLD turns:
#        * Reasoning of tool-bearing assistants is truncated to N chars.
#        * Successful (write_file | edit_files) pairs are replaced by a
#          single `user`-role "SYSTEM MESSAGE: ..." log line that names
#          the tool, the path(s), the result metadata, and a short
#          reasoning excerpt — and DROPS the heavy `arguments` body
#          (the full new_string blocks / file content) which is already
#          reflected in the next turn's '=== SOURCE OF TRUTH ===' block.
#
# Compression is applied ONLY to messages BEFORE the index of the latest
# user message in chat_history (i.e. closed turns). The active turn in
# flight is never touched — its tool_call and tool_response messages
# round-trip in full so the model can see exactly what it just did.
#
# The transformation is a pure function of the input (deterministic):
# the same message processed twice produces the same bytes on the wire,
# which is what makes prefix-matching prompt caches hit across rounds and
# turns.


_SYSTEM_MESSAGE_PREFIX = "SYSTEM MESSAGE: "
_TRUNC_MARKER = "...[truncated]"


def _truncate_reasoning(text: str, char_cap: int) -> str:
    """Return ``text`` clipped to ``char_cap`` chars + a [truncated] marker.

    ``char_cap == 0`` disables the cap and returns the input verbatim.
    Strings already within the budget are returned unchanged so the
    transformation is idempotent for short reasoning blocks.
    """
    if char_cap <= 0 or not isinstance(text, str):
        return text
    if len(text) <= char_cap:
        return text
    return text[:char_cap] + _TRUNC_MARKER


def _truncate_reasoning_details(details: list[Any], char_cap: int) -> list[Any]:
    """Truncate the merged ``text`` of consecutive ``reasoning.text`` entries.

    Mirrors the cap applied to plain ``reasoning`` strings: clamps the
    cumulative text characters across mergeable entries to ``char_cap``
    while leaving non-text entries (encrypted blobs, summaries) atomic.
    Order and entry shapes are preserved; only the ``text`` field of
    text-class entries gets shortened.
    """
    if char_cap <= 0 or not isinstance(details, list):
        return details

    remaining = char_cap
    truncated: list[Any] = []
    capped = False
    for entry in details:
        if not isinstance(entry, dict):
            truncated.append(entry)
            continue
        if entry.get("type") != "reasoning.text":
            truncated.append(entry)
            continue
        text = entry.get("text")
        if not isinstance(text, str):
            truncated.append(entry)
            continue
        if capped:
            new_entry = dict(entry)
            new_entry["text"] = ""
            truncated.append(new_entry)
            continue
        if len(text) <= remaining:
            truncated.append(entry)
            remaining -= len(text)
            continue
        new_entry = dict(entry)
        new_entry["text"] = text[:remaining] + _TRUNC_MARKER
        truncated.append(new_entry)
        remaining = 0
        capped = True

    return truncated


def _excerpt_for_system_log(text: str, char_cap: int) -> str:
    """Return a compact reasoning excerpt embedded in a SYSTEM MESSAGE line.

    Differs from :func:`_truncate_reasoning` only in defaulting to a
    minimum length when the cap is disabled (``char_cap == 0``) — the
    SYSTEM MESSAGE line is meant to be a one-line log, so we always trim
    long reasonings to a sane bound for that specific embed.
    """
    if not isinstance(text, str) or not text:
        return ""
    effective_cap = char_cap if char_cap > 0 else 240
    cleaned = " ".join(text.split())
    if len(cleaned) <= effective_cap:
        return cleaned
    return cleaned[:effective_cap] + _TRUNC_MARKER


def _format_compressed_tool_call(tool_call: dict[str, Any], tool_response: dict[str, Any], reasoning_excerpt: str) -> str:
    """Build the per-tool fragment of a SYSTEM MESSAGE line.

    Produces a compact ``key=value`` rendering with all paths, line
    counts, and byte counts intact (the model will reference these on
    later turns), while the reasoning is reduced to a one-line excerpt.

    Only ``write_file`` and ``edit_files`` are formatted here — they are
    the only tools whose pair gets compressed. Both schemas are parsed
    defensively: a malformed ``arguments`` JSON falls back to a minimal
    rendering so the SYSTEM MESSAGE line is always emittable.
    """
    function = tool_call.get("function") if isinstance(tool_call.get("function"), dict) else {}
    name = function.get("name", "") if isinstance(function.get("name"), str) else ""
    raw_args = function.get("arguments", "")
    try:
        parsed_args = json.loads(raw_args) if isinstance(raw_args, str) and raw_args else {}
    except (json.JSONDecodeError, TypeError, ValueError):
        parsed_args = {}
    if not isinstance(parsed_args, dict):
        parsed_args = {}

    response_content = tool_response.get("content") if isinstance(tool_response, dict) else ""
    response_summary = response_content if isinstance(response_content, str) else ""
    # Collapse newlines so the SYSTEM MESSAGE line stays single-line.
    response_summary = " ".join(response_summary.split())
    # Cap the response excerpt so a verbose tool result doesn't blow up
    # the wire size (the structured fields below already carry the
    # important metadata; the literal response is included as a hint).
    if len(response_summary) > 200:
        response_summary = response_summary[:200] + _TRUNC_MARKER

    parts: list[str] = [name or "?"]

    if name == "write_file":
        path = parsed_args.get("path") if isinstance(parsed_args.get("path"), str) else "?"
        parts.append(f"path={path}")
        parts.append("sot=tracked_unless_detached")
    elif name == "edit_files":
        files = parsed_args.get("files")
        paths: list[str] = []
        edit_count = 0
        if isinstance(files, list):
            for entry in files:
                if not isinstance(entry, dict):
                    continue
                p = entry.get("path")
                if isinstance(p, str):
                    paths.append(p)
                edits = entry.get("edits")
                if isinstance(edits, list):
                    edit_count += len(edits)
        if paths:
            parts.append(f"paths={','.join(paths)}")
        if edit_count:
            parts.append(f"edits={edit_count}")
        parts.append("sot=tracked_unless_detached")
    else:
        # Defensive: COMPRESSED_TOOLS would normally gate this; if a new
        # tool is added there without a formatter, fall back to a generic
        # rendering rather than crash.
        for key in ("path", "paths"):
            if key in parsed_args:
                parts.append(f"{key}={parsed_args[key]}")

    if response_summary:
        parts.append(f"result=\"{response_summary}\"")
    if reasoning_excerpt:
        parts.append(f"reasoning=\"{reasoning_excerpt}\"")

    return " ".join(parts)


def _build_system_message_user(fragments: list[str]) -> dict[str, Any]:
    """Build the `user`-role SYSTEM MESSAGE container for one or more
    compressed tool fragments emitted in the same assistant round.

    Multi-tool rounds are joined with a `` | `` separator so the line
    stays parseable and single-line. The `SYSTEM MESSAGE:` prefix is in
    uppercase intentionally — the system-prompt rule that explains this
    format keys on that exact prefix.
    """
    body = " | ".join(fragments)
    return {"role": "user", "content": _SYSTEM_MESSAGE_PREFIX + "used tools: " + body}


def _is_effectively_empty_text(value: Any) -> bool:
    """True for values that the strict APIs treat as 'no content'.

    None and whitespace-only strings both count: LM Studio and the
    OpenAI strict validator both expect a non-empty string when an
    assistant message has no tool_calls. The model emitting ``"\\n\\n"``
    next to a stripped tool_call (a thought-bubble that points to
    nothing) is functionally the same problem, so we treat them
    identically here.
    """
    if value is None:
        return True
    if isinstance(value, str) and value.strip() == "":
        return True
    return False


def _is_successful_tool_response(content: Any) -> bool:
    """Heuristic: does this tool_response report a successful mutation?

    Refuses to compress whenever the response contains a failure
    marker. Three shapes are caught by the same word-boundary scan
    (:data:`_FAILURE_WORD_PATTERN`):

    1. **Catastrophic crash from the runtime wrapper.** When a tool
       sets ``is_error=True``, ``_build_tool_result_summary`` in
       ``query.py`` returns ``"error: <message>"``. The leading
       ``error`` matches.
    2. **Partial / total failure inside ``edit_files``.** When any
       per-file edit does not apply, the runtime formats the summary
       as ``"edit_files: X/Y ok, N failed. ..."`` (with at least one
       per-file ``- FAILED <path>: <reason>`` line below). The literal
       ``failed`` matches even though the wrapper itself returned
       ``is_error=False`` (the tool ran, the edits did not).
    3. **External (MCP) tool failures whose format we do not own.**
       Any case-insensitive occurrence of ``fail*`` or ``error*`` as a
       whole word in the response body triggers preservation. This is
       the safety net for tool result strings produced by code outside
       this codebase.

    False positives (a successful response that happens to mention
    "error" or "fail" — for example an edit applied to a file whose
    path embeds one of those words) are tolerated by design. The
    failure mode is just "preserve a successful response intact
    instead of compressing it", strictly safer than the inverse
    (silently compressing a failed mutation and letting the model
    loop on it). Non-string ``content`` is treated as
    "unknown shape, do not compress".
    """
    if not isinstance(content, str):
        return False
    stripped = content.strip()
    if not stripped:
        return False
    if _FAILURE_WORD_PATTERN.search(stripped):
        return False
    return True


def _sanitize_messages_for_provider(
    messages: list[dict[str, Any]],
    compression_reasoning_trunc_chars: int = 0,
) -> list[dict[str, Any]]:
    """Strict-API firewall + SoT-aware compression of closed turns.

    Pipeline (each pass operates on a shallow copy; the caller's
    chat_history is never mutated):

    1. **Schema strictness — drop empty husks.** Assistant messages
       whose ``content`` is ``None`` (or whitespace-only) AND that have
       no ``tool_calls`` are rejected by LM Studio (HTTP 500) and by
       OpenAI strict (HTTP 400). They are dropped. ``tool``-role
       messages with ``content: None`` are coerced to ``content: ""``
       instead of dropped, because dropping would orphan their
       matching ``tool_call``.

    2. **Tool-call ↔ tool-message single-use pairing.** OpenAI strict
       requires every assistant ``tool_call`` to be followed by exactly
       one ``tool``-role message carrying the same ``tool_call_id``
       (and vice-versa). Three pathological shapes are scrubbed here:

       * ``tool_call`` with ``arguments: ""`` (stream cut before the
         args delta arrived);
       * ``tool_call`` with no matching ``tool`` response anywhere;
       * ``tool_call`` whose ``id`` collides with a previous one whose
         response has already been consumed (duplicate from a retry).

       A two-pass walk (index then emit) single-use-matches each
       ``tool_call`` against a pending set; orphans/duplicates and
       their dangling companions are dropped silently.

    3. **Compression of CLOSED turns.** Everything STRICTLY BEFORE the
       index of the latest ``user`` message in the chat history is a
       closed turn — the model already received its outcome and moved
       on. The active turn (everything from that index onward) is
       never touched.

       For closed turns the sanitizer does two things:

       a. Truncates the ``reasoning`` and ``reasoning_details`` of any
          assistant message that carries ``tool_calls`` to
          ``compression_reasoning_trunc_chars`` chars. The cap of 0
          disables this and round-trips reasoning verbatim. The
          ``content`` and reasoning of the FINAL assistant message of
          a closed turn (the one the user actually saw, which has no
          ``tool_calls``) are NEVER truncated — that text is the
          historical record of what the model said and the user may
          reference it later.

       b. Replaces the (assistant-with-tool_call → tool-response) pair
          with a single ``user``-role ``"SYSTEM MESSAGE: used tools: ..."``
          line whenever the tool name is in :data:`COMPRESSED_TOOLS`
          (currently ``write_file`` and ``edit_files``) AND the
          tool_response indicates success AND the assistant message
          carries exactly one ``tool_call``. Mixed rounds (a
          compressible tool together with non-compressible tool_calls
          in the same array) are conservatively left intact —
          surgically removing one tool_call from a multi-call array
          and shifting its companion response into a SYSTEM MESSAGE
          while preserving the others would risk breaking strict
          pairing under unusual stream-interruption shapes; in
          practice mutation and exploration tools rarely co-occur in
          the same round under the orchestration prompts.

    The transformation is deterministic: same ``messages`` and same
    ``compression_reasoning_trunc_chars`` produce the same output bytes.
    That stability is what makes Anthropic / OpenAI prefix-matching
    prompt caches hit across rounds and across turns — the prefix of
    closed turns recompresses to identical bytes turn after turn.

    ``reasoning`` and ``reasoning_details`` of the active turn are
    deliberately LEFT ON: OpenRouter and the Anthropic / GPT-5
    reasoning class require reasoning to be round-tripped to maintain
    reasoning continuity; stripping it for the active turn would
    silently degrade those providers.
    """
    # ── Pass 1: locate the active-turn boundary ──
    # ``last_user_idx`` is the index of the most recent ``user`` message
    # in the chat history. Everything strictly before it is a CLOSED
    # turn (compression candidate); everything from that index onward
    # is the ACTIVE turn (never touched). When the chat history starts
    # mid-stream and there is no user message at all, every position is
    # treated as closed for safety — the original behaviour of the
    # sanitizer is preserved in that degenerate case.
    last_user_idx = -1
    for i in range(len(messages) - 1, -1, -1):
        entry = messages[i]
        if isinstance(entry, dict) and entry.get("role") == "user":
            last_user_idx = i
            break
    if last_user_idx < 0:
        last_user_idx = len(messages)

    # ── Pass 2: index every tool_call_id that has a matching tool message ──
    tool_response_ids: set[str] = set()
    tool_response_by_id: dict[str, dict[str, Any]] = {}
    for entry in messages:
        if not isinstance(entry, dict) or entry.get("role") != "tool":
            continue
        tc_id = entry.get("tool_call_id")
        if isinstance(tc_id, str) and tc_id:
            tool_response_ids.add(tc_id)
            tool_response_by_id.setdefault(tc_id, entry)

    # ── Pass 3: emit messages, applying schema firewall + compression ──
    sanitized: list[dict[str, Any]] = []
    pending_tool_call_ids: set[str] = set()
    consumed_tool_call_ids: set[str] = set()
    # Pre-computed for each closed-turn assistant: the tool_call IDs we
    # are going to compress away. Their matching ``tool`` messages will
    # be dropped from the emit pass and replaced by a SYSTEM MESSAGE.
    drop_tool_response_ids: set[str] = set()

    for index, original in enumerate(messages):
        if not isinstance(original, dict):
            # Malformed entry; the provider would reject it anyway.
            continue
        msg = dict(original)
        role = msg.get("role")
        in_closed_turn = index < last_user_idx

        if role == "user":
            if msg.get("content") is None:
                continue
            sanitized.append(msg)
            continue

        if role == "assistant":
            tool_calls = msg.get("tool_calls")
            has_tool_calls = isinstance(tool_calls, list) and bool(tool_calls)

            # Closed-turn reasoning truncation applies whenever this
            # assistant carries tool_calls — it is the "intermediate"
            # assistant whose reasoning often duplicates the body of
            # the tool_call about to fire.
            if in_closed_turn and has_tool_calls:
                if isinstance(msg.get("reasoning"), str):
                    msg["reasoning"] = _truncate_reasoning(
                        msg["reasoning"], compression_reasoning_trunc_chars
                    )
                if isinstance(msg.get("reasoning_details"), list):
                    msg["reasoning_details"] = _truncate_reasoning_details(
                        msg["reasoning_details"], compression_reasoning_trunc_chars
                    )

            if has_tool_calls:
                surviving_calls: list[dict[str, Any]] = []
                # Decide whether this assistant qualifies for full pair
                # compression (single tool_call, in COMPRESSED_TOOLS, in
                # a closed turn, with a successful tool_response).
                compressible_tool_call: dict[str, Any] | None = None
                if in_closed_turn and len(tool_calls) == 1:
                    only_tc = tool_calls[0] if isinstance(tool_calls[0], dict) else None
                    if isinstance(only_tc, dict):
                        only_func = only_tc.get("function") if isinstance(only_tc.get("function"), dict) else None
                        if isinstance(only_func, dict):
                            only_name = only_func.get("name", "") if isinstance(only_func.get("name"), str) else ""
                            only_id = only_tc.get("id") if isinstance(only_tc.get("id"), str) else ""
                            only_args = only_func.get("arguments", "")
                            if (
                                only_name in COMPRESSED_TOOLS
                                and only_id
                                and isinstance(only_args, str)
                                and only_args
                                and only_id in tool_response_ids
                                and _is_successful_tool_response(
                                    tool_response_by_id.get(only_id, {}).get("content")
                                )
                            ):
                                compressible_tool_call = only_tc

                if compressible_tool_call is not None:
                    # Build the SYSTEM MESSAGE replacement and emit it
                    # in place of the assistant message. The matching
                    # tool_response will be dropped when we encounter
                    # it later in the iteration.
                    response_msg = tool_response_by_id.get(
                        compressible_tool_call.get("id", ""), {}
                    )
                    reasoning_excerpt = _excerpt_for_system_log(
                        msg.get("reasoning") if isinstance(msg.get("reasoning"), str) else "",
                        compression_reasoning_trunc_chars,
                    )
                    fragment = _format_compressed_tool_call(
                        compressible_tool_call, response_msg, reasoning_excerpt
                    )
                    sanitized.append(_build_system_message_user([fragment]))
                    drop_tool_response_ids.add(compressible_tool_call.get("id", ""))
                    # The single tool_call is consumed; do NOT add the
                    # original assistant message at all (its reasoning
                    # has been folded into the SYSTEM MESSAGE excerpt).
                    continue

                for tc in tool_calls:
                    if not isinstance(tc, dict):
                        continue
                    tc_id = tc.get("id")
                    if not isinstance(tc_id, str) or not tc_id:
                        continue
                    func = tc.get("function") if isinstance(tc.get("function"), dict) else None
                    if not isinstance(func, dict):
                        continue
                    args = func.get("arguments", "")
                    if not isinstance(args, str) or args == "":
                        # Stream cut before args delta arrived; provider
                        # rejects empty args. Drop.
                        continue
                    try:
                        json.loads(args)
                    except (json.JSONDecodeError, TypeError, ValueError):
                        # Malformed JSON — stream cut mid-arg or model error.
                        # Drop so the provider never sees broken JSON and the
                        # model can retry on the next turn.
                        continue
                    if tc_id not in tool_response_ids:
                        # Orphan: no tool message in this batch will pair
                        # with us. Drop to preserve the strict invariant.
                        continue
                    if tc_id in consumed_tool_call_ids:
                        # Duplicate of a previous tool_call whose
                        # response was already consumed. Drop.
                        continue
                    if tc_id in pending_tool_call_ids:
                        # Two tool_calls in a row with the same id and
                        # no tool message between them. Drop the second.
                        continue
                    surviving_calls.append(tc)
                    pending_tool_call_ids.add(tc_id)

                if surviving_calls:
                    msg["tool_calls"] = surviving_calls
                else:
                    msg.pop("tool_calls", None)

            content_is_empty = _is_effectively_empty_text(msg.get("content"))
            has_surviving_tool_calls = bool(msg.get("tool_calls"))
            if content_is_empty and not has_surviving_tool_calls:
                continue

            sanitized.append(msg)
            continue

        if role == "tool":
            tc_id = msg.get("tool_call_id")
            if not isinstance(tc_id, str) or not tc_id:
                # Malformed tool message without an id — cannot pair.
                continue
            if tc_id in drop_tool_response_ids:
                # Companion of a tool_call we just compressed into a
                # SYSTEM MESSAGE. Drop silently — the SYSTEM MESSAGE
                # already conveys what this response carried.
                continue
            if tc_id not in pending_tool_call_ids:
                # Either no preceding tool_call (orphan) or the call was
                # already consumed by a previous tool message. Drop.
                continue
            pending_tool_call_ids.discard(tc_id)
            consumed_tool_call_ids.add(tc_id)
            if msg.get("content") is None:
                msg["content"] = ""
            sanitized.append(msg)
            continue

        # role == "system" or anything unknown: forward as-is.
        sanitized.append(msg)

    return sanitized


def _write_session_json(label: str, data: Any, session_id: str = "") -> Path:
    """Write a JSON blob to the session directory.
    Also writes a standalone payload.json when the data is a request wrapper.
    """
    import os
    from pathlib import Path as _Path

    sessions_env = os.environ.get("SOT_SESSIONS_DIR")
    if sessions_env:
        sessions_base = _Path(sessions_env).resolve()
    else:
        sessions_base = _Path(".sot-cli/sessions").resolve()

    if session_id:
        base = sessions_base / session_id
    else:
        base = _Path(".sot-cli/session-json").resolve()

    base.mkdir(parents=True, exist_ok=True)
    path = base / f"{label}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)

    # Also write a standalone payload.json for easy Postman/curl debugging
    if label == "request" and isinstance(data, dict) and "payload" in data:
        payload_path = base / "payload.json"
        with open(payload_path, "w", encoding="utf-8") as f:
            json.dump(data["payload"], f, ensure_ascii=True, indent=2, default=str)

    return path


class OpenAICompatibleAdapter:
    def __init__(
        self,
        name: str,
        base_url: str,
        api_key: str,
        model: str = "",
        extra_headers: dict[str, str] | None = None,
        provider_selection: str | None = None,
    ) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.extra_headers = extra_headers or {}
        self.provider_selection = provider_selection
        # Start with nothing — detect_capabilities will populate this
        self.capability = ProviderCapability()
        self._capabilities_detected = False

    async def detect_capabilities(self) -> None:
        """Query the provider's models endpoint to detect what the current model supports."""
        if self._capabilities_detected:
            return
        if self.name == "lmstudio":
            await self._detect_lmstudio_capabilities()
        elif self.name == "openrouter":
            await self._detect_openrouter_capabilities()
        elif self.name == "ollama":
            await self._detect_ollama_capabilities()
        elif self.name == "nvidia":
            await self._detect_nvidia_capabilities()
        elif self.name == "bedrock":
            await self._detect_bedrock_capabilities()
        elif self.name == "openai":
            # OpenAI's /v1/models endpoint doesn't expose tool/modality flags or
            # context windows in a useful shape, and the same `openai` provider
            # is reused to talk to any OpenAI-compatible service (so probing
            # would also be unreliable). We assume the optimistic defaults of
            # current frontier OpenAI-style models: tools on, vision + PDFs on,
            # 400k context. If a downstream model is more limited, the API
            # itself will reject the unsupported feature at request time —
            # which is the right place to surface that.
            self.capability = ProviderCapability(
                supports_tools=True,
                supports_images=True,
                supports_pdfs=True,
                context_length=400_000,
                modality="text+image->text",
            )
        else:
            # xai and other unknown OpenAI-compatible names — minimal default.
            self.capability = ProviderCapability(supports_tools=True)
        self._capabilities_detected = True

    async def _detect_openrouter_capabilities(self) -> None:
        """OpenRouter: GET /models returns architecture.input_modalities and supported_parameters."""
        try:
            headers = {"Authorization": f"Bearer {self.api_key}", **self.extra_headers}
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(f"{self.base_url}/models", headers=headers)
                if resp.status_code == 401:
                    raise ValueError("Invalid API key for OpenRouter.")
                if resp.status_code != 200:
                    raise RuntimeError(f"Failed to fetch models from OpenRouter (HTTP {resp.status_code}).")
                models = resp.json().get("data",[])
        except Exception as exc:
            err_msg = str(exc)
            if "SSL_CERT_FILE" in err_msg or "No such file" in err_msg:
                import sys
                sys.stderr.write(f"[Warning] SSL certificate issue (check SSL_CERT_FILE env var): {exc}\n")
                sys.stderr.flush()
                self.capability = ProviderCapability(supports_tools=True)
                self._capabilities_detected = True
                return
            raise RuntimeError(f"Could not connect to OpenRouter at {self.base_url}: {exc}") from exc

        model_info = None
        for m in models:
            if m.get("id") == self.model or m.get("id", "").startswith(self.model):
                model_info = m
                break

        if model_info is None:
            raise ValueError(f"Model '{self.model}' not found in OpenRouter.")

        arch = model_info.get("architecture", {})
        input_mods = arch.get("input_modalities", [])
        params = model_info.get("supported_parameters", [])
        top = model_info.get("top_provider", {}) or {}

        self.capability = ProviderCapability(
            supports_tools="tools" in params,
            supports_images="image" in input_mods,
            supports_pdfs="file" in input_mods,
            supports_audio="audio" in input_mods,
            supports_video="video" in input_mods,
            context_length=model_info.get("context_length") or top.get("context_length"),
            max_completion_tokens=top.get("max_completion_tokens"),
            modality=arch.get("modality", ""),
        )

    async def _detect_lmstudio_capabilities(self) -> None:
        """LM Studio: Use native API to find loaded models and capabilities."""
        origin = _extract_origin(self.base_url)
        try:
            headers = {}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            async with httpx.AsyncClient(timeout=10.0) as client:
                # Try native v1 API first (LM Studio 0.4.0+)
                resp = await client.get(f"{origin}/api/v1/models", headers=headers)
                if resp.status_code != 200:
                    # Try native v0 API
                    resp = await client.get(f"{origin}/api/v0/models", headers=headers)
                if resp.status_code != 200:
                    # Fallback to OpenAI compat
                    resp = await client.get(f"{origin}/v1/models", headers=headers)
                if resp.status_code != 200:
                    raise RuntimeError(f"Failed to fetch models from LM Studio (HTTP {resp.status_code}).")
                data = resp.json()
        except httpx.RequestError as exc:
            raise RuntimeError(f"Could not connect to LM Studio at {origin}. Is it running?") from exc

        models_list = data.get("models", data.get("data",[]))
        if not models_list:
            raise ValueError("No models found in LM Studio. Please download and load a model.")

        model_info = None
        if not self.model:
            # Look specifically for a LOADED model
            for m in models_list:
                if m.get("state") == "loaded" or m.get("loaded_instances"):
                    model_info = m
                    break
            
            if model_info is None:
                raise ValueError("No model is currently loaded in LM Studio. Please load a model or specify one in sot.toml.")
                
            self.model = model_info.get("id", model_info.get("key", ""))
        else:
            for m in models_list:
                key = m.get("key", m.get("id", ""))
                if key == self.model or self.model in key:
                    model_info = m
                    break
            if model_info is None:
                raise ValueError(f"Model '{self.model}' not found in LM Studio.")

        caps = model_info.get("capabilities", {})
        quant = model_info.get("quantization", {}) or {}
        
        allocated_context_length = None
        loaded_instances = model_info.get("loaded_instances")
        if isinstance(loaded_instances, list) and len(loaded_instances) > 0:
            config = loaded_instances[0].get("config", {})
            if isinstance(config, dict):
                allocated_context_length = config.get("context_length")

        self.capability = ProviderCapability(
            supports_tools=bool(caps.get("trained_for_tool_use", False)),
            supports_images=bool(caps.get("vision", False)),
            supports_pdfs=False,
            supports_audio=False,
            supports_video=False,
            context_length=model_info.get("max_context_length"),
            allocated_context_length=allocated_context_length,
            quantization=quant.get("name", ""),
            parameter_count=model_info.get("params_string", ""),
        )

    async def _detect_ollama_capabilities(self) -> None:
        """Ollama: Use /api/ps to find the currently running model and allocated context."""
        origin = _extract_origin(self.base_url)
        allocated_context_length = None
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                # Always check /api/ps to get the allocated context length of running models
                resp_ps = await client.get(f"{origin}/api/ps")
                if resp_ps.status_code == 200:
                    running_models = resp_ps.json().get("models",[])
                    if not self.model and running_models:
                        self.model = running_models[0].get("name", "")
                    
                    # If we have a model, see if it's currently running to get its actual allocated context
                    if self.model:
                        for rm in running_models:
                            if rm.get("name") == self.model or rm.get("model") == self.model:
                                allocated_context_length = rm.get("context_length")
                                break

                if not self.model:
                    raise ValueError("No model is currently running in Ollama. Please run a model first or specify one in sot.toml.")

                resp = await client.post(
                    f"{origin}/api/show",
                    json={"model": self.model},
                )
                if resp.status_code == 404:
                    raise ValueError(f"Model '{self.model}' not found in Ollama. Did you pull it?")
                if resp.status_code != 200:
                    raise RuntimeError(f"Failed to fetch model info from Ollama (HTTP {resp.status_code}).")
                data = resp.json()
        except httpx.RequestError as exc:
            raise RuntimeError(f"Could not connect to Ollama at {origin}. Is the Ollama service running?") from exc

        details = data.get("details", {}) or {}
        model_info = data.get("model_info", {}) or {}
        capabilities: list[str] = data.get("capabilities") or[]

        context_length: int | None = None
        for key, val in model_info.items():
            if key.endswith(".context_length") and isinstance(val, int):
                context_length = val
                break

        quantization = str(details.get("quantization_level", "")).strip()
        parameter_count = str(details.get("parameter_size", "")).strip()

        self.capability = ProviderCapability(
            supports_tools="tools" in capabilities,
            supports_images="vision" in capabilities,
            supports_pdfs=False,
            supports_audio=False,
            supports_video=False,
            context_length=context_length,
            allocated_context_length=allocated_context_length,
            quantization=quantization,
            parameter_count=parameter_count,
        )

    async def _detect_nvidia_capabilities(self) -> None:
        """NVIDIA API: Use /models endpoint to verify connectivity and list available models."""
        try:
            headers = {"Authorization": f"Bearer {self.api_key}", **self.extra_headers}
            async with httpx.AsyncClient(timeout=15.0) as client:
                # NVIDIA usa la base_url completa para el endpoint de modelos (ej. /v1/models)
                resp = await client.get(f"{self.base_url}/models", headers=headers)
                if resp.status_code == 401:
                    raise ValueError("Invalid API key for NVIDIA API.")
                if resp.status_code != 200:
                    raise RuntimeError(f"Failed to fetch models from NVIDIA API (HTTP {resp.status_code}).")
                models = resp.json().get("data", [])
        except httpx.RequestError as exc:
            raise RuntimeError(f"Could not connect to NVIDIA API at {self.base_url}. Check your internet connection.") from exc

        if not models:
            raise ValueError("No models found in NVIDIA API. Check your API key or network.")

        # El endpoint /v1/models de NVIDIA devuelve una lista simple sin metadatos de arquitectura.
        # Asumimos capacidades estándar OpenAI-compatible para proveedores de API.
        self.capability = ProviderCapability(
            supports_tools=True,
            supports_images=False,
            supports_pdfs=False,
            supports_audio=False,
            supports_video=False,
        )

    async def _detect_bedrock_capabilities(self) -> None:
        """Amazon Bedrock Mantle: verify connectivity via /v1/models.
        All models default to 256k context and multimodal (vision+PDF+tools).
        Falls back to sensible defaults on any error."""
        try:
            headers = {"Authorization": f"Bearer {self.api_key}", **self.extra_headers}
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(f"{self.base_url}/models", headers=headers)
                if resp.status_code != 200:
                    raise RuntimeError(f"Bedrock Mantle returned HTTP {resp.status_code}")
                models_data = resp.json().get("data", [])
                if not models_data:
                    raise RuntimeError("No models returned from Bedrock Mantle")
        except Exception:
            # Cannot reach Mantle — fall back to optimistic defaults
            self.capability = ProviderCapability(
                supports_tools=True,
                supports_images=True,
                supports_pdfs=True,
                context_length=256_000,
            )
            return

        self.capability = ProviderCapability(
            supports_tools=True,
            supports_images=True,
            supports_pdfs=True,
            context_length=256_000,
        )


    async def stream_turn(self, request: ProviderRequest):
        url = f"{self.base_url}/chat/completions"
        resolved_model = self.model or request.model
        payload = build_chat_completions_payload(request, resolved_model)
        if self.name == "openrouter" and self.provider_selection:
            payload["provider"] = {"order": [self.provider_selection], "allow_fallbacks": False}
        headers = {
            "Content-Type": "application/json",
            **self.extra_headers,
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        _write_session_json("request", {"url": url, "payload": payload}, session_id=request.session_id)
        _write_session_json("payload", payload, session_id=request.session_id)

        raw_chunks: list[dict[str, Any]] = []

        timeout = httpx.Timeout(connect=10.0, read=None, write=60.0, pool=60.0)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream("POST", url, headers=headers, json=payload) as response:
                    if response.is_error:
                        body = (await response.aread()).decode("utf-8", errors="replace")
                        _write_session_json("error", {"status": response.status_code, "body": body}, session_id=request.session_id)
                        raise RuntimeError(f"Provider request failed ({response.status_code}): {body}")

                    async for line in response.aiter_lines():
                        if not line or line.startswith(":"):
                            continue
                        if not line.startswith("data:"):
                            continue

                        data = line[5:].strip()
                        if not data:
                            continue
                        
                        # Skip [DONE] marker, but process all chunks including usage before it
                        if data == "[DONE]":
                            continue

                        try:
                            chunk = json.loads(data)
                            raw_chunks.append(chunk)
                            for event in _events_from_chunk(chunk):
                                yield event
                        except json.JSONDecodeError:
                            # Skip malformed chunks
                            continue
        except httpx.HTTPError as exc:
            raise RuntimeError(f"Could not reach provider '{self.name}' at {self.base_url}: {exc}") from exc

        if raw_chunks:
            _write_session_json("response-chunks", raw_chunks, session_id=request.session_id)

        yield ProviderEvent(type="done")

    async def complete_turn(self, request: ProviderRequest) -> ProviderCompletion:
        url = f"{self.base_url}/chat/completions"
        resolved_model = self.model or request.model
        payload = build_chat_completions_payload(request, resolved_model)
        payload["stream"] = False
        if self.name == "openrouter" and self.provider_selection:
            payload["provider"] = {"order": [self.provider_selection], "allow_fallbacks": False}
        headers = {
            "Content-Type": "application/json",
            **self.extra_headers,
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        _write_session_json("request", {"url": url, "payload": payload}, session_id=request.session_id)
        _write_session_json("payload", payload, session_id=request.session_id)

        timeout = httpx.Timeout(connect=10.0, read=120.0, write=60.0, pool=60.0)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(url, headers=headers, json=payload)
                if response.is_error:
                    _write_session_json("error", {"status": response.status_code, "body": response.text}, session_id=request.session_id)
                    raise RuntimeError(f"Provider request failed ({response.status_code}): {response.text}")
                body = response.json()
        except httpx.HTTPError as exc:
            raise RuntimeError(f"Could not reach provider '{self.name}' at {self.base_url}: {exc}") from exc

        _write_session_json("response", body, session_id=request.session_id)

        choice = (body.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        content = message.get("content")
        text = _extract_text(content)
        tool_calls = message.get("tool_calls") or []
        usage = body.get("usage") or {}
        return ProviderCompletion(
            assistant_message=message,
            text=text,
            tool_calls=tool_calls,
            usage=usage,
        )


def _is_openai_reasoning_model(model: str) -> bool:
    """Detect OpenAI reasoning-class models from their canonical name prefix.

    Heuristic — OpenAI does not expose a programmatic capability flag for this,
    and the wire-level differences are baked into model families:

    - `gpt-5*` family (gpt-5, gpt-5-mini, gpt-5-nano, gpt-5.1, gpt-5.2,
      gpt-5.4, gpt-5.5, gpt-5-pro, gpt-5.1-codex, gpt-5.1-codex-max,
      gpt-5.1-codex-mini, plus dated variants like `gpt-5-nano-2025-08-07`).
    - O-series: `o1`, `o1-mini`, `o1-preview`, `o3`, `o3-mini`, `o3-pro`,
      `o4-mini`, dated variants like `o4-mini-2025-04-16`.

    Non-reasoning OpenAI families (`gpt-4*`, `gpt-3.5-*`, `chatgpt-4o-*`)
    return False so they keep getting `temperature`.

    Returns False for empty/None inputs and for anything that doesn't match
    the prefixes above (covers ad-hoc OpenAI-compatible deployments behind
    the same `openai` adapter — those models are user-defined and we have no
    way to know if they're reasoning-class, so we default to "treat as a
    standard chat model").
    """
    if not model:
        return False
    m = model.lower().strip()
    if m.startswith("gpt-5"):
        return True
    # o-series: name is `o<digit>` followed by either end-of-string, hyphen,
    # or dot. Avoids false positives on names like `openai-...` or `oss-...`.
    if len(m) >= 2 and m[0] == "o" and m[1].isdigit():
        return len(m) == 2 or m[2] in "-."
    return False


def build_chat_completions_payload(request: ProviderRequest, resolved_model: str) -> dict[str, Any]:
    raw_messages = request.conversation_messages or [
        {"role": "system", "content": request.system_prompt},
        {
            "role": "user",
            "content": build_user_turn_message(
                request.user_prompt,
                request.source_index,
                request.source_contents,
            ),
        },
    ]
    # Strict-API firewall + SoT-aware compression of closed turns. See
    # _sanitize_messages_for_provider's docstring for the full rationale;
    # in short: drops malformed null-content shapes that crash strict
    # validators (LM Studio HTTP 500, OpenAI HTTP 400), enforces strict
    # tool_call ↔ tool-response pairing, truncates the reasoning of
    # tool-bearing assistants in closed turns, and replaces successful
    # write_file/edit_files (assistant + tool_response) pairs with a
    # compact "SYSTEM MESSAGE: ..." log line. The active turn is never
    # touched. Compression stays deterministic so prefix-matching
    # prompt caches keep hitting across rounds and turns.
    messages = _sanitize_messages_for_provider(
        raw_messages,
        compression_reasoning_trunc_chars=request.compression_reasoning_trunc_chars,
    )

    is_openai = request.provider_name == "openai"
    is_openrouter = request.provider_name == "openrouter"
    # Only flips the wire-level treatment of OpenAI Chat Completions params.
    # Not applied to openrouter even when routing an OpenAI reasoning model
    # through it, because OpenRouter normalizes/strips unsupported params on
    # its end before passing to upstream — so we must keep the universal
    # OpenAI-compatible shape for openrouter.
    openai_is_reasoning = is_openai and _is_openai_reasoning_model(resolved_model)

    payload: dict[str, Any] = {
        "model": resolved_model,
        "messages": messages,
        "stream": request.stream,
    }

    # Output token cap field — OpenAI deprecated `max_tokens` chat-completions-
    # wide and reasoning-class models reject it with HTTP 400
    # `unsupported_parameter`. Use `max_completion_tokens` for openai
    # unconditionally (non-reasoning openai models still accept the new name)
    # and keep `max_tokens` for everyone else, since most OpenAI-compatible
    # servers in the wild (vLLM, llama.cpp server, Ollama, LM Studio, NVIDIA
    # NIM) only know the legacy field name.
    if is_openai:
        payload["max_completion_tokens"] = request.max_output_tokens
    else:
        payload["max_tokens"] = request.max_output_tokens

    # Sampling parameters that OpenAI reasoning models reject (HTTP 400):
    # temperature, top_p, presence_penalty, frequency_penalty, logprobs,
    # top_logprobs, logit_bias. The codebase only sends `temperature` today,
    # so that's the only one we have to gate. Skipping it for OpenAI's
    # reasoning class lets the model use its baked-in default (effectively 1,
    # but it's not even an addressable knob for these models). All other
    # providers always get `temperature`.
    if not openai_is_reasoning:
        payload["temperature"] = request.temperature

    if request.stream:
        payload["stream_options"] = {"include_usage": True}

    if request.enable_tools and request.tools:
        tools = request.tools
        if is_openai:
            # OpenAI's tool-call validator rejects schemas that use
            # oneOf/anyOf/allOf/not at the TOP LEVEL of `function.parameters`
            # (HTTP 400: "schema must have type 'object' and not have ...").
            # Other providers in this codebase (openrouter, lmstudio, ollama,
            # nvidia) accept the same constructs without complaint, so we
            # only sanitize for openai. The runtime tool handlers already
            # enforce equivalent constraints in Python (e.g. `attach/detach
            # path` raises ValueError when both `path` and `paths` are absent),
            # so dropping these keys does not weaken correctness — it only
            # removes a schema-level hint for the model, which is already
            # described in the tool's natural-language description.
            tools = [_sanitize_tool_schema_for_openai(t) for t in tools]
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    # OpenRouter / Bedrock reasoning effort — nested object format
    # OpenRouter docs: https://openrouter.ai/docs/features/reasoning
    # Bedrock Mantle is OpenAI-compatible and accepts the same format.
    if (is_openrouter or request.provider_name == "bedrock") and request.reasoning_effort:
        payload["reasoning"] = {"effort": request.reasoning_effort}


    # Reasoning effort wire format diverges per provider — and OpenAI in
    # particular rejects unknown top-level keys with HTTP 400, so we MUST NOT
    # forward this field to anyone but the two providers that document it,
    # AND only on models that actually accept it.
    #
    return payload


# Top-level keys forbidden by OpenAI inside `function.parameters`. The error
# message from the API enumerates exactly these: "schema must have type
# 'object' and not have 'oneOf'/'anyOf'/'allOf'/'enum'/'not' at the top level".
# `enum` is included for completeness even though it's vanishingly rare on a
# top-level parameters object (whose `type` is normally "object").
_OPENAI_FORBIDDEN_TOP_LEVEL_SCHEMA_KEYS: frozenset[str] = frozenset(
    {"oneOf", "anyOf", "allOf", "not", "enum"}
)


def _sanitize_tool_schema_for_openai(tool: dict[str, Any]) -> dict[str, Any]:
    """Return a shallow-cloned copy of `tool` with the forbidden top-level
    schema keys stripped from `function.parameters`.

    Only the top level of `parameters` is touched. Constructs nested deeper
    inside individual property schemas (e.g. a property whose schema uses
    `enum`) are left alone — OpenAI accepts those. The original `tool` dict
    is not mutated; sibling keys (`type`, `properties`, `required`,
    `additionalProperties`, `description`, …) survive untouched.
    """
    sanitized = dict(tool)
    func = sanitized.get("function")
    if not isinstance(func, dict):
        return sanitized
    func = dict(func)
    sanitized["function"] = func
    params = func.get("parameters")
    if not isinstance(params, dict):
        return sanitized
    params = dict(params)
    func["parameters"] = params
    for forbidden in _OPENAI_FORBIDDEN_TOP_LEVEL_SCHEMA_KEYS:
        params.pop(forbidden, None)
    return sanitized


def _events_from_chunk(chunk: dict[str, Any]) -> list[ProviderEvent]:
    events: list[ProviderEvent] = []

    usage = chunk.get("usage")
    if usage:
        events.append(ProviderEvent(type="usage", payload={"usage": usage}))

    for choice in chunk.get("choices", []):
        delta = choice.get("delta") or choice.get("message") or {}

        reasoning_text, reasoning_details = _extract_reasoning_payload(delta)
        if reasoning_text or reasoning_details:
            events.append(
                ProviderEvent(
                    type="reasoning_delta",
                    payload={"text": reasoning_text, "details": reasoning_details},
                )
            )

        content = delta.get("content")
        text = _extract_text(content)
        if text:
            events.append(ProviderEvent(type="text_delta", payload={"text": text}))

        tool_calls = delta.get("tool_calls") or []
        if tool_calls:
            events.append(ProviderEvent(type="tool_call", payload={"tool_calls": tool_calls}))

        # Detect abrupt stream termination: finish_reason == "length" means the
        # model hit its max_output_tokens limit mid-response. Emit a "finished"
        # event so the stream handlers can warn the user and abort cleanly
        # instead of letting the model retry into an infinite loop.
        # OpenRouter wraps native_finish_reason when the real reason differs
        # from the standard field (e.g. finish_reason="tool_calls" but
        # native_finish_reason="length" — model was cut off mid-tool-call).
        finish_reason = choice.get("finish_reason")
        native_finish_reason = choice.get("native_finish_reason")
        if native_finish_reason:
            events.append(ProviderEvent(type="finished", payload={"finish_reason": native_finish_reason}))
        elif finish_reason:
            events.append(ProviderEvent(type="finished", payload={"finish_reason": finish_reason}))

    return events


def _extract_origin(url: str) -> str:
    """Extract scheme + host + port from a URL, stripping any path.
    'http://192.168.1.169:1234/v1' -> 'http://192.168.1.169:1234'
    """
    from urllib.parse import urlparse
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.hostname:
        return url.rstrip("/")
    origin = f"{parsed.scheme}://{parsed.hostname}"
    if parsed.port:
        origin += f":{parsed.port}"
    return origin


def _extract_text(content: Any) -> str:
    if isinstance(content, str):
        return content

    if isinstance(content, list):
        text_parts: list[str] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") in {"text", "output_text"} and isinstance(part.get("text"), str):
                text_parts.append(part["text"])
        return "".join(text_parts)

    return ""


def _extract_reasoning_payload(delta: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    if not isinstance(delta, dict):
        return "", []

    reasoning_parts: list[str] = []
    reasoning_details: list[dict[str, Any]] = []

    details = delta.get("reasoning_details")
    if isinstance(details, list):
        for item in details:
            if not isinstance(item, dict):
                continue
            reasoning_details.append(item)
            detail_text = _extract_reasoning_detail_text(item)
            if detail_text:
                reasoning_parts.append(detail_text)

    # Some providers emit equivalent reasoning in both `reasoning_details`
    # and legacy string fields. Prefer details when present to avoid
    # duplicate visible thinking output.
    if not reasoning_parts:
        for key in ("reasoning_content", "reasoning", "thinking"):
            value = delta.get(key)
            if isinstance(value, str) and value:
                reasoning_parts.append(value)

    return "".join(reasoning_parts), reasoning_details


def _extract_reasoning_detail_text(detail: dict[str, Any]) -> str:
    detail_type = str(detail.get("type", "")).strip()
    if detail_type == "reasoning.text":
        text = detail.get("text")
        if isinstance(text, str):
            return text
    if detail_type == "reasoning.summary":
        summary = detail.get("summary")
        if isinstance(summary, str):
            return summary
    if detail_type == "reasoning.encrypted":
        return ""

    for key in ("text", "summary", "content"):
        value = detail.get(key)
        if isinstance(value, str) and value:
            return value
    return ""