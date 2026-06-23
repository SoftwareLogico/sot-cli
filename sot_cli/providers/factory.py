from __future__ import annotations
import os
from sot_cli.config import AppConfig, ProviderName
from sot_cli.providers.base import ProviderAdapter
from sot_cli.providers.openai_compat import OpenAICompatibleAdapter
from sot_cli.providers.bedrock_converse import BedrockConverseAdapter

def create_provider_adapter(config: AppConfig, provider_name: ProviderName, model: str | None = None) -> ProviderAdapter:
    provider = config.provider(provider_name)
    effective_model = model.strip() if isinstance(model, str) and model.strip() else provider.model
    extra_headers: dict[str, str] = {}
    if provider_name == "openrouter":
        http_referer = str(provider.extra.get("http_referer", "")).strip()
        app_title = str(provider.extra.get("app_title", "")).strip()
        categories = str(provider.extra.get("categories", "")).strip()
        if http_referer:
            extra_headers["HTTP-Referer"] = http_referer
        if app_title:
            extra_headers["X-OpenRouter-Title"] = app_title
        if categories:
            extra_headers["X-OpenRouter-Categories"] = categories
        provider_selection = str(provider.extra.get("provider_selection", "")).strip() or None
    else:
        provider_selection = None

    if provider_name == "bedrock":
        base_url = getattr(provider, "base_url", None)
        if base_url:
            
            project_id = getattr(provider, "project_id", None)
            if not project_id and hasattr(provider, "extra") and isinstance(provider.extra, dict):
                project_id = provider.extra.get("project_id")
            
            project_id = str(project_id or "default").strip()
            
            extra_headers["OpenAI-Project"] = project_id
            
            return OpenAICompatibleAdapter(
                name=provider_name,
                base_url=base_url,
                api_key=provider.api_key,
                model=effective_model,
                extra_headers=extra_headers,
                provider_selection=provider_selection,
            )

        # De lo contrario, cae en el comportamiento por defecto de Boto3 / Converse API
        region = str(provider.extra.get("region", "us-east-1")).strip()
        thinking_type = str(provider.extra.get("thinking_type", "")).strip() or None
        return BedrockConverseAdapter(
            name=provider_name,
            model=effective_model,
            region=region,
            api_key=provider.api_key,
            extra_headers=extra_headers,
            thinking_type=thinking_type,
        )


async def create_provider_adapter_with_detection(config: AppConfig, provider_name: ProviderName, model: str | None = None) -> OpenAICompatibleAdapter:
    """Create adapter and detect model capabilities from the provider API."""
    adapter = create_provider_adapter(config, provider_name, model=model)
    await adapter.detect_capabilities()
    return adapter
