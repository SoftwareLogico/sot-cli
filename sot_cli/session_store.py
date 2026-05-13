from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
import json
import os
import uuid


SourceEntryKind = Literal["file", "directory"]

_UNSET = object()


@dataclass
class SourceEntry:
    id: str
    kind: SourceEntryKind
    value: str
    label: str
    recursive: bool = True
    added_at: str = field(default_factory=lambda: _utc_now().isoformat())


@dataclass
class SessionRecord:
    id: str
    title: str
    provider: str
    model: str
    created_at: str
    updated_at: str
    subagent_model: str = ""
    reasoning_effort: str | None = None
    temperature: float | None = None
    max_output_tokens: int | None = None
    source_entries: list[SourceEntry] = field(default_factory=list)


class SessionStore:
    def __init__(self, sessions_dir: Path) -> None:
        self.sessions_dir = sessions_dir
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

    def create_session(
        self,
        title: str,
        provider: str,
        model: str,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        subagent_model: str = "",
        reasoning_effort: str | None = None,
    ) -> SessionRecord:
        session_id = self._reserve_session_id()
        timestamp = _utc_now().isoformat()
        record = SessionRecord(
            id=session_id,
            title=title,
            provider=provider,
            model=model,
            subagent_model=subagent_model,
            reasoning_effort=reasoning_effort,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            created_at=timestamp,
            updated_at=timestamp,
        )
        self.save(record)
        return record

    def list_sessions(self) -> list[SessionRecord]:
        records: list[SessionRecord] = []
        for session_file in sorted(self.sessions_dir.glob("*/session.json")):
            try:
                records.append(self._read(session_file))
            except Exception:
                # Skip corrupted/unreadable session files instead of crashing
                # the entire listing — one bad file should not block the rest.
                continue
        return sorted(records, key=lambda item: item.updated_at, reverse=True)

    def load(self, session_id: str) -> SessionRecord:
        path = self._session_file(session_id)
        if not path.exists():
            raise FileNotFoundError(f"Session not found: {session_id}")
        return self._read(path)

    def save(self, record: SessionRecord) -> None:
        session_dir = self._session_dir(record.id)
        session_dir.mkdir(parents=True, exist_ok=True)
        target_file = self._session_file(record.id)
        temp_file = target_file.with_suffix(".tmp")

        temp_file.write_text(
            json.dumps(asdict(record), ensure_ascii=True, indent=2),
            encoding="utf-8",
        )
        os.replace(temp_file, target_file)

    def attach_path(
        self,
        session_id: str,
        target_path: str | Path,
        label: str | None = None,
        recursive: bool = True,
    ) -> SessionRecord:
        record = self.load(session_id)
        path = Path(target_path).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(f"Path not found: {path}")

        kind: SourceEntryKind = "directory" if path.is_dir() else "file"
        value = str(path)
        existing = next((entry for entry in record.source_entries if entry.kind == kind and entry.value == value), None)
        if existing is not None:
            changed = False
            if label and existing.label != label:
                existing.label = label
                changed = True
            if existing.recursive != recursive:
                existing.recursive = recursive
                changed = True
            if changed:
                record.updated_at = _utc_now().isoformat()
                self.save(record)
            return record

        record.source_entries.append(
            SourceEntry(
                id=uuid.uuid4().hex[:6],
                kind=kind,
                value=value,
                label=label or path.name,
                recursive=recursive,
            )
        )
        record.updated_at = _utc_now().isoformat()
        self.save(record)
        return record

    def update_session(
        self,
        session_id: str,
        *,
        title: str | object = _UNSET,
        provider: str | object = _UNSET,
        model: str | object = _UNSET,
        subagent_model: str | object = _UNSET,
        temperature: float | None | object = _UNSET,
        max_output_tokens: int | None | object = _UNSET,
    ) -> SessionRecord:
        record = self.load(session_id)
        changed = False

        if title is not _UNSET and isinstance(title, str) and title != record.title:
            record.title = title
            changed = True
        if provider is not _UNSET and isinstance(provider, str) and provider != record.provider:
            record.provider = provider
            changed = True
        if model is not _UNSET and isinstance(model, str) and model != record.model:
            record.model = model
            changed = True
        if subagent_model is not _UNSET and isinstance(subagent_model, str) and subagent_model != record.subagent_model:
            record.subagent_model = subagent_model
            changed = True
        if temperature is not _UNSET and temperature != record.temperature:
            record.temperature = temperature if isinstance(temperature, float) or temperature is None else float(temperature)
            changed = True
        if max_output_tokens is not _UNSET and max_output_tokens != record.max_output_tokens:
            record.max_output_tokens = (
                max_output_tokens if isinstance(max_output_tokens, int) or max_output_tokens is None else int(max_output_tokens)
            )
            changed = True

        if changed:
            record.updated_at = _utc_now().isoformat()
            self.save(record)
        return record

    def remove_source_entry(
        self,
        session_id: str,
        *,
        path: str | Path | None = None,
        entry_id: str | None = None,
    ) -> tuple[SessionRecord, SourceEntry]:
        record = self.load(session_id)
        if path is None and entry_id is None:
            raise ValueError("Either path or entry_id is required")

        resolved_path = str(Path(path).expanduser().resolve()) if path is not None else None
        for index, entry in enumerate(record.source_entries):
            if entry_id is not None and entry.id.startswith(entry_id):
                removed = record.source_entries.pop(index)
                record.updated_at = _utc_now().isoformat()
                self.save(record)
                return record, removed
            if resolved_path is not None and entry.value == resolved_path:
                removed = record.source_entries.pop(index)
                record.updated_at = _utc_now().isoformat()
                self.save(record)
                return record, removed

        raise FileNotFoundError("Source entry not found in session")

    def _session_dir(self, session_id: str) -> Path:
        return self.sessions_dir / session_id

    def _reserve_session_id(self) -> str:
        base = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
        candidate = base
        sequence = 1

        while True:
            session_dir = self._session_dir(candidate)
            try:
                session_dir.mkdir(parents=True, exist_ok=False)
                return candidate
            except FileExistsError:
                candidate = f"{base}-{sequence:02d}"
                sequence += 1

    def _session_file(self, session_id: str) -> Path:
        return self._session_dir(session_id) / "session.json"

    def _read(self, path: Path) -> SessionRecord:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Session file is corrupted: {path}") from exc

        entries = [
            SourceEntry(
                id=entry["id"],
                kind=entry["kind"],
                value=entry["value"],
                label=entry["label"],
                recursive=entry.get("recursive", True),
                added_at=entry.get("added_at", _utc_now().isoformat()),
            )
            for entry in payload.get("source_entries", [])
        ]
        return SessionRecord(
            id=payload["id"],
            title=payload["title"],
            provider=payload["provider"],
            model=payload["model"],
            subagent_model=payload.get("subagent_model", ""),
            reasoning_effort=payload.get("reasoning_effort"),
            temperature=payload.get("temperature"),
            max_output_tokens=payload.get("max_output_tokens"),
            created_at=payload["created_at"],
            updated_at=payload["updated_at"],
            source_entries=entries,
        )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
