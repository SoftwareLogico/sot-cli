from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Literal, Protocol


ProviderEventType = Literal["text_delta", "reasoning_delta", "tool_call", "usage", "finished", "done", "error"]


def _is_retryable_status(status_code: int | None) -> bool:
    """Classify an HTTP/SSE status code as transient (retryable) or permanent.

    Retryable: unknown/absent codes (transport-level drops where no HTTP
    status exists), 408 Request Timeout, 429 Too Many Requests, and every
    5xx (gateway/upstream failures like OpenRouter's injected
    ``[502] JSON error injected into SSE stream``).
    Permanent: any other 4xx (400 schema, 401 auth, 402 payment, 403, 404)
    where replaying the identical request is pointless.
    """
    if status_code is None:
        return True
    if status_code in (408, 429):
        return True
    return status_code >= 500


@dataclass
class PartialRoundState:
    """Everything a killed streaming attempt had already produced.

    Captured by the runtime when an upstream stream error kills a round
    mid-generation, and attached to :class:`UpstreamStreamError` so the
    turn-level retry can inject a continuation (partial assistant message
    + synthetic "continue" user message) instead of restarting the whole
    generation from scratch.
    """

    reasoning: str = ""
    reasoning_details: list[dict[str, Any]] = field(default_factory=list)
    text: str = ""
    # Merged in-flight tool calls (id/type/function.name/function.arguments
    # accumulated from deltas). Arguments may be INCOMPLETE JSON — the
    # continuation builder folds them into visible text instead of emitting
    # them as real tool_calls (strict pairing would reject an assistant
    # tool_call followed by a user message instead of a tool response).
    tool_state: dict[int, dict[str, Any]] = field(default_factory=dict)
    usage: dict[str, Any] = field(default_factory=dict)
    finished_reason: str = ""


class UpstreamStreamError(RuntimeError):
    """Raised when the provider's stream fails at open time or mid-flight.

    Covers the whole family of transient upstream/gateway stream failures:
    SSE error chunks injected mid-stream (OpenRouter's classic
    ``[502] JSON error injected into SSE stream``), dropped SSE
    connections, and 5xx/429/408 HTTP responses. Subclasses
    :class:`RuntimeError` so every existing handler that catches
    RuntimeError keeps working unchanged.

    Attributes:
        status_code: HTTP/SSE status code when one is known (``None`` for
            transport-level drops and non-numeric SSE error codes).
        retryable: whether replaying the identical request makes sense.
            Auto-classified from ``status_code`` via
            :func:`_is_retryable_status` unless explicitly overridden.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool | None = None,
        partial: PartialRoundState | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = _is_retryable_status(status_code) if retryable is None else retryable
        # Partial generation captured by the runtime before the stream died
        # (None when the failure happened before any token was produced).
        self.partial = partial


@dataclass
class ProviderCapability:
    supports_tools: bool = False
    supports_images: bool = True
    supports_pdfs: bool = False
    supports_audio: bool = False
    supports_video: bool = False
    # Model metadata populated by provider API detection
    supports_reasoning: bool = False
    # Levels the model actually accepts, as advertised by the provider API
    # (LM Studio publishes them in capabilities.reasoning.allowed_options).
    # Empty = provider does not advertise a scale.
    reasoning_options: tuple[str, ...] = ()
    context_length: int | None = None
    allocated_context_length: int | None = None
    max_completion_tokens: int | None = None
    modality: str = ""  # e.g. "text+image->text"
    quantization: str = ""  # e.g. "Q8_0" (lmstudio)
    parameter_count: str = ""  # e.g. "27B" (lmstudio)


@dataclass
class ProviderRequest:
    provider_name: str
    model: str
    session_id: str
    system_prompt: str
    orchestration_rules: str
    user_prompt: str
    source_index: str
    source_contents: str = ""
    temperature: float = 0.2
    max_output_tokens: int = 4096
    stream: bool = True
    enable_tools: bool = True
    disable_delegation: bool = False
    tools: list[dict[str, Any]] = field(default_factory=list)
    conversation_messages: list[dict[str, Any]] = field(default_factory=list)

    # Hard cap on characters kept from `reasoning` of tool-bearing assistant
    # messages in OLD turns when the outbound payload is built. Applied by
    # the sanitizer in `openai_compat._sanitize_messages_for_provider`. 0
    # disables the cap (full reasoning round-trips for every turn). Plumbed
    # from `[tools].compression_reasoning_trunc_chars` in sot.toml.
    compression_reasoning_trunc_chars: int = 0

    # Reasoning effort. Wire format diverges per provider — see the
    # `_REASONING_LADDER` table in openai_compat.py, which is the authority on
    # what each fixed provider accepts.
    reasoning_effort: str | None = None

    # Filled by the adapter at payload-build time. `upstream_dialect` is the
    # real backend behind the open `openai` alias (lyber/nanogpt/strata/...,
    # resolved by `providers/upstream.py`); it decides which reasoning shape
    # and level set the request gets. Empty means "provider is not the alias".
    upstream_dialect: str = ""
    upstream_ignored: bool = False

    # Levels the endpoint advertised as accepted (LM Studio
    # `capabilities.reasoning.allowed_options`, copied from
    # ProviderCapability). Empty = no restriction known; a model that only
    # advertises off/on is collapsed to the native boolean toggle.
    reasoning_options: tuple[str, ...] = ()


@dataclass
class ProviderEvent:
    type: ProviderEventType
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProviderCompletion:
    assistant_message: dict[str, Any]
    text: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)


class ProviderAdapter(Protocol):
    name: str
    capability: ProviderCapability

    async def stream_turn(self, request: ProviderRequest) -> AsyncIterator[ProviderEvent]:
        ...

    async def complete_turn(self, request: ProviderRequest) -> ProviderCompletion:
        ...
