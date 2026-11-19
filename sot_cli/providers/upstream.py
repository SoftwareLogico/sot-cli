"""Identify WHICH upstream sits behind the open `openai` alias.

`openai` is not a provider — it is an alias for "any OpenAI-compatible
endpoint". Each real upstream has its own reasoning dialect and its own
accepted level set, and several of them answer HTTP 400 to anything outside
it (Lyber rejects every unknown field; LM Studio rejects unknown effort
values). Forcing one dialect therefore breaks the others.

Guessing by trial is not enough on its own: a tolerant server answers 200 to
a shape it does not actually implement and silently drops the parameter, so
"the first one that works" leaves the user with reasoning switched off while
everything looks fine.

So detection is layered, and the acceptance test is SEMANTIC, not just a
status code:

1. explicit override  (``SOT_UPSTREAM_DIALECT`` env)
2. persisted verdict  (``<data_dir>/upstream_dialects.json``, keyed by
   base_url + model, TTL-limited)
3. structural fingerprint (host, then response headers, then server paths
   such as ``/props`` — llama.cpp marker)
4. one-shot wire probe, in safest-first order, accepted only when the reply
   carries actual reasoning; a 200 without reasoning is recorded as
   ``ignored`` so the banner can say the endpoint accepts but discards it
5. ``generic`` fallback with a loud warning — never a silent wrong guess

Every verdict is persisted with its evidence so a later run costs zero
requests. A different base_url (or model) is a different key, so pointing the
alias somewhere else re-detects from scratch instead of inheriting the old
verdict.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

import httpx

# Dialects this module can return. The four fixed providers are listed so a
# mis-pointed alias can be recognized as one of them and inherit its exact
# scale; `generic` is the last-resort safe profile.
DIALECTS: tuple[str, ...] = (
    "lyber",
    "nanogpt",
    "strata",
    "heabsy",
    "openrouter",
    "lmstudio",
    "nvidia",
    "bedrock",
    "openai",
    "generic",
)

# Host suffix → dialect. Authoritative and free (no request). Order matters:
# more specific hosts first.
_HOST_SIGNATURES: tuple[tuple[str, str], ...] = (
    ("nano-gpt.com", "nanogpt"),
    ("lyber.ai", "lyber"),
    ("heabsy.com", "heabsy"),
    ("openrouter.ai", "openrouter"),
    ("integrate.api.nvidia.com", "nvidia"),
    ("bedrock-mantle", "bedrock"),
    ("api.openai.com", "openai"),
    ("localhost:1234", "lmstudio"),
    ("127.0.0.1:1234", "lmstudio"),
    ("localhost:11434", "ollama"),
    ("127.0.0.1:11434", "ollama"),
)

# Response-header fingerprints, checked in order.
_HEADER_SIGNATURES: tuple[tuple[str, str, str], ...] = (
    # (header name, value substring ("" = any), dialect)
    ("x-lyber-admission", "", "lyber"),
    ("x-lyber-request-id", "", "lyber"),
    ("x-nanogpt-pricing", "", "nanogpt"),
    ("x-nanogpt-request-id", "", "nanogpt"),
    ("x-heabsy-request-id", "", "heabsy"),
    ("x-openrouter-", "", "openrouter"),
)

# Probe candidates, safest first. `shape` is what gets merged into the body.
_PROBE_ORDER: tuple[tuple[str, dict[str, Any]], ...] = (
    ("generic", {"reasoning_effort": "high"}),
    ("openrouter", {"reasoning": {"effort": "high"}}),
)

_CACHE_TTL_SECONDS = 14 * 24 * 3600
_PROBE_TIMEOUT = 30.0


def _cache_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / "upstream_dialects.json"


def _key(base_url: str, model: str) -> str:
    return hashlib.sha1(f"{base_url.rstrip('/')}|{model}".encode()).hexdigest()[:16]


def load_cached(data_dir: str | Path, base_url: str, model: str) -> dict[str, Any] | None:
    """Return a fresh cached verdict for this exact endpoint+model, else None."""
    path = _cache_path(data_dir)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    entry = raw.get(_key(base_url, model))
    if not isinstance(entry, dict):
        return None
    if time.time() - float(entry.get("verified_at", 0)) > _CACHE_TTL_SECONDS:
        return None
    dialect = entry.get("dialect")
    if dialect not in DIALECTS:
        return None
    return entry


def store_cached(
    data_dir: str | Path,
    base_url: str,
    model: str,
    dialect: str,
    evidence: str,
    ignored: bool,
) -> None:
    path = _cache_path(data_dir)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raw = {}
    except (OSError, ValueError):
        raw = {}
    raw[_key(base_url, model)] = {
        "base_url": base_url,
        "model": model,
        "dialect": dialect,
        "evidence": evidence,
        "ignored": ignored,
        "verified_at": time.time(),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(raw, indent=2, sort_keys=True), encoding="utf-8")
    except OSError:
        pass


def from_host(base_url: str) -> tuple[str, str] | None:
    """(dialect, evidence) when the host itself identifies the upstream."""
    lowered = base_url.lower()
    for needle, dialect in _HOST_SIGNATURES:
        if needle in lowered:
            return dialect, f"host:{needle}"
    return None


def from_model_slug(model: str) -> tuple[str, str] | None:
    """Weak model-id shape heuristics, used only when nothing else decides."""
    m = (model or "").lower()
    if not m:
        return None
    # NanoGPT publishes `vendor/model` slugs, often with a `:free` suffix.
    if ":" in m and m.rsplit(":", 1)[-1] in {"free", "extended", "thinking"}:
        return "nanogpt", "model-suffix"
    # Lyber publishes flat hyphenated ids with no vendor prefix.
    if "/" not in m and m.count("-") >= 2:
        return "lyber", "model-slug"
    if "/" in m:
        return "nanogpt", "model-vendor-slug"
    return None


async def from_endpoint(client: httpx.AsyncClient, origin: str, headers: dict[str, str]) -> tuple[str, str] | None:
    """Structural probe: llama.cpp/Strata exposes /props; headers may identify others."""
    try:
        resp = await client.get(f"{origin}/models", headers=headers)
    except httpx.RequestError:
        return None
    for name, needle, dialect in _HEADER_SIGNATURES:
        value = resp.headers.get(name) or ""
        if needle == "" and value:
            return dialect, f"header:{name}"
        if needle and needle in value.lower():
            return dialect, f"header:{name}"
    try:
        props = await client.get(f"{origin}/props", headers=headers)
        if props.status_code == 200 and isinstance(props.json(), dict):
            body = props.json()
            if "default_generation_settings" in body or "build_info" in body:
                return "strata", "endpoint:/props"
    except (httpx.RequestError, ValueError):
        pass
    return None


def _reasoning_seen(payload: dict[str, Any]) -> bool:
    """True when the reply actually carries reasoning, i.e. the dialect was honored."""
    usage = payload.get("usage") or {}
    if isinstance(usage, dict):
        details = usage.get("completion_tokens_details") or {}
        if isinstance(details, dict) and int(details.get("reasoning_tokens") or 0) > 0:
            return True
        if int(usage.get("reasoning_tokens") or 0) > 0:
            return True
    for choice in payload.get("choices") or []:
        message = (choice or {}).get("message") or {}
        if message.get("reasoning_content") or message.get("reasoning"):
            return True
    return False


def _is_unsupported_error(status: int, body: str) -> bool:
    if status != 400:
        return False
    lowered = body.lower()
    return any(
        token in lowered
        for token in (
            "unsupported_parameter",
            "invalid_value",
            "unknown parameter",
            "unrecognized",
            "extra inputs are not permitted",
        )
    )


async def _probe(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    model: str,
    shape: dict[str, Any],
) -> tuple[str, bool]:
    """Send one tiny probe. Returns (status, honored)."""
    body = {
        "model": model,
        "messages": [{"role": "user", "content": "1"}],
        "max_tokens": 32,
        "stream": False,
        **shape,
    }
    try:
        resp = await client.post(url, headers=headers, json=body)
    except httpx.RequestError as exc:
        return f"transport:{type(exc).__name__}", False
    if resp.status_code != 200:
        return f"http:{resp.status_code}", False
    try:
        data = resp.json()
    except ValueError:
        return "http:200-unparseable", False
    return "http:200", _reasoning_seen(data)


async def resolve(
    base_url: str,
    api_key: str,
    model: str,
    data_dir: str | Path,
    *,
    allow_probe: bool = True,
) -> dict[str, Any]:
    """Resolve the dialect for an `openai` alias target.

    Returns ``{"dialect", "evidence", "ignored", "confidence"}``. Never raises.
    """
    override = (os.environ.get("SOT_UPSTREAM_DIALECT") or "").strip().lower()
    if override in DIALECTS:
        return {"dialect": override, "evidence": "env", "ignored": False, "confidence": "forced"}

    cached = load_cached(data_dir, base_url, model)
    if cached:
        return {
            "dialect": cached["dialect"],
            "evidence": cached.get("evidence", "cache"),
            "ignored": bool(cached.get("ignored")),
            "confidence": "cached",
        }

    host_hit = from_host(base_url)
    if host_hit:
        dialect, evidence = host_hit
        store_cached(data_dir, base_url, model, dialect, evidence, False)
        return {"dialect": dialect, "evidence": evidence, "ignored": False, "confidence": "host"}

    origin = base_url.rstrip("/")
    if origin.endswith("/v1"):
        origin = origin[: -len("/v1")]
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT) as client:
        endpoint_hit = await from_endpoint(client, origin, headers)
        if endpoint_hit:
            dialect, evidence = endpoint_hit
            store_cached(data_dir, base_url, model, dialect, evidence, False)
            return {"dialect": dialect, "evidence": evidence, "ignored": False, "confidence": "endpoint"}

        slug_hit = from_model_slug(model)
        if slug_hit:
            dialect, evidence = slug_hit
            store_cached(data_dir, base_url, model, dialect, evidence, False)
            return {"dialect": dialect, "evidence": evidence, "ignored": False, "confidence": "slug"}

        if not allow_probe or not model:
            return {"dialect": "generic", "evidence": "no-probe", "ignored": False, "confidence": "low"}

        url = f"{base_url.rstrip('/')}/chat/completions"
        last = "http:?"
        for candidate, shape in _PROBE_ORDER:
            status, honored = await _probe(client, url, headers, model, shape)
            last = status
            if status == "http:200":
                if not honored:
                    # Accepted but the reasoning never showed up: record it so the
                    # banner can warn instead of pretending it works.
                    store_cached(data_dir, base_url, model, candidate, f"{status}-ignored", True)
                    return {
                        "dialect": candidate,
                        "evidence": f"{status} (sin reasoning)",
                        "ignored": True,
                        "confidence": "probed",
                    }
                store_cached(data_dir, base_url, model, candidate, status, False)
                return {"dialect": candidate, "evidence": status, "ignored": False, "confidence": "probed"}
            if not _is_unsupported_error(int(status.split(":")[1]), "unsupported_parameter"):
                # Anything that is not a "this parameter is wrong" 400 does not
                # tell us about the dialect; keep the safest profile.
                break

    return {"dialect": "generic", "evidence": last, "ignored": False, "confidence": "low"}


def describe(dialect: str, evidence: str, ignored: bool) -> str:
    """Human line for the startup banner."""
    suffix = " — IGNORA el parámetro" if ignored else ""
    return f"{dialect} ({evidence}){suffix}"
