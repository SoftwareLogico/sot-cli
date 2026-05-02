from __future__ import annotations

from pathlib import Path
from typing import Any

from sot_cli.config import KNOWN_PROVIDERS
from sot_cli.session_store import _UNSET
from sot_cli.tools.utils.path_helpers import resolve_path
from sot_cli.tools.utils.validators import (
    _ensure_no_arguments,
    _normalize_float,
    _normalize_positive_int,
)


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    unique_paths: list[Path] = []
    for path in paths:
        resolved = str(path)
        if resolved in seen:
            continue
        seen.add(resolved)
        unique_paths.append(path)
    return unique_paths



def _resolve_source_argument_paths(
    arguments: dict[str, Any],
    root_dir: Path,
) -> list[Path]:
    raw_path = arguments.get("path")
    raw_paths = arguments.get("paths")

    if raw_path is None and raw_paths is None:
        raise ValueError("Provide path or paths")

    normalized_paths: list[Path] = []

    if raw_path is not None:
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ValueError("path must be a non-empty string")
        normalized_paths.append(resolve_path(raw_path.strip(), root_dir))

    if raw_paths is not None:
        if not isinstance(raw_paths, list) or not raw_paths:
            raise ValueError("paths must be a non-empty array of strings")
        for index, item in enumerate(raw_paths):
            if not isinstance(item, str) or not item.strip():
                raise ValueError(f"paths[{index}] must be a non-empty string")
            normalized_paths.append(resolve_path(item.strip(), root_dir))

    return _dedupe_paths(normalized_paths)


def execute_clean_sot(
    arguments: dict[str, Any],
    runtime: Any,
    session_id: str,
) -> dict[str, Any]:
    _ensure_no_arguments(arguments)
    record = runtime.sessions.load(session_id)

    # Collect all session-backed (permanent) paths
    session_paths: list[str] = []
    for entry in record.source_entries:
        session_paths.append(entry.value)

    # Remove all session-backed entries
    for entry in list(record.source_entries):
        try:
            record, _removed = runtime.sessions.remove_source_entry(session_id, entry_id=entry.id)
        except FileNotFoundError:
            pass

    # Reload to get final count
    record = runtime.sessions.load(session_id)

    return {
        "session_id": record.id,
        "cleaned_session_paths": session_paths,
        "cleaned_count": len(session_paths),
        "source_entries": len(record.source_entries),
        "note": "Session-backed entries removed. Tool-backed (ephemeral) entries will be cleared from memory on the next turn.",
    }


def execute_get_session_state(
    arguments: dict[str, Any],
    runtime: Any,
    session_id: str,
    sot_state: Any = None,  # <--- AÑADIR ESTO
) -> dict[str, Any]:
    _ensure_no_arguments(arguments)
    record = runtime.sessions.load(session_id)
    
    # Recopilar archivos trackeados en memoria (Tool-backed)
    in_memory_files = []
    if sot_state is not None:
        in_memory_files = list(sot_state.tracked_files.keys()) + list(sot_state.tracked_media.keys())
        in_memory_files = list(set(in_memory_files)) # Eliminar duplicados

    provider_summaries = []
    for provider_name in KNOWN_PROVIDERS:
        provider = runtime.config.provider(provider_name)
        provider_summaries.append(
            {
                "name": provider.name,
                "enabled": provider.enabled,
                "model": provider.model,
                "temperature": provider.temperature,
                "max_output_tokens": provider.max_output_tokens,
                "base_url": provider.base_url,
            }
        )

    return {
        "session_id": record.id,
        "title": record.title,
        "provider": record.provider,
        "model": record.model,
        "temperature": record.temperature,
        "max_output_tokens": record.max_output_tokens,
        "permanently_attached_entries": len(record.source_entries), 
        "source_entries": [
            {
                "id": entry.id,
                "kind": entry.kind,
                "path": entry.value,
                "label": entry.label,
                "recursive": entry.recursive,
                "added_at": entry.added_at,
            }
            for entry in record.source_entries
        ],
        "ephemeral_tracked_files": in_memory_files,
        "providers": provider_summaries,
    }


