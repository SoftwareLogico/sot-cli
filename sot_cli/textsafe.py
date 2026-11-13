"""Utilidades para neutralizar surrogates solitarios.

En Windows, pegar texto en la consola puede producir surrogates sueltos
(\\udc80..\\udcff via surrogateescape).  Un str con surrogates NO se puede
codificar a UTF-8, y revienta cualquier json.dumps()+write_text() o el
cuerpo JSON que httpx construye para enviar la peticion.
"""

from __future__ import annotations

from typing import Any

_SURROGATE_MIN = 0xD800
_SURROGATE_MAX = 0xDFFF


def _console_encoding() -> str:
    """Code page de entrada de la consola (cp850, cp1252, ...)."""
    try:
        import ctypes

        cp = int(ctypes.windll.kernel32.GetConsoleCP())  # type: ignore[attr-defined]
    except Exception:
        return "utf-8"
    if cp in (0, 65001):
        return "utf-8"
    return f"cp{cp}"


def repair_surrogates(text: str) -> str:
    """Devuelve *text* sin surrogates.

    Los bytes recuperados de un surrogateescape (\\udc80-\\udcff) se
    reinterpretan con la code page de la consola; el resto de surrogates se
    sustituye por U+FFFD.  Si no hay surrogates devuelve el string tal cual.
    """
    if not any(_SURROGATE_MIN <= ord(ch) <= _SURROGATE_MAX for ch in text):
        return text

    enc = _console_encoding()
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        cp = ord(ch)
        # Par surrogate valido (CESU-8): combinar en el caracter real.
        if 0xD800 <= cp <= 0xDBFF and i + 1 < n:
            nxt = ord(text[i + 1])
            if 0xDC00 <= nxt <= 0xDFFF:
                out.append(chr(0x10000 + ((cp - 0xD800) << 10) + (nxt - 0xDC00)))
                i += 2
                continue
        if 0xDC80 <= cp <= 0xDCFF:
            raw = bytes([cp - 0xDC00])
            for candidate in (enc, "cp850", "cp1252"):
                try:
                    out.append(raw.decode(candidate))
                    break
                except (LookupError, UnicodeDecodeError):
                    continue
            else:
                out.append("\ufffd")
        elif _SURROGATE_MIN <= cp <= _SURROGATE_MAX:
            out.append("\ufffd")
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def sanitize_json(value: Any) -> Any:
    """Recorre dict/list/tuple y repara cualquier str con surrogates."""
    if isinstance(value, str):
        return repair_surrogates(value)
    if isinstance(value, dict):
        return {sanitize_json(k): sanitize_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_json(v) for v in value]
    return value
