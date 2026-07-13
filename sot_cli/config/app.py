from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

if sys.version_info >= (3, 11):
    import tomllib  # pyright: ignore[reportMissingImports]
else:
    import tomli as tomllib  # pyright: ignore[reportMissingImports]


ProviderName = Literal["lmstudio", "openrouter", "openai", "xai", "ollama", "nvidia", "bedrock"]


def _read_default_config_template() -> str:
    """Return a template string for write_default_config().

    Prefers the real sot.toml when present (so users get their current settings
    as a starting point). Falls back to sot.example.toml when only the example
    exists — this is the common case on a fresh clone before first-run setup,
    and it avoids crashing the import chain at module load time.
    """
    root = Path(__file__).resolve().parents[2]
    for candidate in (root / "sot.toml", root / "sot.example.toml"):
        try:
            return candidate.read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
    return ""


DEFAULT_CONFIG_TEMPLATE = _read_default_config_template()
KNOWN_PROVIDERS: tuple[ProviderName, ...] = ("lmstudio", "openrouter", "openai", "ollama", "nvidia", "bedrock")


class ConfigError(ValueError):
    pass


@dataclass
class PromptConfig:
    system: str


@dataclass
class RuntimeConfig:
    primary_provider: ProviderName


@dataclass
class MCPServerConfig:
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class ToolConfig:
    play_finished_notification: bool = True
    default_command_timeout_seconds: int = 180
    binary_check_size: int = 0
    show_thinking: bool = True
    show_full: bool = True
    max_rounds: int = 25
    delegated_max_rounds: int = 8
    repeat_limit: int = 3
    delegated_repeat_limit: int = 2
    search_default_head_limit: int = 200
    search_max_line_length: int = 500
    search_timeout_seconds: int = 30
    reasoning_char_budget: int = 0
    delegated_reasoning_char_budget: int = 4000
    compression_reasoning_trunc_chars: int = 240
    max_readable_file_tokens: int = 64000


@dataclass
class ProviderConfig:
    name: ProviderName
    enabled: bool = False
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    subagent_model: str = ""
    temperature: float = 0.2
    max_output_tokens: int = 4096
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class AppConfig:
    name: str
    data_dir: str
    prompt: PromptConfig
    runtime: RuntimeConfig
    tools: ToolConfig
    providers: dict[ProviderName, ProviderConfig]
    mcp_servers: dict[str, MCPServerConfig] = field(default_factory=dict)

    def provider(self, name: ProviderName) -> ProviderConfig:
        return self.providers[name]

    def primary_provider_config(self) -> ProviderConfig:
        return self.provider(self.runtime.primary_provider)


def resolve_config_path(config_path: str | Path | None = None, start_dir: str | Path | None = None) -> Path:
    if config_path is not None:
        return Path(config_path).expanduser().resolve()

    start = Path(start_dir or Path.cwd()).expanduser().resolve()
    for directory in (start, *start.parents):
        candidate = directory / "sot.toml"
        if candidate.exists():
            return candidate

    return start / "sot.toml"


def write_default_config(destination: str | Path, force: bool = False) -> Path:
    path = Path(destination).expanduser().resolve()
    if path.exists() and not force:
        raise FileExistsError(f"Config already exists: {path}")
    path.write_text(DEFAULT_CONFIG_TEMPLATE, encoding="utf-8")
    return path


def load_config(config_path: str | Path | None = None) -> AppConfig:
    path = resolve_config_path(config_path)
    if not path.exists():
        raise FileNotFoundError(
            f"Config file not found: {path}. Run 'sot-cli init-config' or copy sot.toml to sot.toml."
        )

    with path.open("rb") as handle:
        raw = tomllib.load(handle)

    keys_path = path.with_name("sot.keys.toml")
    keys_raw = {}
    if keys_path.exists():
        with keys_path.open("rb") as handle:
            keys_raw = tomllib.load(handle)

    return _parse_app_config(raw, keys_raw)


