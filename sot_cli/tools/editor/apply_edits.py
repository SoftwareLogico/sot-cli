"""Unified surgical file-editing tool.

This is the single entry point for any text mutation. The public function
:func:`execute_edit_files` accepts a ``files`` array — each entry is one
file with its own ``edits`` array applied atomically — so a single tool
call can mutate one file or many. Three editing modes share the same
``edits`` array and are detected by which keys an edit object carries:

* **text mode** — ``old_string`` (+ optional ``new_string``,
  ``before_context``, ``after_context``, ``replace_all``). Replaces or
  deletes (``new_string=""``) an exact text span. ``replace_all`` expands to
  every match after context filtering.
* **line-range mode** — ``start_line`` + ``end_line`` + ``new_string``.
  Replaces the contents of a 1-indexed inclusive line range, or deletes it
  when ``new_string=""``.
* **insert mode** — ``insert_line`` + ``position`` (``"before"`` or
  ``"after"``) + ``new_string``. Pure insertion at the line boundary, never
  touches the line itself.

Cross-mode rules:

* All edits are resolved against the ORIGINAL file content first; the
  splice is applied in descending offset order so earlier offsets stay
  valid throughout. If anything fails (target not found, overlap, etc.)
  nothing is written — the file on disk is untouched.
* Two edits cannot share an offset boundary if either is zero-width
  (insert touching another edit is rejected as ambiguous; merge them into
  a single edit instead).
* Line endings (CRLF vs LF) are preserved by re-encoding replacement
  text to match the surrounding file. Inserts auto-add the file's line
  separator when needed so they never fuse with adjacent lines, and
  prepend one when appending past an EOF that lacked a trailing newline.
* File creation: when the file does not exist, the call must consist of
  exactly one text-mode edit with ``old_string=""``; the new file is
  created with ``new_string`` as its content.
"""
from __future__ import annotations

from pathlib import Path
import re
from typing import Any

from sot_cli.tools.editor.text_utils import (
    _match_line_endings,
    _normalize_quotes,
    _prepare_replacement_text,
    _preserve_quote_style,
)
from sot_cli.tools.utils.path_helpers import resolve_path
from sot_cli.tools.utils.validators import _require_string, _require_string_allow_empty
from sot_cli.utils.text import _count_lines


class _EditValidationError(ValueError):
    pass


# ─── primitive validators ────────────────────────────────────────────────


