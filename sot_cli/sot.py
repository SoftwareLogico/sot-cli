from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any

from sot_cli.constants import SOT_MARKER, IMAGE_EXTENSIONS, PDF_EXTENSIONS, AUDIO_EXTENSIONS, VIDEO_EXTENSIONS
from sot_cli.message_builder import build_sot_user_message
from sot_cli.providers.base import ProviderCapability, ProviderRequest
from sot_cli.runtime import AppRuntime
from sot_cli.session_store import SourceEntry
from sot_cli.source_of_truth import build_source_bundle
from sot_cli.tools.core import ToolPayload
from sot_cli.tools.reader.main import execute_read_text_file


@dataclass
class SoTState:
    tracked_files: dict[str, str] = field(default_factory=dict)
    tracked_media: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    tracked_file_mtimes: dict[str, int] = field(default_factory=dict)
    session_source_entries: list[SourceEntry] = field(default_factory=list)
    tracked_file_estimated_tokens: dict[str, int | None] = field(default_factory=dict)
    session_source_entries: list[SourceEntry] = field(default_factory=list)
    session_tracked_file_paths: set[str] = field(default_factory=set)
    session_tracked_media_paths: set[str] = field(default_factory=set)


def _estimate_tokens(text: str) -> int | None:
    """Estimate token count using tiktoken o200k_base (GPT-4o tokenizer).
    Returns None if tiktoken is not available."""
    try:
        import tiktoken
        enc = tiktoken.get_encoding("o200k_base")
        return len(enc.encode(text))
    except Exception:
        return None


def _store_file_token_estimate(state: SoTState, fpath: str) -> None:
    content = state.tracked_files.get(fpath)
    if isinstance(content, str) and content:
        state.tracked_file_estimated_tokens[fpath] = _estimate_tokens(content)
    else:
        state.tracked_file_estimated_tokens.pop(fpath, None)


def begin_turn(state: SoTState) -> None:
    del state


def is_sot_block_content(content: Any) -> bool:
    if isinstance(content, str):
        return content.startswith(SOT_MARKER)
    if isinstance(content, list) and content:
        first = content[0]
        if isinstance(first, dict) and first.get("type") == "text":
            text = first.get("text", "")
            return isinstance(text, str) and text.startswith(SOT_MARKER)
    return False


# Fingerprints that identify orchestration rules messages.
# These are ephemeral (re-injected fresh each turn) and must NOT
# be persisted into chat_history when resuming a session.
_ORCHESTRATION_FINGERPRINTS = (
    "ORCHESTRATION, BATCHING",
    "EXPLORATION & DISCOVERY",
    "TOOL STRATEGY",
    "TOKEN ECONOMY & BATCHING",
    "HOST ENVIRONMENT",
    "You are in agent mode.",
    "You are in sub-agent mode.",
    "BATCH FILE READS",
)


def is_orchestration_rules_content(content: Any) -> bool:
    """Return True if the content looks like an orchestration rules block."""
    text = ""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list) and content:
        first = content[0]
        if isinstance(first, dict) and first.get("type") == "text":
            text = first.get("text", "")
    if not text:
        return False
    # Need at least 2 fingerprints to match (avoid false positives on user messages)
    matches = sum(1 for fp in _ORCHESTRATION_FINGERPRINTS if fp in text)
    return matches >= 2


def load_sot_state_from_request_json(session_dir: Path) -> SoTState | None:
    request_path = session_dir / "request.json"
    if not request_path.exists():
        return None

    try:
        request_data = json.loads(request_path.read_text(encoding="utf-8"))
        messages = request_data.get("payload", {}).get("messages", [])
    except (json.JSONDecodeError, OSError, AttributeError):
        return None

    if not isinstance(messages, list):
        return None

    for message in reversed(messages):
        if not isinstance(message, dict):
            continue
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if not is_sot_block_content(content):
            continue
        return _deserialize_sot_message(content)

    return None