def execute_update_session(
    arguments: dict[str, Any],
    runtime: Any,
    session_id: str,
) -> dict[str, Any]:
    if not arguments:
        raise ValueError("At least one session field must be provided.")

    record = runtime.sessions.load(session_id)
    provider = arguments.get("provider")
    model = arguments.get("model")
    title = arguments.get("title")
    temperature = arguments.get("temperature")
    max_output_tokens = arguments.get("max_output_tokens")

    if provider is not None:
        if not isinstance(provider, str) or provider not in KNOWN_PROVIDERS:
            raise ValueError(f"provider must be one of: {', '.join(KNOWN_PROVIDERS)}")
        provider_config = runtime.config.provider(provider)
        if not provider_config.enabled:
            raise ValueError(f"Provider is not configured: {provider}")
        if model is None and provider != record.provider:
            model = provider_config.model
            if not isinstance(model, str) or not model.strip():
                raise ValueError(
                    f"Provider {provider} has no default model configured. Provide model explicitly."
                )

    if model is not None:
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model must be a non-empty string")
        model = model.strip()

    if title is not None:
        if not isinstance(title, str) or not title.strip():
            raise ValueError("title must be a non-empty string")
        title = title.strip()

    if temperature is not None:
        temperature = _normalize_float(temperature, field_name="temperature")

    if max_output_tokens is not None:
        max_output_tokens = _normalize_positive_int(max_output_tokens, field_name="max_output_tokens")

    updated = runtime.sessions.update_session(
        session_id,
        title=title if title is not None else _UNSET,
        provider=provider if provider is not None else _UNSET,
        model=model if model is not None else _UNSET,
        temperature=temperature if temperature is not None else _UNSET,
        max_output_tokens=max_output_tokens if max_output_tokens is not None else _UNSET,
    )
    return {
        "session_id": updated.id,
        "title": updated.title,
        "provider": updated.provider,
        "model": updated.model,
        "temperature": updated.temperature,
        "max_output_tokens": updated.max_output_tokens,
    }


def execute_detach_path(
    arguments: dict[str, Any],
    runtime: Any,
    session_id: str,
    root_dir: Path,
) -> dict[str, Any]:
    resolved_paths = _resolve_source_argument_paths(arguments, root_dir)
    record = runtime.sessions.load(session_id)

    # Best-effort: remove each path from session entries when present.
    # Paths that are only tool-backed (not in session.source_entries) are
    # accepted silently — they will be cleaned from the in-memory SoT by
    # update_tracked_from_tool_result once this result is returned.
    detached_from_session: list[str] = []
    detached_ids: list[str] = []
    tool_backed_only: list[str] = []

    for path in resolved_paths:
        try:
            record, removed = runtime.sessions.remove_source_entry(session_id, path=path)
            detached_from_session.append(str(path))
            detached_ids.append(removed.id)
        except FileNotFoundError:
            tool_backed_only.append(str(path))

    all_detached = detached_from_session + tool_backed_only
    return {
        "session_id": record.id,
        "detached_path": all_detached[0] if all_detached else "",
        "detached_paths": all_detached,
        "detached_from_session": detached_from_session,
        "detached_from_context_only": tool_backed_only,
        "detached_count": len(all_detached),
        "entry_ids": detached_ids,
        "source_entries": len(record.source_entries),
    }


def execute_attach_path(
    arguments: dict[str, Any],
    runtime: Any,
    session_id: str,
    root_dir: Path,
) -> dict[str, Any]:
    resolved_paths = _resolve_source_argument_paths(arguments, root_dir)
    recursive = bool(arguments.get("recursive", True))
    label = arguments.get("label")
    normalized_label = str(label).strip() if isinstance(label, str) and label.strip() else None
    if normalized_label is not None and len(resolved_paths) > 1:
        raise ValueError("label can only be used when attaching a single path")

    missing_paths = [str(path) for path in resolved_paths if not path.exists()]
    if missing_paths:
        raise FileNotFoundError("Path not found: " + ", ".join(missing_paths))

    record = runtime.sessions.load(session_id)
    for path in resolved_paths:
        record = runtime.sessions.attach_path(
            session_id=session_id,
            target_path=path,
            label=normalized_label,
            recursive=recursive,
        )

    attached_paths = [str(path) for path in resolved_paths]
    return {
        "session_id": record.id,
        "attached_path": attached_paths[0],
        "attached_paths": attached_paths,
        "attached_count": len(attached_paths),
        "recursive": recursive,
        "source_entries": len(record.source_entries),
    }
