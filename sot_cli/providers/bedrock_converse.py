from __future__ import annotations

import asyncio
import base64
import json
import os
import queue
import re
import threading
from typing import Any, AsyncIterator

from sot_cli.providers.base import (
    ProviderCapability,
    ProviderCompletion,
    ProviderEvent,
    ProviderRequest,
)
from sot_cli.providers.openai_compat import _sanitize_messages_for_provider, _write_session_json


def _clean_name(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", name)[:200]


def _translate_messages_to_converse(sanitized: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Translate OpenAI-format messages to Bedrock Converse format."""
    system_blocks: list[dict[str, Any]] = []
    converse_msgs: list[dict[str, Any]] = []

    for msg in sanitized:
        role = msg.get("role")
        if role == "system":
            content = msg.get("content")
            if content:
                system_blocks.append({"text": str(content)})
            continue

        if role == "tool":
            content = msg.get("content", "")
            tc_id = msg.get("tool_call_id", "")
            converse_msgs.append({
                "role": "user",
                "content": [{
                    "toolResult": {
                        "toolUseId": tc_id,
                        "content": [{"text": str(content)}],
                    }
                }],
            })
            continue

        content = msg.get("content")
        blocks: list[dict[str, Any]] = []

        if isinstance(content, str) and content:
            blocks.append({"text": content})
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                ptype = part.get("type")
                if ptype == "text":
                    blocks.append({"text": str(part.get("text", ""))})
                elif ptype == "image_url":
                    url = part.get("image_url", {}).get("url", "")
                    if ";" in url and "base64," in url:
                        mime_part, b64data = url.split(";", 1)
                        b64data = b64data.replace("base64,", "")
                        fmt = mime_part.split("/")[1]
                        if fmt == "jpg":
                            fmt = "jpeg"
                        blocks.append({
                            "image": {
                                "format": fmt,
                                "source": {"bytes": base64.b64decode(b64data)},
                            }
                        })
                elif ptype == "file":
                    file_data = part.get("file", {}).get("file_data", "")
                    if ";" in file_data and "base64," in file_data:
                        mime = file_data.split(";")[0].replace("data:", "")
                        b64 = file_data.split("base64,")[1]
                        fname = part.get("file", {}).get("filename", "document")
                        if "pdf" in mime:
                            blocks.append({
                                "document": {
                                    "format": "pdf",
                                    "name": _clean_name(fname.split(".")[0]),
                                    "source": {"bytes": base64.b64decode(b64)},
                                }
                            })
                        else:
                            blocks.append({"text": base64.b64decode(b64).decode("utf-8", errors="replace")})

        tool_calls = msg.get("tool_calls")
        if tool_calls:
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                func = tc.get("function", {})
                args_raw = func.get("arguments", "{}")
                blocks.append({
                    "toolUse": {
                        "toolUseId": tc.get("id", ""),
                        "name": func.get("name", ""),
                        "input": json.loads(args_raw) if args_raw else {},
                    }
                })

        if blocks:
            converse_msgs.append({"role": role, "content": blocks})

    # Merge consecutive identical roles (required by Bedrock)
    merged: list[dict[str, Any]] = []
    for m in converse_msgs:
        if merged and merged[-1]["role"] == m["role"]:
            merged[-1]["content"].extend(m["content"])
        else:
            merged.append(m)

    return system_blocks, merged


def _translate_tools_to_converse(openai_tools: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not openai_tools:
        return None
    specs = []
    for t in openai_tools:
        func = t.get("function", {})
        params = func.get("parameters", {})
        if isinstance(params, dict):
            params = dict(params)
            for forbidden in {"oneOf", "anyOf", "allOf", "not", "enum"}:
                params.pop(forbidden, None)
        specs.append({
            "toolSpec": {
                "name": func.get("name", ""),
                "description": func.get("description", ""),
                "inputSchema": {"json": params},
            }
        })
    return {"tools": specs}


def _infer_context_length(model: str) -> int:
    m = model.lower()
    if "nova" in m:
        return 300_000
    if "claude" in m:
        return 200_000
    if "llama" in m or "deepseek" in m or "mistral" in m or "mixtral" in m or "qwen" in m:
        return 128_000
    if "gemma" in m:
        return 128_000
    if "glm" in m or "zai" in m:
        return 256_000
    return 256_000


class BedrockConverseAdapter:
    """Translates OpenAI-format requests to Bedrock Converse API and back.

    This adapter wraps the AWS Bedrock Converse / ConverseStream API so the
    rest of sot-cli (query.py, cli.py, etc.) never needs to know it is talking
    to a non-OpenAI backend.  The same ProviderRequest / ProviderEvent /
    ProviderCompletion types are used on both sides of the translation layer.
    """

    def __init__(
        self,
        name: str,
        model: str,
        region: str,
        api_key: str | None,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.name = name
        self.model = model
        self.region = region
        self.api_key = api_key
        self.extra_headers = extra_headers or {}
        self.capability = ProviderCapability()
        self._capabilities_detected = False

    async def detect_capabilities(self) -> None:
        """Detect model capabilities via boto3 Bedrock control plane."""
        if self._capabilities_detected:
            return

        def _fetch() -> dict[str, Any]:
            try:
                import boto3
            except ImportError:
                raise RuntimeError("boto3 is required for Amazon Bedrock. Run 'pip install boto3'")

            if self.api_key:
                os.environ["AWS_BEARER_TOKEN_BEDROCK"] = self.api_key

            client = boto3.client("bedrock", region_name=self.region)
            return client.get_foundation_model(modelIdentifier=self.model)

        try:
            response = await asyncio.to_thread(_fetch)
            details = response.get("modelDetails", {})
            input_mods: list[str] = details.get("inputModalities", []) or []
            supports_images = "IMAGE" in input_mods
            self.capability = ProviderCapability(
                supports_tools=True,
                supports_images=supports_images,
                supports_pdfs=supports_images,
                context_length=_infer_context_length(self.model),
            )
        except Exception:
            # Fallback: optimistic defaults
            self.capability = ProviderCapability(
                supports_tools=True,
                supports_images=True,
                supports_pdfs=True,
                context_length=256_000,
            )
        self._capabilities_detected = True

    def _get_runtime_client(self):
        try:
            import boto3
        except ImportError:
            raise RuntimeError("boto3 is required for Amazon Bedrock. Run 'pip install boto3'")

        if self.api_key:
            os.environ["AWS_BEARER_TOKEN_BEDROCK"] = self.api_key

        return boto3.client("bedrock-runtime", region_name=self.region)

    def _build_converse_kwargs(self, request: ProviderRequest, stream: bool = True) -> dict[str, Any]:
        sanitized = _sanitize_messages_for_provider(
            request.conversation_messages or [],
            request.compression_reasoning_trunc_chars,
        )
        system_blocks, converse_messages = _translate_messages_to_converse(sanitized)
        tool_config = _translate_tools_to_converse(request.tools) if request.enable_tools and request.tools else None

        kwargs: dict[str, Any] = {
            "modelId": self.model or request.model,
            "messages": converse_messages,
            "inferenceConfig": {
                "temperature": request.temperature,
                "maxTokens": request.max_output_tokens,
            },
        }
        if system_blocks:
            kwargs["system"] = system_blocks
        if tool_config:
            kwargs["toolConfig"] = tool_config

        # Reasoning effort → additionalModelRequestFields
        # Format varies by model family:
        # - Claude: thinking.type enabled + budget_tokens + temperature=1.0
        # - Kimi/GLM: reasoning_config high
        if request.reasoning_effort:
            model_lower = (self.model or request.model).lower()
            if "claude" in model_lower:
                kwargs["additionalModelRequestFields"] = {
                    "thinking": {
                        "type": "enabled",
                        "budget_tokens": max(1024, min(request.max_output_tokens, 32000)),
                    }
                }
                # Claude requires temperature=1.0 when thinking is enabled
                kwargs["inferenceConfig"]["temperature"] = 1.0
            elif "kimi" in model_lower or "glm" in model_lower or "zai" in model_lower:
                kwargs["additionalModelRequestFields"] = {
                    "reasoning_config": "high"
                }
            else:
                # Default: try thinking format (works for most)
                kwargs["additionalModelRequestFields"] = {
                    "thinking": {
                        "type": "enabled",
                        "budget_tokens": max(1024, min(request.max_output_tokens, 32000)),
                    }
                }

        return kwargs

    def _run_converse(
        self,
        kwargs: dict[str, Any],
        session_id: str,
        stream: bool = True,
    ) -> tuple[queue.Queue, threading.Thread]:
        """Start a Converse API call in a background thread. Returns (queue, thread)."""
        client = self._get_runtime_client()
        q: queue.Queue = queue.Queue()

        def _worker() -> None:
            try:
                if stream:
                    response = client.converse_stream(**kwargs)
                    for event in response["stream"]:
                        q.put(("event", event))
                else:
                    result = client.converse(**kwargs)
                    q.put(("result", result))
                q.put(("done", None))
            except Exception as e:
                q.put(("error", e))

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        return q, t

    def _is_reasoning_error(self, error: Exception) -> bool:
        err_str = str(error).lower()
        return "reasoning_effort" in err_str or "reasoning_config" in err_str or "additionalModelRequestFields" in err_str

    async def stream_turn(self, request: ProviderRequest) -> AsyncIterator[ProviderEvent]:
        # Save the OpenAI-format payload for round-tripping (NOT Converse format)
        _write_session_json(
            "request",
            {
                "url": f"bedrock:converse_stream ({self.region})",
                "payload": {
                    "model": self.model or request.model,
                    "messages": request.conversation_messages,
                    "temperature": request.temperature,
                    "max_tokens": request.max_output_tokens,
                    "stream": True,
                },
            },
            session_id=request.session_id,
        )

        kwargs = self._build_converse_kwargs(request, stream=True)

        # Save the raw Converse kwargs as payload.json for debugging
        _write_session_json("payload", kwargs, session_id=request.session_id)

        client = self._get_runtime_client()
        q: queue.Queue = queue.Queue()
        attempt = 0
        max_attempts = 2

        def _worker(kw: dict[str, Any]) -> None:
            try:
                response = client.converse_stream(**kw)
                for event in response["stream"]:
                    q.put(("event", event))
                q.put(("done", None))
            except Exception as e:
                q.put(("error", e))

        t = threading.Thread(target=_worker, args=(kwargs,), daemon=True)
        t.start()

        raw_chunks: list[dict[str, Any]] = []
        while True:
            try:
                item_type, item = await asyncio.to_thread(q.get, timeout=0.1)
            except queue.Empty:
                if not t.is_alive():
                    break
                continue

            if item_type == "error":
                _write_session_json("error", {"error": str(item)}, session_id=request.session_id)
                raise RuntimeError(f"Amazon Bedrock Converse Error: {item}")
            if item_type == "done":
                break

            raw_chunks.append(item)

            if "contentBlockStart" in item:
                start = item["contentBlockStart"]
                idx = start["contentBlockIndex"]
                if "toolUse" in start.get("start", {}):
                    tool = start["start"]["toolUse"]
                    tc = {
                        "index": idx,
                        "id": tool["toolUseId"],
                        "type": "function",
                        "function": {"name": tool["name"], "arguments": ""},
                    }
                    yield ProviderEvent(type="tool_call", payload={"tool_calls": [tc]})

            elif "contentBlockDelta" in item:
                delta = item["contentBlockDelta"]
                idx = delta["contentBlockIndex"]
                d = delta.get("delta", {})
                if "text" in d:
                    yield ProviderEvent(type="text_delta", payload={"text": d["text"]})
                elif "reasoningContent" in d:
                    rc = d["reasoningContent"]
                    rc_text = rc.get("text", "") if isinstance(rc, dict) else str(rc)
                    if rc_text:
                        yield ProviderEvent(type="reasoning_delta", payload={"text": rc_text})
                elif "toolUse" in d:
                    args = d["toolUse"]["input"]
                    yield ProviderEvent(
                        type="tool_call",
                        payload={"tool_calls": [{"index": idx, "function": {"arguments": args}}]},
                    )

            elif "metadata" in item:
                usage = item["metadata"].get("usage", {})
                if usage:
                    mapped = {
                        "prompt_tokens": usage.get("inputTokens", 0),
                        "completion_tokens": usage.get("outputTokens", 0),
                        "total_tokens": usage.get("totalTokens", 0),
                    }
                    yield ProviderEvent(type="usage", payload={"usage": mapped})

            elif "messageStop" in item:
                reason = item["messageStop"].get("stopReason", "")
                if reason == "max_tokens":
                    reason = "length"
                elif reason == "tool_use":
                    reason = "tool_calls"
                yield ProviderEvent(type="finished", payload={"finish_reason": reason})

        if raw_chunks:
            _write_session_json("response-chunks", raw_chunks, session_id=request.session_id)
        yield ProviderEvent(type="done")

    async def complete_turn(self, request: ProviderRequest) -> ProviderCompletion:
        # Save the OpenAI-format payload for round-tripping (NOT Converse format)
        _write_session_json(
            "request",
            {
                "url": f"bedrock:converse ({self.region})",
                "payload": {
                    "model": self.model or request.model,
                    "messages": request.conversation_messages,
                    "temperature": request.temperature,
                    "max_tokens": request.max_output_tokens,
                    "stream": False,
                },
            },
            session_id=request.session_id,
        )

        kwargs = self._build_converse_kwargs(request, stream=False)

        # Save the raw Converse kwargs as payload.json for debugging
        _write_session_json("payload", kwargs, session_id=request.session_id)

        client = self._get_runtime_client()

        try:
            response = await asyncio.to_thread(client.converse, **kwargs)
        except Exception as e:
            _write_session_json("error", {"error": str(e)}, session_id=request.session_id)
            raise RuntimeError(f"Amazon Bedrock Converse Error: {e}")

        _write_session_json("response", response, session_id=request.session_id)

        msg = response.get("output", {}).get("message", {})
        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []

        for block in msg.get("content", []):
            if "text" in block:
                text_parts.append(block["text"])
            elif "toolUse" in block:
                tu = block["toolUse"]
                tool_calls.append({
                    "id": tu["toolUseId"],
                    "type": "function",
                    "function": {
                        "name": tu["name"],
                        "arguments": json.dumps(tu["input"]),
                    },
                })

        usage = response.get("usage", {})
        mapped_usage = {
            "prompt_tokens": usage.get("inputTokens", 0),
            "completion_tokens": usage.get("outputTokens", 0),
            "total_tokens": usage.get("totalTokens", 0),
        }

        return ProviderCompletion(
            assistant_message={
                "role": "assistant",
                "content": "".join(text_parts),
                "tool_calls": tool_calls,
            },
            text="".join(text_parts),
            tool_calls=tool_calls,
            usage=mapped_usage,
        )