def merge_session_into_tracked(
    runtime: AppRuntime,
    request: ProviderRequest,
    state: SoTState,
) -> None:
    session = runtime.sessions.load(request.session_id)
    state.session_source_entries = list(session.source_entries)

    bundle = build_source_bundle(session)
    session_snapshots = {snapshot.path: snapshot.content for snapshot in bundle.text_snapshots}

    for path in list(state.session_tracked_file_paths):
        if not _is_session_backed_path(path, state.session_source_entries):
            state.tracked_files.pop(path, None)
            state.tracked_file_estimated_tokens.pop(path, None)
            state.tracked_file_mtimes.pop(path, None)
            state.session_tracked_file_paths.discard(path)

    for path in list(state.session_tracked_media_paths):
        if not _is_session_backed_path(path, state.session_source_entries):
            state.tracked_media.pop(path, None)
            state.session_tracked_media_paths.discard(path)

    for path, content in session_snapshots.items():
        state.tracked_files[path] = content
        _store_file_token_estimate(state, path)
        if _is_session_backed_path(path, state.session_source_entries):
            state.session_tracked_file_paths.add(path)

    # Rescatar archivos multimedia que fueron omitidos por ser binarios
    for skipped_msg in bundle.skipped:
        if skipped_msg.startswith("cannot-include-binary: "):
            bin_path = skipped_msg.replace("cannot-include-binary: ", "").strip()
            ext = bin_path.split(".")[-1].lower() if "." in bin_path else ""
            
            if ext in IMAGE_EXTENSIONS | PDF_EXTENSIONS | AUDIO_EXTENSIONS | VIDEO_EXTENSIONS:
                if bin_path not in state.tracked_media:
                    # Una lista vacía forzará a _refresh_tracked_media_from_disk a leer el archivo
                    state.tracked_media[bin_path] = []
                if _is_session_backed_path(bin_path, state.session_source_entries):
                    state.session_tracked_media_paths.add(bin_path)


def refresh_tracked_state_from_disk(
    runtime: AppRuntime,
    capability: ProviderCapability,
    state: SoTState,
) -> None:
    _refresh_tracked_files_from_disk(state)
    _refresh_tracked_media_from_disk(runtime, capability, state)


def update_tracked_from_tool_result(
    state: SoTState,
    tool_name: str,
    tool_result: Any,
) -> None:
    if tool_result.is_error:
        return

    try:
        payload = json.loads(tool_result.record_content)
    except (json.JSONDecodeError, TypeError):
        return

    if tool_name == "read_files":
        results = payload.get("results")
        if not isinstance(results, list):
            return

        supplemental_messages = list(tool_result.supplemental_messages)
        supplemental_index = 0
        for item in results:
            if not isinstance(item, dict) or not item.get("ok"):
                continue

            result_type = item.get("type", "")
            current_messages: list[dict[str, Any]] = []
            if result_type in {"image", "pdf", "notebook", "audio", "video"} and supplemental_index < len(supplemental_messages):
                current_messages = [supplemental_messages[supplemental_index]]
                supplemental_index += 1

            _update_single_read_result(state, item, current_messages)
        return

    if tool_name == "write_file":
        fpath = payload.get("path")
        if isinstance(fpath, str) and fpath:
            state.tracked_files.setdefault(fpath, "")
            _store_file_token_estimate(state, fpath)
            if _is_session_backed_path(fpath, state.session_source_entries):
                state.session_tracked_file_paths.add(fpath)
        return

    if tool_name == "edit_files":
        # Multi-file edit: per-file results steer the SoT refresh policy.
        #
        # Rules (intentional asymmetry between create and update):
        #
        #   - operation == "create" → ALWAYS add to the SoT. A freshly
        #     created file is almost always something the model will work
        #     with on the next turn; auto-injecting saves the turn it would
        #     otherwise waste calling read_files. The cost is bounded
        #     because edit_files creation goes through the model's output
        #     budget — large generated dumps would use write_file or
        #     run_command with redirection instead. If the model decides it
        #     does NOT want the new file tracked it can call detach_path.
        #
        #   - operation == "update" on a path ALREADY tracked (or under a
        #     permanently-attached source entry) → refresh from disk on the
        #     next turn so the SoT shows the post-edit content.
        #
        #   - operation == "update" on a path NOT in the SoT and NOT
        #     session-backed → DO NOT inject. The tool result tells the
        #     model "file X updated"; the SoT/context stays clean. Silently
        #     adding unknown files would bloat the context for no benefit
        #     (the model already has the success report).
        results = payload.get("results")
        if not isinstance(results, list):
            return
        for entry in results:
            if not isinstance(entry, dict) or not entry.get("ok"):
                continue
            fpath = entry.get("path")
            if not isinstance(fpath, str) or not fpath:
                continue

            operation = entry.get("operation")
            is_session_backed = _is_session_backed_path(fpath, state.session_source_entries)

            if operation == "create":
                state.tracked_files.setdefault(fpath, "")
                _store_file_token_estimate(state, fpath)
                if is_session_backed:
                    state.session_tracked_file_paths.add(fpath)
                continue

            # operation == "update" path
            is_already_tracked = (
                fpath in state.tracked_files or fpath in state.tracked_media
            )
            if not (is_already_tracked or is_session_backed):
                continue
            state.tracked_files.setdefault(fpath, "")
            _store_file_token_estimate(state, fpath)
            if is_session_backed:
                state.session_tracked_file_paths.add(fpath)
        return

    if tool_name == "delete_file":
        fpath = payload.get("path")
        if isinstance(fpath, str) and fpath:
            state.tracked_files.pop(fpath, None)
            state.tracked_file_estimated_tokens.pop(fpath, None)
            state.tracked_media.pop(fpath, None)
            state.tracked_file_mtimes.pop(fpath, None)
            state.session_tracked_file_paths.discard(fpath)
            state.session_tracked_media_paths.discard(fpath)

    if tool_name == "clean_sot":
        # Remove ALL tracked paths — both session-backed and tool-backed — from in-memory SoT.
        state.tracked_files.clear()
        state.tracked_media.clear()
        state.tracked_file_mtimes.clear()
        state.tracked_file_estimated_tokens.clear()
        state.session_tracked_file_paths.clear()
        state.session_tracked_media_paths.clear()
        return

    if tool_name == "detach_path_from_source":
        # Remove both session-backed and tool-backed paths from the in-memory SoT.
        # detached_paths contains ALL paths that were requested (regardless of
        # whether they were in session.source_entries or only tool-backed).
        detached = payload.get("detached_paths")
        if not isinstance(detached, list):
            single = payload.get("detached_path")
            detached = [single] if isinstance(single, str) and single else []
        for fpath in detached:
            if isinstance(fpath, str) and fpath:
                state.tracked_files.pop(fpath, None)
                state.tracked_file_estimated_tokens.pop(fpath, None)
                state.tracked_media.pop(fpath, None)
                state.tracked_file_mtimes.pop(fpath, None)
                state.session_tracked_file_paths.discard(fpath)
                state.session_tracked_media_paths.discard(fpath)


