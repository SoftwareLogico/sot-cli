from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import mimetypes

from sot_cli.session_store import SessionRecord, SourceEntry


@dataclass
class SourceIndexItem:
    path: str
    size_bytes: int


@dataclass
class SourceFileSnapshot:
    path: str
    size_bytes: int
    content: str


@dataclass
class SourceBundle:
    index_items: list[SourceIndexItem] = field(default_factory=list)
    text_snapshots: list[SourceFileSnapshot] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def total_files(self) -> int:
        return len(self.index_items)

    @property
    def total_bytes(self) -> int:
        return sum(item.size_bytes for item in self.index_items)

    @property
    def total_snapshot_files(self) -> int:
        return len(self.text_snapshots)

    @property
    def total_snapshot_bytes(self) -> int:
        return sum(item.size_bytes for item in self.text_snapshots)

    def build_index(self) -> str:
        lines = [
            "SOURCE OF TRUTH INDEX",
            "This index describes the current authoritative working set for this session.",
            f"Files: {self.total_files}",
            f"Bytes: {self.total_bytes}",
            f"Text snapshots included this turn: {self.total_snapshot_files}",
            "",
        ]
        for item in self.index_items:
            lines.append(f"- {item.path} ({item.size_bytes} bytes)")
        if self.skipped:
            lines.extend(["", "Skipped:"])
            lines.extend(f"- {entry}" for entry in self.skipped)
        return "\n".join(lines)

    def build_contents_payload(self) -> str:
        if not self.text_snapshots:
            return ""

        sections = [
            "SOURCE OF TRUTH FILE CONTENTS",
            "Use the content below as the current authoritative snapshot. Ignore stale historical versions.",
            "",
        ]
        for item in self.text_snapshots:
            sections.append(f"BEGIN FILE: {item.path}")
            sections.append(item.content)
            sections.append(f"END FILE: {item.path}")
            sections.append("")
        return "\n".join(sections).strip()


def build_source_bundle(session: SessionRecord) -> SourceBundle:
    bundle = SourceBundle()
    seen_paths: set[str] = set()

    for entry in session.source_entries:
        _materialize_entry(entry, bundle, seen_paths)

    return bundle


def _materialize_entry(
    entry: SourceEntry,
    bundle: SourceBundle,
    seen_paths: set[str],
) -> None:
    path = Path(entry.value)
    if entry.kind == "file":
        _register_file(path, entry, bundle, seen_paths)
        return

    if entry.kind == "directory":
        iterator = path.rglob("*") if entry.recursive else path.glob("*")
        for candidate in sorted(iterator):
            if candidate.is_file():
                _register_file(candidate, entry, bundle, seen_paths, root=path)
        return

    bundle.skipped.append(f"unsupported-entry-kind: {entry.kind}:{entry.value}")


def _register_file(
    path: Path,
    entry: SourceEntry,
    bundle: SourceBundle,
    seen_paths: set[str],
    root: Path | None = None,
) -> None:
    resolved = str(path.resolve())
    if resolved in seen_paths:
        return
    seen_paths.add(resolved)

    try:
        size_bytes = path.stat().st_size
    except OSError:
        bundle.skipped.append(f"stat-failed: {path}")
        return

    bundle.index_items.append(
        SourceIndexItem(
            path=resolved,
            size_bytes=size_bytes,
        )
    )

    if not _is_probably_text(path):
        bundle.skipped.append(f"cannot-include-binary: {path}")
        return

    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        bundle.skipped.append(f"cannot-include-decode-failed: {path}")
        return

    bundle.text_snapshots.append(
        SourceFileSnapshot(
            path=resolved,
            size_bytes=size_bytes,
            content=content,
        )
    )


def _is_probably_text(path: Path) -> bool:
    """
    Detect if a file is text. Not by a whitelist of extensions, but by actual content.
    Any file that decodes as UTF-8 without null bytes is text, regardless of extension.
    This covers .py, .rs, .md, .json, .toml, .yaml, .env, .cfg, Makefile,
    Dockerfile, .gitignore, extensionless scripts, and anything else that IS text.
    """
    # Fast path: mime type says text
    mime_type, _ = mimetypes.guess_type(path.name)
    if mime_type:
        if mime_type.startswith("text/"):
            return True
        if mime_type in {"application/json", "application/xml", "application/javascript",
                         "application/x-yaml", "application/toml", "application/x-sh",
                         "application/x-httpd-php", "application/sql",
                         "application/graphql", "application/ld+json",
                         "application/x-perl", "application/x-ruby",
                         "application/x-python", "application/x-lua"}:
            return True

    # Content-based detection: read a chunk and check for binary markers
    try:
        chunk = path.read_bytes()[:32768]
    except OSError:
        return False

    if not chunk:
        return True  # empty files are fine

    # Null bytes = binary
    if b"\x00" in chunk:
        return False

    # High ratio of non-printable non-whitespace bytes = binary
    non_text = sum(1 for b in chunk if b < 8 or (b > 13 and b < 32 and b != 27))
    if len(chunk) > 0 and non_text / len(chunk) > 0.10:
        return False

    return True
