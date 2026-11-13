from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path

from sot_cli.config import AppConfig, load_config
from sot_cli.paths import AppPaths, build_paths, ensure_runtime_directories
from sot_cli.providers.factory import create_provider_adapter
from sot_cli.constants import DEFAULT_CONTEXT_LIMIT
from sot_cli.providers.base import ProviderAdapter, ProviderCapability
from sot_cli.providers.openai_compat import OpenAICompatibleAdapter
from sot_cli.session_store import SessionStore
from sot_cli.mcp_client import MCPManager


@dataclass
class AppRuntime:
    config: AppConfig
    paths: AppPaths
    sessions: SessionStore
    # CLI overrides, per invocation. Never persisted: they only change how
    # THIS process interprets the provider's numbers and gates media.
    context_limit_override: int | None = None
    media_disabled: bool = False
    _adapter_cache: dict[tuple[str, str], ProviderAdapter] = field(default_factory=dict, repr=False)
    mcp: MCPManager = field(init=False, repr=False)

    def context_limit_for(self, capability: ProviderCapability) -> tuple[int, str]:
        """Resolve the context-limit denominator used by the UI and the tools.

        Precedence: --context_limit override > detected allocated_context_length
        > detected context_length > 1M. This is a display/awareness value only:
        the numerator is always the provider's reported prompt tokens, so a
        wrong denominator can mislead the bar but never the token accounting.
        """
        if self.context_limit_override:
            return self.context_limit_override, "Context Limit (User)"
        if capability.allocated_context_length:
            return capability.allocated_context_length, "Context Limit (Allocated)"
        if capability.context_length:
            return capability.context_length, "Context Limit (Max)"
        return DEFAULT_CONTEXT_LIMIT, "Context Limit (Max)"

    def _apply_capability_overrides(self, adapter: ProviderAdapter) -> ProviderAdapter:
        """Force capability flags off when the user asked for a text-only run.

        Applied to the cached adapter, so every consumer (read_files gate, SoT
        media materialization, tool schemas) sees the same values.
        """
        if self.media_disabled:
            capability = adapter.capability
            capability.supports_images = False
            capability.supports_pdfs = False
            capability.supports_audio = False
            capability.supports_video = False
        return adapter

    def provider_adapter(self, provider_name: str, model: str | None = None) -> ProviderAdapter:
        """Get adapter (sync) for a specific provider/model pair."""
        effective_model = model.strip() if isinstance(model, str) and model.strip() else self.config.provider(provider_name).model
        cache_key = (provider_name, effective_model)
        if cache_key not in self._adapter_cache:
            self._adapter_cache[cache_key] = create_provider_adapter(self.config, provider_name, model=effective_model)
        return self._apply_capability_overrides(self._adapter_cache[cache_key])

    async def provider_adapter_async(self, provider_name: str, model: str | None = None) -> ProviderAdapter:
        """Get adapter with capabilities detected from the provider API."""
        adapter = self.provider_adapter(provider_name, model=model)
        if hasattr(adapter, "detect_capabilities") and not getattr(adapter, "_capabilities_detected", False):
            await adapter.detect_capabilities()
        return self._apply_capability_overrides(adapter)

    def __post_init__(self):
        self.mcp = MCPManager(self.config.mcp_servers)


def bootstrap_runtime(config_path: str | None = None) -> AppRuntime:
    config = load_config(config_path)

    # ── Auto-update sot.toml structure from example ──
    _auto_update_config(config_path)

    paths = build_paths(config, config_path)
    ensure_runtime_directories(paths)

    # Allow overriding the sessions directory via environment variable.
    # This is important for delegated sub-agents that run inside a parent's
    # agents/ folder — the parent will set SOT_SESSIONS_DIR so the child
    # runtime points its SessionStore at that directory.
    sessions_env = os.environ.get("SOT_SESSIONS_DIR")
    sessions_dir = Path(sessions_env) if sessions_env else paths.sessions_dir

    return AppRuntime(
        config=config,
        paths=paths,
        sessions=SessionStore(sessions_dir),
    )


def _auto_update_config(config_path: str | None = None) -> None:
    """Silently update sot.toml structure from sot.example.toml.

    Adds new keys, but never overwrites user values. Runs on every
    bootstrap — fast no-op when already up to date.
    """
    try:
        from sot_cli.config.app import resolve_config_path
        from sot_cli.sot_updater import update_sot_structure
        from pathlib import Path

        resolved = resolve_config_path(config_path)
        example = resolved.parent / "sot.example.toml"
        if example.is_file():
            update_sot_structure(resolved, example, dry_run=False, quiet=True)
    except Exception:
        pass  # Never crash on config update failures