def build_sot_payload_message(state: SoTState) -> dict[str, Any] | None:
    if not state.tracked_files and not state.tracked_media:
        return None
    return build_sot_user_message(
        state.tracked_files,
        state.tracked_media,
        media_file_count=len(state.tracked_media),
    )


def _deserialize_sot_message(content: Any) -> SoTState:
    state = SoTState()
    sot_text = _extract_sot_text(content)
    if isinstance(sot_text, str) and sot_text:
        state.tracked_files = _parse_tracked_files_from_text(sot_text)
    if isinstance(content, list):
        state.tracked_media = _parse_tracked_media_from_parts(content)
    return state


def _extract_sot_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list) and content:
        text_parts: list[str] = []
        for part in content:
            if not isinstance(part, dict) or part.get("type") != "text":
                continue
            text = part.get("text")
            if isinstance(text, str):
                text_parts.append(text)
        if text_parts:
            return "\n\n".join(text_parts)
    return ""


def _parse_tracked_files_from_text(sot_text: str) -> dict[str, str]:
    tracked_files: dict[str, str] = {}
    lines = sot_text.split("\n")
    index = 0

    while index < len(lines):
        line = lines[index]
        if not line.startswith("--- FILE: ") or not line.endswith(" ---"):
            index += 1
            continue

        header = line[len("--- FILE: "):-len(" ---")]
        path = header.rsplit(" (", 1)[0]
        end_marker = f"--- END: {path} ---"

        index += 1
        body: list[str] = []
        while index < len(lines) and lines[index] != end_marker:
            raw_line = lines[index]
            # Strip line-number prefix if present ("     1|")
            if len(raw_line) >= 7 and raw_line[6] == "|":
                prefix = raw_line[:6]
                if prefix.replace(" ", "").isdigit():
                    raw_line = raw_line[7:]
            body.append(raw_line)
            index += 1

        tracked_files[path] = "\n".join(body)
        if index < len(lines):
            index += 1

    return tracked_files


