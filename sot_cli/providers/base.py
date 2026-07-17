from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Literal, Protocol


ProviderEventType = Literal["text_delta", "reasoning_delta", "tool_call", "usage", "finished", "done", "error"]


@dataclass
class ProviderCapability:
    supports_tools: bool = False
    supports_images: bool = True
    supports_pdfs: bool = False
    supports_audio: bool = False
    supports_video: bool = False
    # Model metadata populated by provider API detection
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

    # OpenRouter reasoning effort — uses nested "reasoning": {"effort": "..."}
    # Only OpenRouter supports this; OpenAI rejects it with tools.
    reasoning_effort: str | None = None


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
