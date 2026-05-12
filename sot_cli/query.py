from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
import sys
from typing import Any

from sot_cli.spinners import SPINNERS
from rich.console import Console

from sot_cli.constants import (
    FALLBACK_DELEGATED_MAX_ROUNDS,
    FALLBACK_DELEGATED_REASONING_CHAR_BUDGET,
    FALLBACK_DELEGATED_REPEAT_LIMIT,
    FALLBACK_REASONING_CHAR_BUDGET,
    FALLBACK_REPEAT_LIMIT,
    SESSION_MUTATION_TOOLS,
)
from sot_cli.message_builder import build_previous_turn_metadata_message
from sot_cli.providers.base import ProviderAdapter, ProviderRequest
from sot_cli.runtime import AppRuntime
from sot_cli.sot import (
    SoTState,
    begin_turn,
    build_sot_payload_message,
    merge_session_into_tracked,
    refresh_tracked_state_from_disk,
    update_tracked_from_tool_result,
)
from sot_cli.tools.core import ToolExecutionResult
from sot_cli.tools import ToolRegistry


# we wait for the model. Pure ASCII so it renders the same on any
# terminal (Windows cmd, PowerShell, iTerm, Terminal.app, tmux, etc).
#
_REASONING_NEWLINE_RUN_RE = re.compile(r"\n{3,}")


def _normalize_reasoning_whitespace(text: str) -> str:
    """Collapse pathological newline runs in reasoning text to a paragraph break.

    Reasoning streams from some providers (notably DeepSeek-V4 via OpenRouter)
    contain dense runs of newline-only deltas — ``"\\n\\n\\n\\n\\n"`` followed
    by a single word followed by another ``"\\n\\n\\n"``, etc. These appear
    to be an artifact of the model's internal step pacing and carry zero
    semantic information for downstream reasoning continuity. Collapsing any
    run of three or more consecutive ``\\n`` to a single paragraph break
    (``\\n\\n``) keeps every paragraph boundary the model produced while
    cutting wire bytes meaningfully on long reasoning chains. Runs of one or
    two newlines are left untouched (those ARE meaningful — single-line and
    paragraph breaks).
    """
    if not text or "\n\n\n" not in text:
        return text
    return _REASONING_NEWLINE_RUN_RE.sub("\n\n", text)