def _parse_tracked_media_from_parts(content_parts: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    tracked_media: dict[str, list[dict[str, Any]]] = {}
    current_path: str | None = None
    current_parts: list[dict[str, Any]] = []

    for part in content_parts[1:]:
        if not isinstance(part, dict):
            continue

        if _is_end_of_sot_text_part(part):
            if current_path is not None and current_parts:
                tracked_media[current_path] = list(current_parts)
            break

        source_path = _extract_media_source_path(part)
        if source_path is not None:
            if current_path is not None and current_parts:
                tracked_media[current_path] = list(current_parts)
            current_path = source_path
            current_parts = [part]
            continue

        if current_path is not None:
            current_parts.append(part)

    if current_path is not None and current_parts:
        tracked_media[current_path] = list(current_parts)

    return tracked_media


def _extract_media_source_path(part: dict[str, Any]) -> str | None:
    if part.get("type") != "text":
        return None
    text = part.get("text")
    if not isinstance(text, str):
        return None

    marker = " content from read_text_file for "
    if not text.startswith("Supplemental ") or marker not in text:
        return None

    tail = text.split(marker, 1)[1]
    for separator in (". Requested pages:", ".\n", "\n", ". "):
        if separator in tail:
            candidate = tail.split(separator, 1)[0]
            return candidate or None
    if tail.endswith("."):
        return tail[:-1] or None
    return tail or None


def _flatten_media(tracked_media: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    flat: list[dict[str, Any]] = []
    for parts in tracked_media.values():
        flat.extend(parts)
    return flat


def _is_end_of_sot_text_part(part: dict[str, Any]) -> bool:
    if part.get("type") != "text":
        return False
    text = part.get("text")
    return isinstance(text, str) and text.strip() == "=== END SOURCE OF TRUTH ==="


def _refresh_tracked_files_from_disk(state: SoTState) -> None:
    for fpath in list(state.tracked_files.keys()):
        path = Path(fpath)
        if not path.exists() or path.is_dir():
            continue
        try:
            state.tracked_files[fpath] = path.read_text(encoding="utf-8")
            _store_file_token_estimate(state, fpath)
            try:
                stat = path.stat()
                mtime_ns = getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000))
                state.tracked_file_mtimes[fpath] = mtime_ns
            except OSError:
                state.tracked_file_mtimes.pop(fpath, None)
        except (UnicodeDecodeError, OSError):
            continue


def _refresh_tracked_media_from_disk(
    runtime: AppRuntime,
    capability: ProviderCapability,
    state: SoTState,
) -> None:
    for fpath in list(state.tracked_media.keys()):
        try:
            raw_result = execute_read_text_file(
                {"path": fpath},
                root_dir=runtime.paths.root_dir,
                read_cache={},
                binary_check_size=runtime.config.tools.binary_check_size,
                supports_images=capability.supports_images,
                supports_pdf=capability.supports_pdfs,
                supports_audio=capability.supports_audio,
                supports_video=capability.supports_video,
                file_unchanged_stub="File unchanged since last read.",
            )
        except Exception:
            continue

        if not isinstance(raw_result, ToolPayload):
            continue

        media_parts = _extract_media_parts(raw_result.supplemental_messages)
        if media_parts:
            state.tracked_media[fpath] = media_parts


def _extract_media_parts(supplemental_messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    media_parts: list[dict[str, Any]] = []
    for message in supplemental_messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            part_type = part.get("type", "")
            if part_type in {"image_url", "input_audio", "video_url", "file", "text"}:
                media_parts.append(part)
    return media_parts


def _update_single_read_result(
    state: SoTState,
    payload: dict[str, Any],
    supplemental_messages: list[dict[str, Any]],
) -> None:
    fpath = payload.get("path")
    ftype = payload.get("type", "")
    if not isinstance(fpath, str) or not fpath:
        return

    if ftype in {"image", "pdf", "notebook", "audio", "video"}:
        media_parts = _extract_media_parts(supplemental_messages)
        if media_parts:
            state.tracked_media[fpath] = media_parts
            if _is_session_backed_path(fpath, state.session_source_entries):
                state.session_tracked_media_paths.add(fpath)
        return

    if ftype in {"file_unchanged", "file_in_sot"}:
        return

    content = payload.get("content")
    if isinstance(content, str):
        state.tracked_files[fpath] = content
        _store_file_token_estimate(state, fpath)
        mtime_ns = payload.get("modified_ns")
        if isinstance(mtime_ns, int):
            state.tracked_file_mtimes[fpath] = mtime_ns
        if _is_session_backed_path(fpath, state.session_source_entries):
            state.session_tracked_file_paths.add(fpath)


def _is_session_backed_path(path: str, session_source_entries: list[SourceEntry]) -> bool:
    candidate = Path(path)
    for entry in session_source_entries:
        entry_path = Path(entry.value)
        if entry.kind == "file":
            if candidate == entry_path:
                return True
            continue
        if entry.kind != "directory":
            continue
        try:
            relative = candidate.relative_to(entry_path)
        except ValueError:
            continue
        if entry.recursive:
            return True
        if len(relative.parts) == 1:
            return True
    return False