def _parse_app_config(raw: dict[str, Any], keys_raw: dict[str, Any] | None = None) -> AppConfig:
    if keys_raw is None:
        keys_raw = {}

    app_raw = _require_mapping(raw, "app")
    prompt_raw = _optional_mapping(raw, "prompt")
    runtime_raw = _require_mapping(raw, "runtime")
    tools_raw = _optional_mapping(raw, "tools")
    providers_raw = _require_mapping(raw, "providers")
    keys_providers = _optional_mapping(keys_raw, "providers")

    providers: dict[ProviderName, ProviderConfig] = {}
    for provider_name in KNOWN_PROVIDERS:
        section_exists = provider_name in providers_raw
        section = providers_raw.get(provider_name, {})
        key_section = keys_providers.get(provider_name, {})

        if not isinstance(section, dict):
            raise ConfigError(f"providers.{provider_name} must be a table")
        if not isinstance(key_section, dict):
            raise ConfigError(f"keys providers.{provider_name} must be a table")

        api_key = str(key_section.get("api_key", section.get("api_key", "")))

        providers[provider_name] = ProviderConfig(
            name=provider_name,
            enabled=section_exists,
            base_url=str(section.get("base_url", "")),
            api_key=api_key,
            model=str(section.get("model", "")),
            subagent_model=str(section.get("subagent_model", "")),
            temperature=float(section.get("temperature", 0.2)),
            max_output_tokens=int(section.get("max_output_tokens", 4096)),
            extra={
                key: value
                for key, value in section.items()
                if key not in {
                    "base_url",
                    "api_key",
                    "model",
                    "subagent_model",
                    "temperature",
                    "max_output_tokens",
                }
            },
        )

    primary_provider = _parse_provider_name(runtime_raw.get("primary_provider"), "runtime.primary_provider")

    mcp_raw = _optional_mapping(raw, "mcp")
    mcp_servers_raw = _optional_mapping(mcp_raw, "servers")
    
    mcp_servers: dict[str, MCPServerConfig] = {}
    for srv_name, srv_config in mcp_servers_raw.items():
        if not isinstance(srv_config, dict):
            raise ConfigError(f"mcp.servers.{srv_name} must be a table")

        command = str(srv_config.get("command", "")).strip()
        if not command:
            raise ConfigError(f"mcp.servers.{srv_name}.command must be a non-empty string")

        raw_args = srv_config.get("args", [])
        if not isinstance(raw_args, list):
            raise ConfigError(f"mcp.servers.{srv_name}.args must be an array of strings")

        raw_env = srv_config.get("env", {})
        if not isinstance(raw_env, dict):
            raise ConfigError(f"mcp.servers.{srv_name}.env must be a table")

        mcp_servers[srv_name] = MCPServerConfig(
            command=command,
            args=[str(a) for a in raw_args],
            env={str(k): str(v) for k, v in raw_env.items()},
        )

    return AppConfig(
        name=str(app_raw.get("name", "sot-cli")),
        data_dir=str(app_raw.get("data_dir", ".sot-cli")),
        prompt=PromptConfig(system=str(prompt_raw.get("system", "")).strip()),
        runtime=RuntimeConfig(primary_provider=primary_provider),
        tools=ToolConfig(
            default_command_timeout_seconds=_parse_non_negative_int(
                tools_raw.get("default_command_timeout_seconds", 180),
                "tools.default_command_timeout_seconds",
            ),
            binary_check_size=_parse_non_negative_int(tools_raw.get("binary_check_size", 0), "tools.binary_check_size"),
            show_thinking=_parse_bool(tools_raw.get("show_thinking", True), "tools.show_thinking"),
            show_full=_parse_bool(tools_raw.get("show_full", True), "tools.show_full"),
            max_rounds=_parse_non_negative_int(tools_raw.get("max_rounds", 25), "tools.max_rounds"),
            delegated_max_rounds=_parse_non_negative_int(tools_raw.get("delegated_max_rounds", 8), "tools.delegated_max_rounds"),
            repeat_limit=_parse_non_negative_int(tools_raw.get("repeat_limit", 3), "tools.repeat_limit"),
            delegated_repeat_limit=_parse_non_negative_int(tools_raw.get("delegated_repeat_limit", 2), "tools.delegated_repeat_limit"),
            search_default_head_limit=_parse_non_negative_int(
                tools_raw.get("search_default_head_limit", 200),
                "tools.search_default_head_limit",
            ),
            search_max_line_length=_parse_non_negative_int(
                tools_raw.get("search_max_line_length", 500),
                "tools.search_max_line_length",
            ),
            search_timeout_seconds=_parse_positive_int(
                tools_raw.get("search_timeout_seconds", 30),
                "tools.search_timeout_seconds",
            ),
            reasoning_char_budget=_parse_non_negative_int(
                tools_raw.get("reasoning_char_budget", 8000),
                "tools.reasoning_char_budget",
            ),
            delegated_reasoning_char_budget=_parse_non_negative_int(
                tools_raw.get("delegated_reasoning_char_budget", 4000),
                "tools.delegated_reasoning_char_budget",
            ),
            compression_reasoning_trunc_chars=_parse_non_negative_int(
                tools_raw.get("compression_reasoning_trunc_chars", 240),
                "tools.compression_reasoning_trunc_chars",
            ),
            max_readable_file_tokens=_parse_non_negative_int(
                tools_raw.get("max_readable_file_tokens", 64000),
                "tools.max_readable_file_tokens",
            ),
            play_finished_notification=_parse_bool(
                tools_raw.get("play_finished_notification", True),
                "tools.play_finished_notification",
            ),
        ),
        mcp_servers=mcp_servers,
        providers=providers,
    )


def _require_mapping(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key, {})
    if not isinstance(value, dict):
        raise ConfigError(f"{key} must be a table")
    return value


def _optional_mapping(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key, {})
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{key} must be a table")
    return value


def _parse_provider_name(value: Any, field_name: str) -> ProviderName:
    if not isinstance(value, str):
        raise ConfigError(f"{field_name} must be a string")
    if value not in KNOWN_PROVIDERS:
        allowed = ", ".join(KNOWN_PROVIDERS)
        raise ConfigError(f"{field_name} must be one of: {allowed}")
    return value


def _parse_positive_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool):
        raise ConfigError(f"{field_name} must be a positive integer")
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{field_name} must be a positive integer") from exc
    if normalized <= 0:
        raise ConfigError(f"{field_name} must be a positive integer")
    return normalized


def _parse_non_negative_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool):
        raise ConfigError(f"{field_name} must be a non-negative integer")
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{field_name} must be a non-negative integer") from exc
    if normalized < 0:
        raise ConfigError(f"{field_name} must be a non-negative integer")
    return normalized


def _parse_bool(value: Any, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    raise ConfigError(f"{field_name} must be a boolean")