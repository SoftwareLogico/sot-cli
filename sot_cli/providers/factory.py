from __future__ import annotations

from sot_cli.config import AppConfig, ProviderName
from sot_cli.providers.base import ProviderAdapter
from sot_cli.providers.openai_compat import OpenAICompatibleAdapter

def create_provider_adapter(config: AppConfig, provider_name: ProviderName, model: str | None = None) -> ProviderAdapter:
    provider = config.provider(provider_name)
    effective_model = model.strip() if isinstance(model, str) and model.strip() else provider.model
    extra_headers: dict[str, str] = {}
    if provider_name == "openrouter":
        http_referer = str(provider.extra.get("http_referer", "")).strip()
        app_title = str(provider.extra.get("app_title", "")).strip()
        if http_referer:
            extra_headers["HTTP-Referer"] = http_referer
        if app_title:
            extra_headers["X-OpenRouter-Title"] = app_title

    if provider_name == "bedrock":
        region = str(provider.extra.get("region", "us-east-1")).strip()
        effective_base_url = provider.base_url or f"https://bedrock-mantle.{region}.api.aws/v1"
        return OpenAICompatibleAdapter(
            name=provider_name,
            base_url=effective_base_url,
            api_key=provider.api_key,
            model=effective_model,
            extra_headers=extra_headers,
        )

    return OpenAICompatibleAdapter(
        name=provider_name,
        base_url=provider.base_url,
        api_key=provider.api_key,
        model=effective_model,
        extra_headers=extra_headers,
    )


async def create_provider_adapter_with_detection(config: AppConfig, provider_name: ProviderName, model: str | None = None) -> OpenAICompatibleAdapter:
    """Create adapter and detect model capabilities from the provider API."""
    adapter = create_provider_adapter(config, provider_name, model=model)
    await adapter.detect_capabilities()
    return adapter
