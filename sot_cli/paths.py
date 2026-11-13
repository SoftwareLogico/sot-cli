from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sot_cli.config import AppConfig, resolve_config_path


@dataclass
class AppPaths:
    root_dir: Path
    config_file: Path
    data_dir: Path
    sessions_dir: Path
    logs_dir: Path
    cache_dir: Path


def build_paths(config: AppConfig, config_path: str | Path | None = None) -> AppPaths:
    resolved_config = resolve_config_path(config_path)
    root_dir = resolved_config.parent
    data_dir = Path(config.data_dir).expanduser()
    if not data_dir.is_absolute():
        data_dir = root_dir / data_dir

    return AppPaths(
        root_dir=root_dir,
        config_file=resolved_config,
        data_dir=data_dir,
        sessions_dir=data_dir / "sessions",
        logs_dir=data_dir / "logs",
        cache_dir=data_dir / "cache",
    )


def ensure_runtime_directories(paths: AppPaths) -> None:
    for directory in (paths.data_dir, paths.sessions_dir, paths.logs_dir, paths.cache_dir):
        directory.mkdir(parents=True, exist_ok=True)