def _normalize_optional_string(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _EditValidationError(f"{field_name} must be a string")
    return value


def _normalize_positive_line(value: Any, field_name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise _EditValidationError(f"{field_name} must be a positive integer")
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise _EditValidationError(f"{field_name} must be a positive integer") from exc
    if normalized <= 0:
        raise _EditValidationError(f"{field_name} must be a positive integer")
    return normalized


def _normalize_bool(value: Any, field_name: str) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    raise _EditValidationError(f"{field_name} must be a boolean")


# ─── edits-array validator ───────────────────────────────────────────────


def _require_edits(arguments: dict[str, Any]) -> list[dict[str, Any]]:
    """Parse and normalize the ``edits`` array.

    Returns a list of dicts where ``mode`` is one of ``"text"``,
    ``"line_range"``, ``"insert"`` and the remaining keys hold whatever
    that mode needs. Mode is determined by which targeting keys appear; the
    validator rejects any combination that mixes targeting modes or supplies
    options (``replace_all``, ``before_context``, etc.) outside the mode that
    accepts them.
    """
    raw_edits = arguments.get("edits")
    if not isinstance(raw_edits, list) or not raw_edits:
        raise _EditValidationError("edits must be a non-empty array")

    normalized_edits: list[dict[str, Any]] = []
    for index, item in enumerate(raw_edits, start=1):
        if not isinstance(item, dict):
            raise _EditValidationError(f"edits[{index}] must be an object")

        new_string = _require_string_allow_empty(item, "new_string")

        old_string = _normalize_optional_string(item.get("old_string"), f"edits[{index}].old_string")
        before_context = _normalize_optional_string(item.get("before_context"), f"edits[{index}].before_context")
        after_context = _normalize_optional_string(item.get("after_context"), f"edits[{index}].after_context")
        replace_all = _normalize_bool(item.get("replace_all"), f"edits[{index}].replace_all")
        start_line = _normalize_positive_line(item.get("start_line"), f"edits[{index}].start_line")
        end_line = _normalize_positive_line(item.get("end_line"), f"edits[{index}].end_line")
        insert_line = _normalize_positive_line(item.get("insert_line"), f"edits[{index}].insert_line")
        position = _normalize_optional_string(item.get("position"), f"edits[{index}].position")

        has_text = old_string is not None
        has_line_range = start_line is not None or end_line is not None
        has_insert = insert_line is not None or position is not None

        # Exactly one targeting mode must be picked.
        modes_picked = sum([has_text, has_line_range, has_insert])
        if modes_picked == 0:
            raise _EditValidationError(
                f"edits[{index}] must target text (old_string), a line range "
                f"(start_line/end_line), or an insert position (insert_line/position)"
            )
        if modes_picked > 1:
            raise _EditValidationError(
                f"edits[{index}] mixes targeting modes — pick exactly one of: "
                f"old_string OR start_line/end_line OR insert_line/position"
            )

        if has_text:
            # ``old_string=""`` is reserved for the file-creation path; the
            # caller (execute_edit_files via _apply_edits_to_one_file) checks ``len(edits)==1`` and that
            # the file does not exist before allowing it. Anywhere else it
            # is a malformed edit.
            if before_context is not None or after_context is not None:
                if old_string == "":
                    raise _EditValidationError(
                        f"edits[{index}] cannot use before_context/after_context with empty old_string"
                    )
            if replace_all and old_string == "":
                raise _EditValidationError(
                    f"edits[{index}] cannot use replace_all with empty old_string"
                )
            normalized_edits.append({
                "index": index,
                "mode": "text",
                "new_string": new_string,
                "old_string": old_string,
                "before_context": before_context,
                "after_context": after_context,
                "replace_all": replace_all,
            })
            continue

        if has_line_range:
            if start_line is None or end_line is None:
                raise _EditValidationError(
                    f"edits[{index}] requires both start_line and end_line when targeting lines"
                )
            if end_line < start_line:
                raise _EditValidationError(
                    f"edits[{index}].end_line must be greater than or equal to start_line"
                )
            if before_context is not None or after_context is not None:
                raise _EditValidationError(
                    f"edits[{index}] cannot use before_context/after_context with line targeting"
                )
            if replace_all:
                raise _EditValidationError(
                    f"edits[{index}] cannot use replace_all with line targeting"
                )
            normalized_edits.append({
                "index": index,
                "mode": "line_range",
                "new_string": new_string,
                "start_line": start_line,
                "end_line": end_line,
            })
            continue

        # has_insert
        if insert_line is None:
            raise _EditValidationError(
                f"edits[{index}].insert_line is required when targeting insert position"
            )
        if position not in {"before", "after"}:
            raise _EditValidationError(
                f"edits[{index}].position must be exactly 'before' or 'after'"
            )
        if before_context is not None or after_context is not None:
            raise _EditValidationError(
                f"edits[{index}] cannot use before_context/after_context with insert mode"
            )
        if replace_all:
            raise _EditValidationError(
                f"edits[{index}] cannot use replace_all with insert mode"
            )
        if new_string == "":
            raise _EditValidationError(
                f"edits[{index}] insert with empty new_string would be a no-op; remove it"
            )
        normalized_edits.append({
            "index": index,
            "mode": "insert",
            "new_string": new_string,
            "insert_line": insert_line,
            "position": position,
        })

    return normalized_edits


# ─── span resolvers (file-relative, against ORIGINAL content) ────────────


def _line_start_offsets(content: str) -> list[int]:
    """Return character offsets where each 1-indexed line starts.

    ``offsets[i-1]`` is the start of line ``i``. The returned list always
    contains at least one element (offset 0). For files that end with a
    trailing newline, the list does NOT include an extra entry for the
    "phantom" line after the last newline; callers handle EOF explicitly.
    """
    if content == "":
        return [0]
    offsets = [0]
    for index, character in enumerate(content):
        if character == "\n":
            offsets.append(index + 1)
    return offsets


def _find_text_target_spans(
    content: str,
    old_string: str,
    before_context: str | None,
    after_context: str | None,
    replace_all: bool,
) -> list[tuple[int, str]]:
    """Locate text-mode targets, returning ``[(start_index, actual_old_string), ...]``.

    * Quote normalization (curly → straight) is applied to both sides so the
      match succeeds even when the file uses typographic quotes.
    * CRLF fallback: if the file has Windows line endings but the model
      emitted LF, the search strings are transparently re-encoded.
    * ``before_context`` / ``after_context`` filter candidates to the ones
      whose immediate neighbours match.
    * ``replace_all=False`` requires exactly one final match (raises on
      multi-match). ``replace_all=True`` returns every surviving candidate.
    """
    # CRLF re-encoding so multi-line searches work on Windows files.
    if "\r\n" in content and "\r\n" not in old_string:
        old_string = _match_line_endings(content, old_string)
        if before_context is not None:
            before_context = _match_line_endings(content, before_context)
        if after_context is not None:
            after_context = _match_line_endings(content, after_context)

    normalized_content = _normalize_quotes(content)
    normalized_old_string = _normalize_quotes(old_string)
    normalized_before = _normalize_quotes(before_context) if before_context is not None else None
    normalized_after = _normalize_quotes(after_context) if after_context is not None else None

    matches: list[int] = []
    search_start = 0
    while True:
        found_index = normalized_content.find(normalized_old_string, search_start)
        if found_index == -1:
            break
        search_end = found_index + len(normalized_old_string)
        if normalized_before is not None:
            before_start = found_index - len(normalized_before)
            if before_start < 0 or normalized_content[before_start:found_index] != normalized_before:
                search_start = found_index + 1
                continue
        if normalized_after is not None:
            after_end = search_end + len(normalized_after)
            if normalized_content[search_end:after_end] != normalized_after:
                search_start = found_index + 1
                continue
        matches.append(found_index)
        search_start = found_index + 1

    if not matches:
        context_hint = ""
        if before_context is not None or after_context is not None:
            context_hint = " with the provided surrounding context"
        raise _EditValidationError(f"Target text was not found{context_hint}.")

    if not replace_all and len(matches) > 1:
        raise _EditValidationError(
            "Target text matched multiple locations. Pass replace_all=true to change "
            "every occurrence, or provide before_context/after_context (or use line targeting) "
            "to disambiguate."
        )

    spans: list[tuple[int, str]] = []
    for start_index in matches:
        actual_old_string = content[start_index:start_index + len(old_string)]
        spans.append((start_index, actual_old_string))
    return spans


def _resolve_line_range(
    content: str, start_line: int, end_line: int
) -> tuple[int, int]:
    """Resolve a line-range edit to absolute (start, end) offsets in ``content``."""
    offsets = _line_start_offsets(content)
    total_lines = _count_lines(content)
    if total_lines == 0:
        if start_line == 1 and end_line == 1:
            return 0, 0
        raise _EditValidationError(
            "Cannot target lines in an empty file unless start_line=end_line=1"
        )
    if end_line > total_lines:
        raise _EditValidationError(
            f"Line range {start_line}-{end_line} is outside the file (total lines: {total_lines})"
        )
    start_index = offsets[start_line - 1]
    end_index = offsets[end_line] if end_line < len(offsets) else len(content)
    return start_index, end_index


def _resolve_insert(content: str, insert_line: int, position: str) -> int:
    """Resolve an insert edit to a single anchor offset (zero-width span)."""
    total_lines = _count_lines(content)
    offsets = _line_start_offsets(content)

    if total_lines == 0:
        # Empty file: only insert_line == 1 (any position) is meaningful.
        if insert_line == 1:
            return 0
        raise _EditValidationError(
            f"Cannot insert at line {insert_line} in an empty file (use insert_line=1)"
        )

    if position == "before":
        # before line N: anchor at start of line N.
        # insert_line == total_lines + 1 with "before" means "before EOF"
        # (i.e. append after all existing lines) — we accept that as a
        # convenience alias for ``insert_line=total_lines, position="after"``.
        if insert_line < 1 or insert_line > total_lines + 1:
            raise _EditValidationError(
                f"insert_line={insert_line} is out of range "
                f"(file has {total_lines} lines; valid 'before' range is 1..{total_lines + 1})"
            )
        if insert_line == total_lines + 1:
            return len(content)
        return offsets[insert_line - 1]

    # position == "after"
    if insert_line < 1 or insert_line > total_lines:
        raise _EditValidationError(
            f"insert_line={insert_line} is out of range "
            f"(file has {total_lines} lines; valid 'after' range is 1..{total_lines})"
        )
    if insert_line == total_lines:
        return len(content)
    return offsets[insert_line]


def _prepare_insert_replacement(content: str, anchor: int, prepared_text: str) -> str:
    """Make ``prepared_text`` safe to splice at ``anchor`` (zero-width).

    Two surgical guarantees enforced here:

    1. The inserted block ALWAYS ends with the file's line separator. For
       mid-file inserts that prevents fusing with the line below; for
       appends at EOF (or inserts into an empty file) it preserves the
       POSIX convention of newline-terminated text files so subsequent
       line-range edits still see well-formed lines.
    2. When appending at EOF on a file that does NOT end with a newline,
       prepend the file's separator so the previous content's last line
       and the inserted content stay on separate lines.
    """
    text = _match_line_endings(content, prepared_text)
    sep = "\r\n" if "\r\n" in content else "\n"

    if not text.endswith(("\n",)):
        text = text + sep

    inserting_at_eof = anchor == len(content)
    if inserting_at_eof and content and not content.endswith(("\n",)):
        text = sep + text

    return text


# ─── per-file engine ─────────────────────────────────────────────────────


# ─── SoT line-number prefix stripping ────────────────────────────────────
# The Source of Truth renders file contents with line numbers like `   123|<content>`
# (see message_builder.build_sot_user_message). When the model copies content
# from the SoT into edit_files old_string/new_string/before_context/after_context,
# it often includes the leading `   123|` prefix. The actual file on disk does
# NOT have this prefix, so the edit fails to match.
#
# This helper strips the prefix per-line, but ONLY when the prefix is at the
# START of a line and matches the exact format `[ \t]*\d+\|`. Patterns like `|256|`
# that appear mid-line (e.g., in CSV files or documents) are NOT stripped —
# they're treated as content, not line markers.
#
# Conservative: if the first line doesn't have a prefix, we don't strip anything
# (the model intentionally omitted line markers). If subsequent lines don't have
# a prefix, we leave them as-is (mixed markers + content is preserved).
_LINE_NUMBER_PREFIX_RE = re.compile(r'^[ \t]*\d+\|')


def _strip_line_number_prefix(text: str) -> str | None:
    """Strip SoT line-number prefixes from text. Returns None if no prefix detected.

    Only strips prefixes that match `^[ \t]*\d+\|` at the START of each line.
    Mid-line patterns like `|256|` are left untouched (they're content).
    """
    if not text:
        return None

    lines = text.split('\n')
    if not lines:
        return None

    # First line must have a prefix — otherwise the model intentionally
    # omitted line markers and we should not strip anything.
    if not _LINE_NUMBER_PREFIX_RE.match(lines[0]):
        return None

    # Strip per-line: each line independently checks for a prefix.
    stripped_lines = []
    for line in lines:
        match = _LINE_NUMBER_PREFIX_RE.match(line)
        if match:
            stripped_lines.append(line[match.end():])
        else:
            stripped_lines.append(line)

    return '\n'.join(stripped_lines)




def _apply_edits_to_one_file(arguments: dict[str, Any], root_dir: Path) -> dict[str, Any]:
    """Apply one or more atomic edits to a single text file.

    This is the per-file engine; the public entry point is
    :func:`execute_edit_files`, which loops over the ``files`` array and
    invokes this function once per entry, isolating per-file failures.

    Returns a dict with ``status``, ``operation`` ("create" or "update"),
    ``edit_count`` (number of input edits), ``line_count``, ``size_bytes``,
    and ``applied_edits`` (one entry per input edit, in input order).
    """
    raw_path = _require_string(arguments, "path")
    path = resolve_path(raw_path, root_dir)
    edits = _require_edits(arguments)

    # ── SoT line-number prefix stripping ──
    # If the model copied content from the SoT (which renders files with
    # `   123|<content>` line markers), strip those markers before matching.
    # See _strip_line_number_prefix for the conservative per-line rules.
    for edit in edits:
        if edit["mode"] == "text":
            for field in ("old_string", "new_string", "before_context", "after_context"):
                value = edit.get(field)
                if isinstance(value, str) and value:
                    stripped = _strip_line_number_prefix(value)
                    if stripped is not None:
                        edit[field] = stripped
        else:
            # line_range and insert modes only have new_string as content
            new_string = edit.get("new_string")
            if isinstance(new_string, str) and new_string:
                stripped = _strip_line_number_prefix(new_string)
                if stripped is not None:
                    edit["new_string"] = stripped


    # ── File-creation special case ──
    # Single text-mode edit with empty old_string against a missing file →
    # create the file with new_string. This is the only situation where an
    # empty old_string is allowed.
    if not path.exists():
        only_edit = edits[0] if len(edits) == 1 else None
        if (
            only_edit is not None
            and only_edit["mode"] == "text"
            and only_edit["old_string"] == ""
            and not only_edit["replace_all"]
            and only_edit["before_context"] is None
            and only_edit["after_context"] is None
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            content_to_write = _prepare_replacement_text(path, only_edit["new_string"])
            with path.open("w", encoding="utf-8", newline="") as fh:
                fh.write(content_to_write)
            return {
                "path": str(path),
                "status": "success",
                "operation": "create",
                "edit_count": 1,
                "line_count": _count_lines(content_to_write),
                "size_bytes": path.stat().st_size,
                "applied_edits": [{"index": 1, "mode": "create"}],
            }
        raise FileNotFoundError(f"File does not exist: {path}")

    if path.is_dir():
        raise IsADirectoryError(f"Path is a directory, not a file: {path}")

    # Reject edits that should never have been allowed against an EXISTING
    # file (empty old_string is only valid for the create path above).
    for edit in edits:
        if edit["mode"] == "text" and edit["old_string"] == "":
            raise _EditValidationError(
                f"edits[{edit['index']}].old_string must not be empty when the file already exists"
            )

    try:
        # newline="" disables universal-newlines so CRLF files keep their
        # \r\n in memory; otherwise we would silently rewrite the document
        # with LF on save and break Windows line endings.
        with path.open("r", encoding="utf-8", newline="") as fh:
            original_content = fh.read()
    except UnicodeDecodeError as exc:
        raise ValueError(
            f"Cannot edit file as UTF-8 text: {path}. "
            "Use write_file for full replacement or run_command for binary handling."
        ) from exc

    # ──────────────────────────────────────────────────────────────────────
    # Phase 1 — resolve every edit against the ORIGINAL content. This is
    # what makes multi-edit calls safe: positions/offsets that the model
    # emitted (line numbers, search anchors) are evaluated before any
    # splice happens, so a prior edit cannot shift them out of alignment.
    # Each text-mode edit may expand to multiple resolved spans when
    # ``replace_all=True``; line-range and insert always produce one.
    # ──────────────────────────────────────────────────────────────────────
    resolved: list[dict[str, Any]] = []
    for edit in edits:
        prepared_new_string = _prepare_replacement_text(path, edit["new_string"])
        index = edit["index"]

        if edit["mode"] == "text":
            spans = _find_text_target_spans(
                original_content,
                edit["old_string"],
                edit["before_context"],
                edit["after_context"],
                edit["replace_all"],
            )
            for start_index, actual_old_string in spans:
                replacement = _preserve_quote_style(
                    edit["old_string"], actual_old_string, prepared_new_string
                )
                replacement = _match_line_endings(original_content, replacement)
                end_index = start_index + len(actual_old_string)
                # When deleting a span that fully consumes its line — i.e.
                # nothing remained before the match on that line AND the next
                # character is the line's terminator — also absorb the
                # trailing \n so we don't leave a blank line behind. We
                # explicitly require the line to be empty BEFORE the match
                # (line_start..start_index): if there was leading content,
                # the model wanted that content to stay on its own line, and
                # absorbing the \n would silently fuse it with the next line
                # (a corruption that can break syntax in many languages).
                if (
                    replacement == ""
                    and not actual_old_string.endswith("\n")
                    and original_content[end_index:end_index + 1] == "\n"
                ):
                    line_start = original_content.rfind("\n", 0, start_index) + 1
                    if original_content[line_start:start_index] == "":
                        end_index += 1
                if actual_old_string == replacement and end_index == start_index + len(actual_old_string):
                    raise _EditValidationError(
                        f"edits[{index}] would not change the file (old and new are identical)."
                    )
                resolved.append({
                    "index": index,
                    "mode": "text",
                    "start": start_index,
                    "end": end_index,
                    "replacement": replacement,
                    "target_line": original_content.count("\n", 0, start_index) + 1,
                    "match_count": len(spans),
                })
            continue

        if edit["mode"] == "line_range":
            start_index, end_index = _resolve_line_range(
                original_content, edit["start_line"], edit["end_line"]
            )
            replacement = _match_line_endings(original_content, prepared_new_string)
            # Preserve "lineness": if the replaced block was newline-terminated
            # in the ORIGINAL file, the replacement must be too. This covers
            # both cases — replacing a middle block (otherwise the next line
            # would fuse) and replacing the last line of a file that DOES
            # end with a newline (otherwise we'd silently strip the file's
            # trailing newline). For the rare case of replacing the last
            # line of a file that lacks a trailing newline, the original
            # block does not end with "\n", so we leave the replacement as-is.
            replaced_block = original_content[start_index:end_index]
            if (
                replacement
                and not replacement.endswith(("\n",))
                and replaced_block.endswith(("\n",))
            ):
                sep = "\r\n" if "\r\n" in original_content else "\n"
                replacement = replacement + sep
            resolved.append({
                "index": index,
                "mode": "line_range",
                "start": start_index,
                "end": end_index,
                "replacement": replacement,
                "start_line": edit["start_line"],
                "end_line": edit["end_line"],
            })
            continue

        # insert mode
        anchor = _resolve_insert(original_content, edit["insert_line"], edit["position"])
        replacement = _prepare_insert_replacement(original_content, anchor, prepared_new_string)
        resolved.append({
            "index": index,
            "mode": "insert",
            "start": anchor,
            "end": anchor,
            "replacement": replacement,
            "insert_line": edit["insert_line"],
            "position": edit["position"],
        })

    # ──────────────────────────────────────────────────────────────────────
    # Phase 2 — overlap detection. Strict overlap (curr.start < prev.end)
    # is rejected. Boundary contact (curr.start == prev.end) is rejected
    # only when at least one of the two spans is zero-width (insert
    # touching another edit), because then the apply order would be
    # ambiguous. Two adjacent non-zero ranges that simply share a boundary
    # are fine — the descending-apply order resolves them deterministically.
    # ──────────────────────────────────────────────────────────────────────
    overlap_check = sorted(resolved, key=lambda r: (r["start"], r["end"]))
    for i in range(1, len(overlap_check)):
        prev = overlap_check[i - 1]
        curr = overlap_check[i]
        if curr["start"] < prev["end"]:
            raise _EditValidationError(
                f"edits[{prev['index']}] and edits[{curr['index']}] overlap in the original "
                f"file. Split them into separate calls or merge them into one edit."
            )
        if curr["start"] == prev["end"]:
            prev_zw = prev["start"] == prev["end"]
            curr_zw = curr["start"] == curr["end"]
            if prev_zw or curr_zw:
                raise _EditValidationError(
                    f"edits[{prev['index']}] and edits[{curr['index']}] touch at the same "
                    f"position and at least one is an insert. Merge them into a single edit "
                    f"with the combined new_string to make the order explicit."
                )

    # ──────────────────────────────────────────────────────────────────────
    # Phase 3 — apply in descending offset order. Equal starts only happen
    # for two zero-width inserts at different positions (already guarded
    # above against same position), so a stable sort by ``start`` alone is
    # enough; we add ``end`` as a secondary key for determinism.
    # ──────────────────────────────────────────────────────────────────────
    updated_content = original_content
    for span in sorted(resolved, key=lambda r: (r["start"], r["end"]), reverse=True):
        updated_content = updated_content[: span["start"]] + span["replacement"] + updated_content[span["end"]:]

    if updated_content == original_content:
        raise ValueError("Original and edited file are identical. Failed to apply edits.")

    # ──────────────────────────────────────────────────────────────────────
    # Phase 4 — write atomically (single open/write) and build per-edit
    # report in input order. Multiple resolved spans that came from the
    # same input edit (replace_all expansion) are collapsed into a single
    # report row for the caller's clarity.
    # ──────────────────────────────────────────────────────────────────────
    with path.open("w", encoding="utf-8", newline="") as fh:
        fh.write(updated_content)

    applied_edits: list[dict[str, Any]] = []
    seen_indices: set[int] = set()
    # Walk resolved in input order so the report mirrors the request.
    for span in sorted(resolved, key=lambda r: r["index"]):
        if span["index"] in seen_indices:
            continue
        seen_indices.add(span["index"])
        if span["mode"] == "text":
            applied_edits.append({
                "index": span["index"],
                "mode": "text",
                "target_line": span["target_line"],
                "replacements": span.get("match_count", 1),
            })
        elif span["mode"] == "line_range":
            applied_edits.append({
                "index": span["index"],
                "mode": "line_range",
                "start_line": span["start_line"],
                "end_line": span["end_line"],
            })
        else:  # insert
            applied_edits.append({
                "index": span["index"],
                "mode": "insert",
                "insert_line": span["insert_line"],
                "position": span["position"],
            })

    return {
        "path": str(path),
        "status": "success",
        "operation": "update",
        "edit_count": len(edits),
        "line_count": _count_lines(updated_content),
        "size_bytes": path.stat().st_size,
        "applied_edits": applied_edits,
    }


# ─── public entry point: multi-file ──────────────────────────────────────


def execute_edit_files(arguments: dict[str, Any], root_dir: Path) -> dict[str, Any]:
    """Apply edits to one OR many text files in a single batched call.

    Mirrors ``read_files`` / ``read_many_files``-style fan-out: the input
    is a ``files`` array, each entry is ``{path, edits}``. Per-file
    semantics are preserved exactly — within a file, all its edits are
    atomic (all-or-nothing) and follow the three-mode contract documented
    in :func:`_apply_edits_to_one_file`.

    Cross-file behaviour is intentionally per-file independent (the same
    model ``read_files`` uses): one file's failure does NOT roll back
    another file's success. Each file gets its own ``results[i]`` entry
    with ``ok: True`` and the success payload, or ``ok: False`` and an
    ``error`` string. The model can then re-emit only the failing files
    on the next turn, instead of having to repeat the whole batch.

    SoT-update policy lives in ``sot.update_tracked_from_tool_result``:
    only paths that were ALREADY in the tracked set (or that fall under
    a permanently-attached source entry) get refreshed from disk on the
    next turn. Paths the model edited "blindly" (not in SoT) are not
    auto-injected — the tool reports the update but the context stays
    clean, by design.
    """
    files_input = arguments.get("files")
    if not isinstance(files_input, list) or not files_input:
        raise _EditValidationError("files must be a non-empty array")

    results: list[dict[str, Any]] = []
    for index, file_entry in enumerate(files_input, start=1):
        if not isinstance(file_entry, dict):
            results.append({
                "ok": False,
                "path": None,
                "error": f"files[{index}] must be an object with 'path' and 'edits'",
            })
            continue

        raw_path = file_entry.get("path")
        # We capture the raw path up front so even validation-stage
        # failures still report which file failed.
        path_for_report = raw_path if isinstance(raw_path, str) else None

        try:
            file_args = {"path": raw_path, "edits": file_entry.get("edits")}
            file_result = _apply_edits_to_one_file(file_args, root_dir)
            file_result["ok"] = True
            results.append(file_result)
        except _EditValidationError as exc:
            results.append({
                "ok": False,
                "path": path_for_report,
                "error": str(exc),
            })
        except FileNotFoundError as exc:
            results.append({
                "ok": False,
                "path": path_for_report,
                "error": f"FileNotFoundError: {exc}",
            })
        except IsADirectoryError as exc:
            results.append({
                "ok": False,
                "path": path_for_report,
                "error": f"IsADirectoryError: {exc}",
            })
        except (UnicodeDecodeError, OSError, ValueError) as exc:
            results.append({
                "ok": False,
                "path": path_for_report,
                "error": f"{type(exc).__name__}: {exc}",
            })

    succeeded = sum(1 for r in results if r.get("ok"))
    failed = len(results) - succeeded

    return {
        "results": results,
        "summary": {
            "total": len(results),
            "succeeded": succeeded,
            "failed": failed,
        },
    }
