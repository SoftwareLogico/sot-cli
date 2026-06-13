from __future__ import annotations

from sot_cli.config import AppConfig, ProviderName
from sot_cli.message_builder import build_system_prompt, build_orchestration_rules
from sot_cli.providers.base import ProviderRequest
from sot_cli.session_store import SessionRecord
from sot_cli.source_of_truth import SourceBundle


def prepare_turn_request(
    config: AppConfig,
    session: SessionRecord,
    user_prompt: str,
    bundle: SourceBundle,
    provider_name: ProviderName | None = None,
    model_override: str | None = None,
    enable_tools: bool = True,
    disable_delegation: bool = False,
) -> ProviderRequest:
    selected_provider = provider_name or session.provider or config.runtime.primary_provider
    provider = config.provider(selected_provider)
    if not provider.enabled:
        raise ValueError(f"Provider is not configured: {selected_provider}")

    if model_override and model_override.strip():
        model = model_override.strip()
    elif session.model:
        model = session.model
    else:
        model = provider.model

    if not model and selected_provider not in {"lmstudio", "ollama"}:
        raise ValueError(f"Provider has no model configured: {selected_provider}")

    temperature = session.temperature if session.temperature is not None else provider.temperature
    max_output_tokens = (
        session.max_output_tokens if session.max_output_tokens is not None else provider.max_output_tokens
    )


    # Reasoning effort from sot.toml (OpenRouter, Bedrock, and NVIDIA use this)
    raw_effort = provider.extra.get("reasoning_effort")
    reasoning_effort: str | None = (
        str(raw_effort).strip() or None if raw_effort is not None else None
    )

    system_prompt = build_system_prompt()
    orchestration_rules = build_orchestration_rules(is_sub_agent=disable_delegation)

    return ProviderRequest(
        provider_name=selected_provider,
        model=model,
        session_id=session.id,
        system_prompt=system_prompt,
        orchestration_rules=orchestration_rules,
        user_prompt=user_prompt.rstrip(),
        source_index=bundle.build_index(),
        source_contents=bundle.build_contents_payload(),
        temperature=temperature,
        max_output_tokens=max_output_tokens,
        enable_tools=enable_tools,
        disable_delegation=disable_delegation,
        reasoning_effort=reasoning_effort,
        compression_reasoning_trunc_chars=config.tools.compression_reasoning_trunc_chars,
    )
