from __future__ import annotations

from pathlib import Path

LEFT_SINGLE_CURLY_QUOTE = "\u2018"
RIGHT_SINGLE_CURLY_QUOTE = "\u2019"
LEFT_DOUBLE_CURLY_QUOTE = "\u201c"
RIGHT_DOUBLE_CURLY_QUOTE = "\u201d"


def _normalize_quotes(text: str) -> str:
    return (
        text.replace(LEFT_SINGLE_CURLY_QUOTE, "'")
        .replace(RIGHT_SINGLE_CURLY_QUOTE, "'")
        .replace(LEFT_DOUBLE_CURLY_QUOTE, '"')
        .replace(RIGHT_DOUBLE_CURLY_QUOTE, '"')
    )


def _strip_trailing_whitespace(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.split("\n"))


def _match_line_endings(template: str, text: str) -> str:
    """Return ``text`` re-encoded to use the same line-ending style as ``template``.

    Models almost always emit ``\\n`` regardless of the file they are editing,
    so on Windows-encoded files (CRLF) a literal find() against the file would
    miss every multi-line match. This helper converts ``text`` to whatever the
    surrounding file uses so the search succeeds and the edited block keeps
    consistent line endings after replacement.
    """
    if "\r\n" in template and "\r\n" not in text:
        # Normalize any stray CR-only first so we do not create CRCRLF.
        return text.replace("\r\n", "\n").replace("\n", "\r\n")
    return text


def _find_actual_string(file_content: str, search_string: str) -> str | None:
    if file_content.find(search_string) != -1:
        return search_string

    normalized_search = _normalize_quotes(search_string)
    normalized_file = _normalize_quotes(file_content)
    search_index = normalized_file.find(normalized_search)
    if search_index != -1:
        return file_content[search_index:search_index + len(search_string)]

    # CRLF fallback: file uses Windows line endings but the model produced LF.
    if "\r\n" in file_content and "\r\n" not in search_string:
        crlf_search = _match_line_endings(file_content, search_string)
        idx = file_content.find(crlf_search)
        if idx != -1:
            return crlf_search
        normalized_crlf_search = _normalize_quotes(crlf_search)
        idx = normalized_file.find(normalized_crlf_search)
        if idx != -1:
            return file_content[idx:idx + len(crlf_search)]

    return None


def _prepare_replacement_text(path: Path, new_string: str) -> str:
    if path.suffix.lower() in {".md", ".mdx"}:
        return new_string
    return _strip_trailing_whitespace(new_string)


def _is_opening_quote_context(characters: list[str], index: int) -> bool:
    if index == 0:
        return True
    previous = characters[index - 1]
    return previous in {" ", "\t", "\n", "\r", "(", "[", "{", "\u2014", "\u2013"}


def _apply_curly_double_quotes(text: str) -> str:
    characters = list(text)
    result: list[str] = []
    for index, character in enumerate(characters):
        if character == '"':
            result.append(LEFT_DOUBLE_CURLY_QUOTE if _is_opening_quote_context(characters, index) else RIGHT_DOUBLE_CURLY_QUOTE)
        else:
            result.append(character)
    return "".join(result)


def _apply_curly_single_quotes(text: str) -> str:
    characters = list(text)
    result: list[str] = []
    for index, character in enumerate(characters):
        if character != "'":
            result.append(character)
            continue
        previous = characters[index - 1] if index > 0 else None
        following = characters[index + 1] if index < len(characters) - 1 else None
        if isinstance(previous, str) and isinstance(following, str) and previous.isalpha() and following.isalpha():
            result.append(RIGHT_SINGLE_CURLY_QUOTE)
            continue
        result.append(LEFT_SINGLE_CURLY_QUOTE if _is_opening_quote_context(characters, index) else RIGHT_SINGLE_CURLY_QUOTE)
    return "".join(result)


def _preserve_quote_style(old_string: str, actual_old_string: str, new_string: str) -> str:
    if old_string == actual_old_string:
        return new_string

    has_double_quotes = (
        LEFT_DOUBLE_CURLY_QUOTE in actual_old_string
        or RIGHT_DOUBLE_CURLY_QUOTE in actual_old_string
    )
    has_single_quotes = (
        LEFT_SINGLE_CURLY_QUOTE in actual_old_string
        or RIGHT_SINGLE_CURLY_QUOTE in actual_old_string
    )

    result = new_string
    if has_double_quotes:
        result = _apply_curly_double_quotes(result)
    if has_single_quotes:
        result = _apply_curly_single_quotes(result)
    return result


def _apply_edit_to_text(original_content: str, old_string: str, new_string: str, replace_all: bool) -> str:
    if replace_all:
        return original_content.replace(old_string, new_string)
    if new_string != "":
        return original_content.replace(old_string, new_string, 1)

    strip_trailing_newline = not old_string.endswith("\n") and f"{old_string}\n" in original_content
    target = f"{old_string}\n" if strip_trailing_newline else old_string
    return original_content.replace(target, new_string, 1)