def _consolidate_reasoning_details(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge consecutive per-token reasoning deltas into compact entries.

    Streaming providers emit one ``reasoning_details`` entry per token chunk:

    .. code-block:: json

        [
            {"type": "reasoning.text", "text": "The",   "format": "unknown", "index": 0},
            {"type": "reasoning.text", "text": " user", "format": "unknown", "index": 0},
            ...
        ]

    Sending those back verbatim is functionally accepted by every provider,
    but it bloats the payload absurdly — a 30-token reasoning becomes 30
    JSON objects with repeated ``type`` / ``format`` / ``index`` metadata.
    Consolidating consecutive entries that share ``(type, format, index)``
    into a single entry by concatenating their ``text`` fields cuts the
    wire size by 10-100x while preserving the exact reasoning content the
    provider needs for continuity. The merged ``text`` is also passed
    through :func:`_normalize_reasoning_whitespace` to collapse pathological
    newline runs (``\\n\\n\\n+`` → ``\\n\\n``) — paragraph structure is
    preserved, but the dense whitespace some reasoning models emit between
    thoughts is squeezed out.

    Non-text reasoning blocks (``reasoning.encrypted`` blobs used by
    Anthropic / GPT-5 encrypted reasoning, ``reasoning.summary``, etc.)
    are atomic units — never merged, never normalized. Likewise, two
    ``reasoning.text`` blocks that DIFFER in ``format`` or ``index`` stay
    separate, because the model treats them as distinct reasoning steps.
    """
    if not raw:
        return raw

    consolidated: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    def _is_mergeable_text(d: dict[str, Any]) -> bool:
        return d.get("type") == "reasoning.text" and isinstance(d.get("text"), str)

    def _same_group(a: dict[str, Any], b: dict[str, Any]) -> bool:
        return (
            _is_mergeable_text(a)
            and _is_mergeable_text(b)
            and a.get("format") == b.get("format")
            and a.get("index") == b.get("index")
        )

    def _finalize(entry: dict[str, Any]) -> dict[str, Any]:
        # Normalize whitespace ONLY on text-class reasoning. Encrypted /
        # summary blocks are atomic and must not be touched.
        if _is_mergeable_text(entry):
            entry["text"] = _normalize_reasoning_whitespace(entry.get("text", ""))
        return entry

    for entry in raw:
        if not isinstance(entry, dict):
            continue
        if current is not None and _same_group(current, entry):
            current["text"] = current.get("text", "") + entry.get("text", "")
        else:
            if current is not None:
                consolidated.append(_finalize(current))
            current = dict(entry)

    if current is not None:
        consolidated.append(_finalize(current))

    return consolidated


@dataclass(frozen=True)
class RoundObservation:
    signature: str
    summary: str
    is_error: bool


@dataclass
class TurnResult:
    text: str = ""
    reasoning: str = ""
    reasoning_details: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    is_error: bool = False
    finished_reason: str = ""  # "length" if the model hit max_output_tokens


@dataclass
class ConversationState:
    chat_history: list[dict[str, Any]] = field(default_factory=list)
    sot: SoTState = field(default_factory=SoTState)
    last_turn_metadata: dict[str, Any] | None = None


@dataclass
class StreamRenderState:
    reasoning_started: bool = False
    text_started: bool = False
    # True when the stdout cursor is at column 0 (last visible char written
    # was "\n", or nothing has been written yet). Tracked so that runtime
    # meta output (e.g. the "tool_call:" header we print before a call) can
    # guarantee exactly ONE fresh line — without stacking extra newlines on
    # top of whatever trailing "\n" the provider already emitted inside the
    # reasoning/text chunks. The chunks themselves are still written
    # verbatim; only our own separator is conditional.
    at_line_start: bool = True


def _stream_chunk(state: StreamRenderState, text: str, ansi_prefix: str = "", ansi_suffix: str = "") -> None:
    """Write a provider chunk verbatim (optionally wrapped in ANSI style).

    The ANSI wrapper is invisible in terms of cursor column, so the
    at_line_start tracking is based on the raw `text` payload, not on the
    wrapped bytes.
    """
    if not text:
        return
    sys.stdout.write(ansi_prefix + text + ansi_suffix)
    sys.stdout.flush()
    state.at_line_start = text.endswith("\n")


def _ensure_fresh_line(state: StreamRenderState) -> None:
    """Emit a single "\n" only if the cursor is not already at column 0."""
    if not state.at_line_start:
        sys.stdout.write("\n")
        sys.stdout.flush()
        state.at_line_start = True


def _write_meta(state: StreamRenderState, text: str, ends_on_newline: bool) -> None:
    """Write runtime meta output (headers/separators) and update line-state.

    Caller declares whether the literal payload ends on "\n". ANSI sequences
    inside `text` do not affect cursor column, so the declaration only
    reflects the plain-text tail.
    """
    if not text:
        return
    sys.stdout.write(text)
    sys.stdout.flush()
    state.at_line_start = ends_on_newline


async def run_single_turn(
    adapter: ProviderAdapter,
    request: ProviderRequest,
    console: Console,
    show_thinking: bool = True,
    show_full: bool = True,
    reasoning_char_budget: int = 0,
) -> TurnResult:
    # Easter Egg: Add demon emoji if model is uncensored/nsfw
    if any(k in request.model.lower() for k in ["uncensored", "uncensor", "abliterated", "obliterated", "nsfw"]) and "😈" not in request.model:
        request.model += " 😈"

    result = TurnResult()
    render_state = StreamRenderState()
    _tool_call_header_shown: set[int] = set()
    reasoning_chars = 0
    reasoning_budget_tripped = False

    stream = adapter.stream_turn(request)
    try:
        async for event in stream:
            if event.type == "reasoning_delta":
                text = str(event.payload.get("text", ""))
                details = event.payload.get("details") or []
                if text:
                    result.reasoning += text
                    reasoning_chars += len(text)
                    if show_thinking:
                        if not render_state.reasoning_started:
                            _write_meta(render_state, "\x1b[2mthinking:\x1b[0m ", ends_on_newline=False)
                            render_state.reasoning_started = True
                        # Verbatim: whatever the provider sent, print it as-is
                        # inside the dim-style envelope. No regex, no dedup.
                        _stream_chunk(render_state, text, ansi_prefix="\x1b[2m", ansi_suffix="\x1b[0m")
                if isinstance(details, list):
                    for detail in details:
                        if isinstance(detail, dict):
                            result.reasoning_details.append(detail)
                if reasoning_char_budget and reasoning_chars >= reasoning_char_budget:
                    reasoning_budget_tripped = True
                    break
            elif event.type == "text_delta":
                text = str(event.payload.get("text", ""))
                result.text += text
                if text:
                    if show_thinking and render_state.reasoning_started and not render_state.text_started:
                        # At most one blank line between reasoning and final text,
                        # regardless of how many trailing "\n" the reasoning carried.
                        _ensure_fresh_line(render_state)
                        _write_meta(render_state, "\n", ends_on_newline=True)
                    render_state.text_started = True
                    _stream_chunk(render_state, text)
            elif event.type == "tool_call":
                tool_calls = event.payload.get("tool_calls") or []
                result.tool_calls.extend(tool_calls)
                if show_full:
                    for tool_delta in tool_calls:
                        index = int(tool_delta.get("index", 0))
                        func = tool_delta.get("function") or {}
                        name = func.get("name", "")
                        args_chunk = func.get("arguments", "")
                        if name and index not in _tool_call_header_shown:
                            _tool_call_header_shown.add(index)
                            # Guarantee the header starts on a fresh line without
                            # stacking on top of trailing newlines from reasoning.
                            _ensure_fresh_line(render_state)
                            _write_meta(render_state, f"\x1b[2mtool_call: {name}(\x1b[0m", ends_on_newline=False)
                        if args_chunk:
                            _stream_chunk(render_state, args_chunk)
            elif event.type == "usage":
                usage = event.payload.get("usage") or {}
                if isinstance(usage, dict):
                    _replace_usage_snapshot(result.usage, usage)
                    _store_latest_usage_snapshot(result.usage, usage)
            elif event.type == "error":
                raise RuntimeError(str(event.payload.get("message", "Unknown provider error")))
            elif event.type == "finished":
                finish_reason = event.payload.get("finish_reason", "")
                if finish_reason:
                    result.finished_reason = finish_reason
    finally:
        await stream.aclose()

    if reasoning_budget_tripped:
        _ensure_fresh_line(render_state)
        _write_meta(
            render_state,
            f"\x1b[33m⚠  reasoning budget exceeded ({reasoning_chars} chars ≥ {reasoning_char_budget}); stream cut, continuing.\x1b[0m\n",
            ends_on_newline=True,
        )
    if show_full and _tool_call_header_shown:
        _write_meta(render_state, "\x1b[2m)\x1b[0m\n", ends_on_newline=True)
    if result.finished_reason == "length":
        _ensure_fresh_line(render_state)
        _write_meta(
            render_state,
            f"\x1b[33m⚠ Maximum output tokens reached. The response was cut off. "
            f"Increase `max_output_tokens` in your provider config for longer responses.\x1b[0m\n",
            ends_on_newline=True,
        )

    if result.text or (show_thinking and render_state.reasoning_started):
        _ensure_fresh_line(render_state)

    return result


async def run_tool_loop(
    runtime: AppRuntime,
    request: ProviderRequest,
    console: Console,
    max_rounds: int | None = None,
    conversation_state: ConversationState | None = None,
    is_task: bool = False,
) -> TurnResult:
    if conversation_state is None:
        conversation_state = ConversationState()

    begin_turn(conversation_state.sot)

    # ── SoT Step 1: Capture — save user prompt to permanent history ──
    conversation_state.chat_history.append({"role": "user", "content": request.user_prompt})

    if not request.enable_tools:
        adapter = await runtime.provider_adapter_async(request.provider_name, request.model)
        if not is_task:
            merge_session_into_tracked(runtime, request, conversation_state.sot)
            refresh_tracked_state_from_disk(runtime, adapter.capability, conversation_state.sot)
        plain_request = ProviderRequest(
            provider_name=request.provider_name,
            model=request.model,
            session_id=request.session_id,
            system_prompt=request.system_prompt,
            orchestration_rules=request.orchestration_rules,
            user_prompt=request.user_prompt,
            source_index=request.source_index,
            source_contents=request.source_contents,
            temperature=request.temperature,
            max_output_tokens=request.max_output_tokens,
            stream=request.stream,
            enable_tools=False,
            disable_delegation=request.disable_delegation,
            tools=[],
            conversation_messages=_build_payload_messages(conversation_state, request),
            compression_reasoning_trunc_chars=request.compression_reasoning_trunc_chars,
        )
        turn_result = await run_single_turn(
            adapter,
            plain_request,
            console,
            show_thinking=runtime.config.tools.show_thinking,
            show_full=runtime.config.tools.show_full,
            reasoning_char_budget=_effective_reasoning_char_budget(
                request,
                runtime.config.tools.reasoning_char_budget,
                runtime.config.tools.delegated_reasoning_char_budget,
            ),
        )
        # SoT Step 6: Clean — save assistant response to permanent history.
        # reasoning_details are consolidated before persisting: streaming
        # providers send one entry per token-delta; merging consecutive
        # entries with the same (type, format, index) collapses dozens of
        # JSON objects into one without losing any reasoning content.
        assistant_message = {"role": "assistant", "content": turn_result.text}
        if turn_result.reasoning_details:
            assistant_message["reasoning_details"] = _consolidate_reasoning_details(
                turn_result.reasoning_details
            )
        elif turn_result.reasoning:
            assistant_message["reasoning"] = turn_result.reasoning
        conversation_state.chat_history.append(assistant_message)
        return turn_result

    result = TurnResult()
    executed_any_tool = False
    previous_round_fingerprint: tuple[RoundObservation, ...] | None = None
    repeated_round_count = 0
    _tools_cfg = runtime.config.tools
    if max_rounds is None:
        max_rounds = _tools_cfg.max_rounds
    effective_max_rounds = _effective_tool_loop_max_rounds(request, max_rounds, _tools_cfg.delegated_max_rounds)
    repeat_round_limit = _repeat_round_limit(request, _tools_cfg.repeat_limit, _tools_cfg.delegated_repeat_limit)

    for round_index in range(effective_max_rounds):
        adapter = await runtime.provider_adapter_async(request.provider_name, request.model)

        # Build context info for tool validation
        ctx_cap = adapter.capability.context_length
        used_tokens = result.usage.get("latest_total_tokens")
        if used_tokens is None and conversation_state.last_turn_metadata:
            # Fallback: use raw context numbers from previous turn's metadata
            meta_ctx_length = conversation_state.last_turn_metadata.get("__ctx_length__")
            meta_prompt = conversation_state.last_turn_metadata.get("__ctx_prompt_tokens__")
            if meta_ctx_length and meta_prompt:
                if ctx_cap is None:
                    ctx_cap = meta_ctx_length
                used_tokens = meta_prompt
        if ctx_cap and used_tokens is not None:
            context_info = {
                "context_length": ctx_cap,
                "estimated_remaining": max(0, ctx_cap - used_tokens),
            }
        else:
            context_info = None

        registry = ToolRegistry(
            runtime,
            request.session_id,
            adapter.capability,
            request.model,
            request.disable_delegation,
            conversation_state.sot,  # <--- AÑADIR ESTO
            context_info=context_info,
        )

        if not is_task:
            merge_session_into_tracked(runtime, request, conversation_state.sot)
            refresh_tracked_state_from_disk(runtime, adapter.capability, conversation_state.sot)

        # ── SoT Steps 2-4: Assemble + Inject — build ephemeral payload each round ──
        payload_messages = _build_payload_messages(conversation_state, request)

        round_request = ProviderRequest(
            provider_name=request.provider_name,
            model=request.model,
            session_id=request.session_id,
            system_prompt=request.system_prompt,
            orchestration_rules=request.orchestration_rules,
            user_prompt=request.user_prompt,
            source_index=request.source_index,
            source_contents=request.source_contents,
            temperature=request.temperature,
            max_output_tokens=request.max_output_tokens,
            stream=request.stream,
            enable_tools=True,
            disable_delegation=request.disable_delegation,
            tools=registry.schemas(),
            conversation_messages=payload_messages,
            compression_reasoning_trunc_chars=request.compression_reasoning_trunc_chars,
        )

        # ── SoT Step 5: Inference ──
        # While waiting for the first chunk (or, in non-stream mode, the
        # whole completion), show the walking-robot spinner so the user
        # knows the request is in flight. Auto-degrades on non-TTY
        # consoles (logs, redirects) thanks to Rich.
        # Label is baked into the spinner frames (see SPINNERS["sot_robot"]
        # above), so the Status `text` argument is intentionally empty —
        # otherwise Rich would render the text AFTER the robot and break
        # the "Processing prompt: [robot]" order the user wants.
        prompt_status = console.status(
            "",
            spinner="sot_robot",
            spinner_style="bold bright_cyan",
        )
        if round_request.stream:
            prompt_status.start()
            try:
                completion = await _run_streaming_round(
                    adapter,
                    round_request,
                    console,
                    show_thinking=runtime.config.tools.show_thinking,
                    show_full=runtime.config.tools.show_full,
                    reasoning_char_budget=_effective_reasoning_char_budget(
                        request,
                        runtime.config.tools.reasoning_char_budget,
                        runtime.config.tools.delegated_reasoning_char_budget,
                    ),
                    prompt_status=prompt_status,
                )
            finally:
                prompt_status.stop()
        else:
            with prompt_status:
                completion = await adapter.complete_turn(round_request)

        # ── Token limit check: abort tool loop if model was cut off ──
        if getattr(completion, "finished_reason", "") == "length":
            console.print(
                "[bold yellow]⚠ Token limit reached — response was cut off. "
                "Aborting tool loop to prevent retry loop. "
                "Increase `max_output_tokens` in your provider config.[/bold yellow]"
            )
            result.text = completion.text
            result.tool_calls = []
            result.finished_reason = "length"
            # Still save the partial assistant message so the model knows
            # what happened when it sees the CURRENT METADATA in the next turn.
            assistant_message = dict(completion.assistant_message)
            assistant_message.setdefault("role", "assistant")
            conversation_state.chat_history.append(assistant_message)
            if completion.usage:
                _merge_usage_totals(result.usage, completion.usage)
            return result

        # ── SoT Step 6: Clean — save assistant to permanent history (no SoT) ──
        assistant_message = dict(completion.assistant_message)
        assistant_message.setdefault("role", "assistant")
        conversation_state.chat_history.append(assistant_message)

        if completion.usage:
            _merge_usage_totals(result.usage, completion.usage)
            _store_latest_usage_snapshot(result.usage, completion.usage)

        # ── No tool calls: turn is done ──
        if not completion.tool_calls:
            result.text = completion.text
            result.tool_calls = []
            if executed_any_tool and not (completion.text or "").strip():
                console.print(
                    "[bold yellow]Warning:[/bold yellow] The model stopped on its own after running tools, "
                    "without writing any reply for you. Nothing was sent. "
                    "To continue, send another prompt manually (for example: ask it to answer based on what it just read, "
                    "or tell it to keep going)."
                )
            if completion.text and not round_request.stream:
                console.print(completion.text)
            return result

        # ── Execute each tool call, rebuild SoT after EACH one ──
        result.tool_calls = completion.tool_calls
        console.print(f"[cyan]Round {round_index + 1}: executing {len(completion.tool_calls)} tool call(s)[/cyan]")
        same_round_cache: dict[str, tuple[str, ToolExecutionResult, str]] = {}
        round_observations: list[RoundObservation] = []

        for tool_call in completion.tool_calls:
            function = tool_call.get("function") or {}
            tool_name = str(function.get("name", "unknown"))
            tool_args_raw = str(function.get("arguments", "{}"))
            console.print(f"[blue]assistant requested[/blue] {tool_name} {tool_args_raw}")

            tool_signature = _build_tool_call_signature(tool_call)
            cached_execution = same_round_cache.get(tool_signature)

            if cached_execution is None:
                if tool_name in {"run_command", "delegate_task"}:
                    with console.status(
                        "",
                        spinner="sot_robot",
                        spinner_style="bold bright_cyan",
                    ):
                        tool_call_id, tool_result = await registry.execute_tool_call(tool_call)
                else:
                    tool_call_id, tool_result = await registry.execute_tool_call(tool_call)
                executed_any_tool = True

                console.print(f"[dim]tool {tool_result.name} -> {'error' if tool_result.is_error else 'ok'}[/dim]")

                tool_summary = _build_tool_result_summary(tool_result)
                same_round_cache[tool_signature] = (
                    tool_call_id,
                    _clone_tool_execution_result(tool_result),
                    tool_summary,
                )
                round_observations.append(
                    RoundObservation(
                        signature=tool_signature,
                        summary=tool_summary,
                        is_error=tool_result.is_error,
                    )
                )
            else:
                original_tool_call_id, cached_tool_result, cached_summary = cached_execution
                tool_call_id = str(tool_call.get("id", ""))
                tool_result = _clone_tool_execution_result(cached_tool_result)
                tool_summary = f"duplicate of {original_tool_call_id} -> {cached_summary}"
                console.print(
                    f"[dim]tool {tool_result.name} -> reused duplicate result from {original_tool_call_id}[/dim]"
                )

            # Tool result to permanent history: metadata only (SoT Rule 2)
            conversation_state.chat_history.append({
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": tool_summary,
            })

            if cached_execution is None and tool_name == "delegate_task":
                delegated_usage = _extract_usage_from_tool_result(tool_result)
                if delegated_usage:
                    _merge_delegated_usage_totals(result.usage, delegated_usage)

            # ── Update SoT after THIS tool call ──
            if cached_execution is not None:
                continue

            # If session mutation, merge session SoT entries into tracked state
            if tool_name in SESSION_MUTATION_TOOLS and not tool_result.is_error:
                if not is_task:
                    merge_session_into_tracked(runtime, request, conversation_state.sot)
                    _refresh_request_from_session(runtime, request)


            # Update tracked files/media from tool effects
            update_tracked_from_tool_result(conversation_state.sot, tool_name, tool_result)

        round_fingerprint = tuple(round_observations)
        if round_fingerprint and round_fingerprint == previous_round_fingerprint:
            repeated_round_count += 1
        else:
            repeated_round_count = 0
        previous_round_fingerprint = round_fingerprint or None

        if repeat_round_limit > 0 and round_fingerprint and repeated_round_count >= repeat_round_limit:
            message = _build_repeated_rounds_message(
                round_observations,
                repeat_count=repeated_round_count + 1,
                delegated=request.disable_delegation,
            )
            if request.disable_delegation:
                result.is_error = True
                result.text = message
                result.tool_calls = []
                return result
            console.print(f"[bold yellow]Warning:[/bold yellow] {message}")
            result.text = message
            result.tool_calls = []
            return result

    message = _build_tool_loop_exhausted_message(effective_max_rounds, request.disable_delegation)
    if request.disable_delegation:
        result.is_error = True
        result.text = message
        result.tool_calls = []
        return result
    console.print(f"[bold yellow]Warning:[/bold yellow] {message}")
    result.text = message
    result.tool_calls = []
    return result


def _refresh_request_from_session(runtime: AppRuntime, request: ProviderRequest) -> None:
    """After session mutation tools, refresh ALL request metadata from session."""
    from sot_cli.message_builder import build_system_prompt, build_orchestration_rules
    session = runtime.sessions.load(request.session_id)
    request.provider_name = session.provider
    request.model = session.model
    if hasattr(session, "temperature") and session.temperature is not None:
        request.temperature = session.temperature
    if hasattr(session, "max_output_tokens") and session.max_output_tokens is not None:
        request.max_output_tokens = session.max_output_tokens
    # Rebuild system prompt and rules so user overrides stay current; respect sub-agent mode
    request.system_prompt = build_system_prompt()
    request.orchestration_rules = build_orchestration_rules(is_sub_agent=request.disable_delegation)


def _effective_tool_loop_max_rounds(
    request: ProviderRequest,
    default_max_rounds: int,
    delegated_max_rounds: int = FALLBACK_DELEGATED_MAX_ROUNDS,
) -> int:
    if request.disable_delegation:
        return min(default_max_rounds, delegated_max_rounds)
    return default_max_rounds


def _repeat_round_limit(
    request: ProviderRequest,
    main_limit: int = FALLBACK_REPEAT_LIMIT,
    delegated_limit: int = FALLBACK_DELEGATED_REPEAT_LIMIT,
) -> int:
    if request.disable_delegation:
        return delegated_limit
    return main_limit


def _effective_reasoning_char_budget(
    request: ProviderRequest,
    boss_budget: int = FALLBACK_REASONING_CHAR_BUDGET,
    delegated_budget: int = FALLBACK_DELEGATED_REASONING_CHAR_BUDGET,
) -> int:
    """Return the reasoning-char cap that applies to this request.

    Returns 0 when the cap is disabled (either side set to 0).
    Sub-agents get the (usually tighter) delegated budget.
    """
    if request.disable_delegation:
        return delegated_budget
    return boss_budget


def _build_tool_call_signature(tool_call: dict[str, Any]) -> str:
    function = tool_call.get("function") or {}
    name = str(function.get("name", "")).strip()
    arguments = _normalize_tool_arguments(function.get("arguments") or "{}")
    return f"{name}:{arguments}"


def _normalize_tool_arguments(raw_arguments: Any) -> str:
    if isinstance(raw_arguments, str):
        try:
            parsed = json.loads(raw_arguments)
        except json.JSONDecodeError:
            return raw_arguments.strip()
        return json.dumps(parsed, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return json.dumps(raw_arguments, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _clone_tool_execution_result(tool_result: ToolExecutionResult) -> ToolExecutionResult:
    return ToolExecutionResult(
        name=tool_result.name,
        content=tool_result.content,
        record_content=tool_result.record_content,
        supplemental_messages=list(tool_result.supplemental_messages),
        is_error=tool_result.is_error,
    )


def _build_repeated_rounds_message(
    round_observations: list[RoundObservation],
    repeat_count: int,
    delegated: bool,
) -> str:
    if round_observations:
        first = round_observations[0]
        preview = f"{first.signature} -> {first.summary}"
    else:
        preview = "no unique tool activity"
    if len(preview) > 320:
        preview = preview[:317] + "..."

    prefix = "Delegated sub-agent aborted" if delegated else "Tool loop stopped"
    return (
        f"{prefix} after {repeat_count} repeated rounds without progress. "
        f"Repeated pattern: {preview}. Try a different tool, narrower filters, or a different search strategy."
    )


def _build_tool_loop_exhausted_message(max_rounds: int, delegated: bool) -> str:
    if delegated:
        return (
            f"Delegated sub-agent exceeded its fail-fast budget of {max_rounds} rounds without reaching a final answer. "
            "Return the partial findings you have or try a narrower task."
        )
    return (
        f"Tool loop exceeded {max_rounds} rounds without reaching a final answer. "
        "Try a narrower request or a different tool strategy."
    )


def _merge_usage_totals(target: dict[str, Any], incoming: dict[str, Any]) -> None:
    for key, value in incoming.items():
        if isinstance(value, bool):
            current = target.get(key)
            target[key] = bool(current) or value
            continue

        if isinstance(value, (int, float)):
            current = target.get(key)
            if isinstance(current, (int, float)) and not isinstance(current, bool):
                target[key] = current + value
            else:
                target[key] = value
            continue

        if isinstance(value, dict):
            current = target.get(key)
            if not isinstance(current, dict):
                current = {}
                target[key] = current
            _merge_usage_totals(current, value)
            continue

        if key not in target:
            target[key] = value


def _replace_usage_snapshot(target: dict[str, Any], incoming: dict[str, Any]) -> None:
    preserved_latest = {k: v for k, v in target.items() if str(k).startswith("latest_")}
    target.clear()
    for key, value in incoming.items():
        if isinstance(value, dict):
            nested: dict[str, Any] = {}
            _replace_usage_snapshot(nested, value)
            target[key] = nested
        else:
            target[key] = value
    target.update(preserved_latest)


def _store_latest_usage_snapshot(target: dict[str, Any], incoming: dict[str, Any]) -> None:
    prompt_tokens = incoming.get("prompt_tokens")
    completion_tokens = incoming.get("completion_tokens")
    total_tokens = incoming.get("total_tokens")

    if isinstance(prompt_tokens, (int, float)) and not isinstance(prompt_tokens, bool):
        target["latest_prompt_tokens"] = prompt_tokens
    if isinstance(completion_tokens, (int, float)) and not isinstance(completion_tokens, bool):
        target["latest_completion_tokens"] = completion_tokens
    if isinstance(total_tokens, (int, float)) and not isinstance(total_tokens, bool):
        target["latest_total_tokens"] = total_tokens


def _merge_delegated_usage_totals(target: dict[str, Any], delegated_usage: dict[str, Any]) -> None:
    _merge_usage_totals(target, delegated_usage)

    delegated_numeric_keys = (
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "cost",
    )
    delegated_detail_keys = (
        "prompt_tokens_details",
        "completion_tokens_details",
        "cost_details",
    )

    target["delegated_task_count"] = int(target.get("delegated_task_count", 0) or 0) + 1

    for key in delegated_numeric_keys:
        value = delegated_usage.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            prefixed_key = f"delegated_{key}"
            current = target.get(prefixed_key)
            if isinstance(current, (int, float)) and not isinstance(current, bool):
                target[prefixed_key] = current + value
            else:
                target[prefixed_key] = value

    for key in delegated_detail_keys:
        value = delegated_usage.get(key)
        if not isinstance(value, dict):
            continue
        prefixed_key = f"delegated_{key}"
        current = target.get(prefixed_key)
        if not isinstance(current, dict):
            current = {}
            target[prefixed_key] = current
        _merge_usage_totals(current, value)


def _extract_usage_from_tool_result(tool_result: ToolExecutionResult) -> dict[str, Any]:
    try:
        payload = json.loads(tool_result.record_content)
    except (json.JSONDecodeError, TypeError):
        return {}

    usage = payload.get("usage")
    if isinstance(usage, dict):
        return usage
    return {}


# NOTE: session usage persistence has been removed. Usage will be recorded
# inside delegated agent reports (response.md) and provider response files.


def _build_payload_messages(conversation_state: ConversationState, request: ProviderRequest) -> list[dict[str, Any]]:
    """Assemble the ephemeral payload sent to the provider.

    Structure: [system] + [orchestration rules] + chat_history (permanent) + [SoT block (ephemeral)].

    The SoT block is rebuilt from disk every round and NEVER enters chat_history.
    This is the core of the SoT Method (Steps 2-4: State Registry + Assemble + Inject).
    """
    payload: list[dict[str, Any]] = [{"role": "system", "content": request.system_prompt}]
    
    if request.orchestration_rules:
        payload.append({"role": "user", "content": request.orchestration_rules})

    # Encontrar el índice del último prompt del usuario para inyectar el SoT justo antes
    last_user_idx = 0
    for i in range(len(conversation_state.chat_history) - 1, -1, -1):
        if conversation_state.chat_history[i].get("role") == "user":
            last_user_idx = i
            break

    # 1. Historial pasado (todo hasta el último user prompt, exclusivo)
    payload.extend(conversation_state.chat_history[:last_user_idx])

    # 2. El SoT (Estado actual del mundo)
    sot_message = build_sot_payload_message(conversation_state.sot)
    if sot_message is not None:
        payload.append(sot_message)

    # 2.5 CURRENT METADATA block — metadata del turno previo (vacía en el primer turno,
    #     en cuyo caso se omite). Efímero, no entra a chat_history.
    if conversation_state.last_turn_metadata:
        meta_message = build_previous_turn_metadata_message(conversation_state.last_turn_metadata)
        if meta_message is not None:
            payload.append(meta_message)

    # 3. El turno actual (último user prompt y lo que venga despues)
    payload.extend(conversation_state.chat_history[last_user_idx:])

    return payload


def _build_tool_result_summary(tool_result: Any) -> str:
    """Build a metadata-only summary. Never include file content."""
    try:
        payload = json.loads(tool_result.record_content)
    except (json.JSONDecodeError, TypeError):
        if tool_result.is_error:
            return f"error: {tool_result.content[:200]}"
        return "ok"

    if tool_result.is_error:
        return f"error: {payload.get('error', 'unknown')}"

    name = tool_result.name

    if name == "read_files":
        # Prepend context warnings from supplemental_messages
        warning_parts: list[str] = []
        for sm in tool_result.supplemental_messages:
            if isinstance(sm, dict) and isinstance(sm.get("content"), str):
                txt = sm["content"].strip()
                if txt.startswith("[CONTEXT WARNING]") and txt not in warning_parts:
                    warning_parts.append(txt)

        result_count = payload.get("result_count", "?")
        success_count = payload.get("success_count", "?")
        error_count = payload.get("error_count", "?")
        results = payload.get("results") or []

        if not isinstance(results, list) or not results:
            warning_text = warning_parts[0] if warning_parts else ""
            batch_line = f"batch read {success_count}/{result_count} ok ({error_count} errors)"
            if success_count != "?" and int(success_count) > 0:
                batch_line += " -> SoT updated"
            if warning_text:
                return warning_text + "\n" + batch_line
            return batch_line

        lines = []
        if warning_parts:
            lines.append(warning_parts[0])
        lines.append(f"batch read {success_count}/{result_count} ok ({error_count} errors):")
        for item in results:
            if not isinstance(item, dict):
                continue
            if not item.get("ok"):
                err_path = item.get("path", "?")
                err_msg = str(item.get("error", "unknown")).strip().splitlines()[0][:160]
                lines.append(f"- ERROR {err_path}: {err_msg}")
                continue
            fpath = item.get("path", "?")
            ftype = item.get("type", "text")
            if ftype == "text":
                lines.append(
                    f"- read {fpath} ({item.get('total_lines', '?')} lines, "
                    f"{item.get('size_bytes', '?')} bytes) -> SoT"
                )
            elif ftype == "image":
                lines.append(f"- read image {fpath} ({item.get('original_size_bytes', '?')} bytes) -> SoT")
            elif ftype == "pdf":
                lines.append(f"- read pdf {fpath} ({item.get('page_count', '?')} pages) -> SoT")
            elif ftype == "notebook":
                lines.append(f"- read notebook {fpath} ({item.get('cell_count', '?')} cells) -> SoT")
            elif ftype == "audio":
                lines.append(f"- read audio {fpath} ({item.get('size_bytes', '?')} bytes) -> SoT")
            elif ftype == "video":
                lines.append(f"- read video {fpath} ({item.get('size_bytes', '?')} bytes) -> SoT")
            elif ftype == "file_unchanged":
                lines.append(f"- unchanged {fpath}")
            elif ftype == "file_in_sot":
                lines.append(f"- {fpath} already in SoT — see '=== SOURCE OF TRUTH ===' block, do not need to re-read using a tool")
            else:
                lines.append(f"- read {fpath} type={ftype}")

        return "\n".join(lines)

    if name == "open_path":
        fpath = payload.get("path", "?")
        application = payload.get("application")
        resolved_application = payload.get("resolved_application")
        if isinstance(resolved_application, str) and resolved_application.strip():
            return f"opened {fpath} with {application} -> {resolved_application}"
        if isinstance(application, str) and application.strip():
            return f"opened {fpath} with {application}"
        return f"opened {fpath} with default application"

    if name == "edit_files":
        results = payload.get("results") or []
        summary = payload.get("summary") or {}
        total = summary.get("total", len(results) if isinstance(results, list) else 0)
        succeeded = summary.get("succeeded", 0)
        failed = summary.get("failed", 0)
        if not isinstance(results, list) or not results:
            return f"edit_files: {succeeded}/{total} ok, {failed} failed."

        # Per-file one-liners. Successful entries report op + edit_count and
        # an explicit SoT-status note so the model never has to guess whether
        # a re-read is needed:
        #   - "create" → always added to SoT.
        #   - "update" → refreshed only when the path was already in SoT
        #     (or under a session-attached source entry); silently NOT
        #     injected otherwise.
        lines: list[str] = []
        for entry in results:
            if not isinstance(entry, dict):
                continue
            fpath = entry.get("path") or "?"
            if entry.get("ok"):
                op = entry.get("operation", "update")
                edit_count = entry.get("edit_count", "?")
                size = entry.get("size_bytes", "?")
                if op == "create":
                    sot_note = "added to SoT"
                else:
                    sot_note = "SoT will be refreshed if file was already tracked"
                lines.append(f"  - {op} {fpath} ({edit_count} atomic edits, {size} bytes; {sot_note})")
            else:
                err = entry.get("error", "unknown error")
                lines.append(f"  - FAILED {fpath}: {err}")

        header = (
            f"edit_files: {succeeded}/{total} ok"
            + (f", {failed} failed" if failed else "")
            + ". Created files are now in the SoT (no need to read_files them); "
            + "updates only refresh paths that were already tracked — see per-file notes below."
        )
        return header + "\n" + "\n".join(lines)

    if name == "write_file":
        fpath = payload.get("path", "?")
        op = payload.get("operation", "write")
        lines = payload.get("line_count", "?")
        size = payload.get("size_bytes", "?")
        return f"{op} {fpath} ({lines} lines, {size} bytes). SoT already has the updated version — do not need re-read using a tool."

    if name == "list_dir":
        fpath = payload.get("path", "?")
        count = int(payload.get("entry_count", 0) or 0)
        entries = payload.get("entries") or []
        if not isinstance(entries, list) or not entries:
            return f"listed {fpath} (0 entries)"

        summary_lines = [f"listed {fpath} ({count} entries):"]
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            entry_path = str(entry.get("path") or entry.get("relative_path") or entry.get("name") or "?").strip()
            entry_kind = str(entry.get("kind") or "?").strip()
            entry_size = entry.get("size_bytes")
            # Surface the blocked_by_os flag inline so the model knows that
            # an empty/unreadable directory was a permissions problem and
            # not just genuinely empty — avoids wasted retries.
            blocked = entry.get("blocked_by_os")
            status_text = " [Blocked by OS]" if blocked else ""
            size_text = f"{entry_size} bytes" if isinstance(entry_size, int) else "size unknown"
            summary_lines.append(f"- {entry_path} ({entry_kind}, {size_text}){status_text}")

        return "\n".join(summary_lines)

    if name == "search_code":
        mode = payload.get("mode", "files_with_matches")
        if mode == "content":
            content = payload.get("content", "")
            line_count = payload.get("line_count", 0)
            total = payload.get("total_result_lines", line_count)
            truncated = payload.get("truncated", False)
            if not content:
                return "search: no matches found"
            result = content
            if truncated:
                result += f"\n\n[showing {line_count} of {total} result lines — use offset to paginate]"
            return result
        elif mode == "count":
            match_count = payload.get("match_count", 0)
            file_count = payload.get("file_count", 0)
            content = payload.get("content", "")
            if not content:
                return "search: no matches found"
            return f"{content}\n\n{match_count} matches across {file_count} files"
        else:
            files = payload.get("files", [])
            if not files:
                return "search: no files matched"
            file_count = payload.get("file_count", len(files))
            total = payload.get("total_matches", file_count)
            truncated = payload.get("truncated", False)
            result = f"found {file_count} files:\n" + "\n".join(files)
            if truncated:
                result += f"\n\n[showing {file_count} of {total} — use offset to paginate]"
            return result

    if name == "run_command":
        cmd = payload.get("command", "?")
        if len(cmd) > 80:
            cmd = cmd[:77] + "..."
        exit_code = payload.get("exit_code", "?")
        timed_out = payload.get("timed_out", False)
        stdout = str(payload.get("stdout", "")).strip()
        stderr = str(payload.get("stderr", "")).strip()

        if timed_out:
            timeout_limit = payload.get("timeout_seconds", "?")
            header = f"'{cmd}' killed after timeout (limit={timeout_limit}s)"
            stdout_label = "[stdout captured before kill]"
            stderr_label = "[stderr captured before kill]"
        else:
            header = f"'{cmd}' exit={exit_code}"
            stdout_label = "[stdout]"
            stderr_label = "[stderr]"

        result_parts = [header]

        if stdout:
            result_parts.append(f"{stdout_label}\n{stdout}")

        if stderr:
            result_parts.append(f"{stderr_label}\n{stderr}")

        if len(result_parts) == 1:
            stdout_bytes = payload.get("stdout_bytes", 0)
            stderr_bytes = payload.get("stderr_bytes", 0)
            result_parts.append(f"stdout={stdout_bytes}b stderr={stderr_bytes}b")

        return "\n".join(result_parts)

    if name == "list_tasks":
        tasks = payload.get("tasks", [])
        if not tasks:
            return "no delegated tasks found"
        summary = []
        for t in tasks:
            agent_id = t.get("agent_id", "?")
            status = t.get("status", "?")
            summary.append(f"{agent_id}:{status}")
        return f"tasks: {', '.join(summary)}"

    if name == "wait_task":
        agent_id = payload.get("agent_id", "?")
        status = payload.get("status", "?")
        if payload.get("timed_out"):
            return f"waited for {agent_id} -> still {status} (timed out)"
        report = payload.get("report", "")
        return f"waited for {agent_id} -> {status}\n\n{report}"

    if name == "delegate_task":
        agent_id = payload.get("agent_id", "?")
        status = payload.get("status", "?")
        return f"delegated task {agent_id} {status}. Use wait_task to get the result."

    if name == "attach_path_to_source":
        attached_paths = payload.get("attached_paths")
        entries = payload.get("source_entries", "?")
        if isinstance(attached_paths, list) and len(attached_paths) > 1:
            return f"attached {len(attached_paths)} paths (entries={entries})"
        attached = payload.get("attached_path", "?")
        return f"attached {attached} (entries={entries})"

    if name == "detach_path_from_source":
        detached_paths = payload.get("detached_paths")
        entries = payload.get("source_entries", "?")
        if isinstance(detached_paths, list) and len(detached_paths) > 1:
            return f"detached {len(detached_paths)} paths (entries={entries})"
        detached = payload.get("detached_path", "?")
        return f"detached {detached} (entries={entries})"

    if name == "delete_file":
        fpath = payload.get("path", "?")
        return f"deleted {fpath}"

    if name == "get_session_state":
        # Filtramos 'providers' porque ocupa muchos tokens y rara vez se necesita
        state_info = {k: v for k, v in payload.items() if k != "providers"}
        return f"Session state: {json.dumps(state_info, ensure_ascii=False)}"

    # Fallback for tools without an explicit summary branch (typically MCP tools).
    # Drop only the heaviest binary-ish keys; let the rest of the payload reach
    # the model verbatim — partial context is worse than full context.
    return f"ok {json.dumps({k: v for k, v in payload.items() if k not in ('content', 'ok', 'base64')})}"




# ── Streaming ─────────────────────────────────────────────────────────────

async def _run_streaming_round(
    adapter: ProviderAdapter,
    request: ProviderRequest,
    console: Console,
    show_thinking: bool = True,
    show_full: bool = True,
    reasoning_char_budget: int = 0,
    prompt_status: Any = None,
):
    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    reasoning_details: list[dict[str, Any]] = []
    usage: dict[str, Any] = {}
    tool_state: dict[int, dict[str, Any]] = {}
    render_state = StreamRenderState()
    _tool_call_header_shown: set[int] = set()
    reasoning_chars = 0
    reasoning_budget_tripped = False
    _stream_finished_reason = ""

    stream = adapter.stream_turn(request)
    try:
        async for event in stream:
            # Tear down the loading spinner the instant any real content
            # starts arriving — reasoning, text, or a tool call. Doing
            # this BEFORE the per-event render path is critical: the
            # render path writes raw bytes to stdout and would collide
            # with Rich's Live cursor management if the spinner were
            # still drawing.
            if prompt_status is not None and event.type in {
                "reasoning_delta",
                "text_delta",
                "tool_call",
            }:
                prompt_status.stop()
                prompt_status = None

            if event.type == "reasoning_delta":
                text = str(event.payload.get("text", ""))
                details = event.payload.get("details") or []
                if text:
                    reasoning_parts.append(text)
                    reasoning_chars += len(text)
                    if show_thinking:
                        if not render_state.reasoning_started:
                            _write_meta(render_state, "\x1b[2mthinking:\x1b[0m ", ends_on_newline=False)
                            render_state.reasoning_started = True
                        # Verbatim: whatever the provider sent, print it as-is
                        # inside the dim-style envelope. No regex, no dedup.
                        _stream_chunk(render_state, text, ansi_prefix="\x1b[2m", ansi_suffix="\x1b[0m")
                if isinstance(details, list):
                    for detail in details:
                        if isinstance(detail, dict):
                            reasoning_details.append(detail)
                if reasoning_char_budget and reasoning_chars >= reasoning_char_budget:
                    reasoning_budget_tripped = True
                    break
            elif event.type == "text_delta":
                text = str(event.payload.get("text", ""))
                if text:
                    if show_thinking and render_state.reasoning_started and not render_state.text_started:
                        # At most one blank line between reasoning and final text.
                        _ensure_fresh_line(render_state)
                        _write_meta(render_state, "\n", ends_on_newline=True)
                    render_state.text_started = True
                    text_parts.append(text)
                    _stream_chunk(render_state, text)
            elif event.type == "tool_call":
                for tool_delta in event.payload.get("tool_calls") or []:
                    _merge_tool_call_delta(tool_state, tool_delta)
                    if show_full:
                        index = int(tool_delta.get("index", 0))
                        func = tool_delta.get("function") or {}
                        name = func.get("name", "")
                        args_chunk = func.get("arguments", "")
                        if name and index not in _tool_call_header_shown:
                            _tool_call_header_shown.add(index)
                            _ensure_fresh_line(render_state)
                            _write_meta(render_state, f"\x1b[2mtool_call: {name}(\x1b[0m", ends_on_newline=False)
                        if args_chunk:
                            _stream_chunk(render_state, args_chunk)
            elif event.type == "usage":
                event_usage = event.payload.get("usage") or {}
                if isinstance(event_usage, dict):
                    _replace_usage_snapshot(usage, event_usage)
            elif event.type == "error":
                raise RuntimeError(str(event.payload.get("message", "Unknown provider error")))
            elif event.type == "finished":
                finish_reason = event.payload.get("finish_reason", "")
                if finish_reason:
                    _stream_finished_reason = finish_reason
    finally:
        await stream.aclose()

    text = "".join(text_parts)
    tool_calls = [_finalize_tool_call(tool_state[index]) for index in sorted(tool_state)]
    if reasoning_budget_tripped:
        _ensure_fresh_line(render_state)
        _write_meta(
            render_state,
            f"\x1b[33m⚠  reasoning budget exceeded ({reasoning_chars} chars ≥ {reasoning_char_budget}); stream cut, continuing.\x1b[0m\n",
            ends_on_newline=True,
        )
    if show_full and _tool_call_header_shown:
        _write_meta(render_state, "\x1b[2m)\x1b[0m\n", ends_on_newline=True)
    if text or (show_thinking and render_state.reasoning_started):
        _ensure_fresh_line(render_state)

    assistant_message: dict[str, Any] = {"role": "assistant"}
    assistant_message["content"] = text if text else None
    if reasoning_details:
        # Per-token streaming deltas get collapsed into the minimum
        # number of entries before persistence; see
        # _consolidate_reasoning_details for the exact rules.
        assistant_message["reasoning_details"] = _consolidate_reasoning_details(reasoning_details)
    elif reasoning_parts:
        assistant_message["reasoning"] = "".join(reasoning_parts)
    if tool_calls:
        assistant_message["tool_calls"] = tool_calls

    return type("StreamingCompletion", (), {
        "assistant_message": assistant_message,
        "text": text,
        "tool_calls": tool_calls,
        "usage": usage,
        "finished_reason": _stream_finished_reason,
    })()


def _merge_tool_call_delta(tool_state: dict[int, dict[str, Any]], delta: dict[str, Any]) -> None:
    index = int(delta.get("index", 0))
    entry = tool_state.setdefault(index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
    tool_id = delta.get("id")
    if isinstance(tool_id, str) and tool_id:
        entry["id"] = tool_id
    tool_type = delta.get("type")
    if isinstance(tool_type, str) and tool_type:
        entry["type"] = tool_type
    function_delta = delta.get("function") or {}
    function_name = function_delta.get("name")
    if isinstance(function_name, str) and function_name:
        entry["function"]["name"] += function_name
    function_arguments = function_delta.get("arguments")
    if isinstance(function_arguments, str) and function_arguments:
        entry["function"]["arguments"] += function_arguments


def _finalize_tool_call(tool_call: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": tool_call.get("id", ""),
        "type": tool_call.get("type", "function"),
        "function": {
            "name": tool_call.get("function", {}).get("name", ""),
            "arguments": tool_call.get("function", {}).get("arguments", "{}"),
        },
    }


# ── Display helpers ──────────────────────────────────────────────────────
