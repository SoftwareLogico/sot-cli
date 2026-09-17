"""
Test script to find the correct format for enabling reasoning/thinking
on Claude via Bedrock Converse API.

Tries multiple additionalModelRequestFields formats until one works.
"""
from __future__ import annotations

import json
import os
import sys
import time
import base64

# ── Config ──────────────────────────────────────────────────────────────
REGION = "us-east-1"
MODEL = "zai.glm-5"
API_KEY = os.environ.get("AWS_BEARER_TOKEN_BEDROCK") or ""

# ── Helpers ──────────────────────────────────────────────────────────────
def _make_client():
    try:
        import boto3
    except ImportError:
        print("ERROR: boto3 not installed. Run: pip install boto3")
        sys.exit(1)

    session = boto3.Session()
    return session.client("bedrock-runtime", region_name=REGION)


def _test_format(client, format_name: str, additional_fields: dict) -> dict:
    """Test a specific additionalModelRequestFields format."""
    print(f"\n{'='*60}")
    print(f"TESTING: {format_name}")
    print(f"{'='*60}")

    kwargs = {
        "modelId": MODEL,
        "messages": [
            {
                "role": "user",
                "content": [{"text": "Solve this step by step: 17 × 24 = ? Think carefully before answering."}]
            }
        ],
        "system": [{"text": "You are Claude. You MUST reason step by step before answering. Show your reasoning."}],
        "inferenceConfig": {
            "maxTokens": 2000,
            "temperature": 0.7,
        },
    }

    if additional_fields:
        kwargs["additionalModelRequestFields"] = additional_fields

    print(f"Payload:\n{json.dumps(kwargs, indent=2, default=str)[:2000]}...")

    try:
        start = time.time()
        response = client.converse(**kwargs)
        elapsed = time.time() - start

        output = response.get("output", {}).get("message", {})
        content_blocks = output.get("content", [])
        text = ""
        for block in content_blocks:
            if "text" in block:
                text += block["text"]

        usage = response.get("usage", {})
        metrics = response.get("metrics", {})

        result = {
            "format_name": format_name,
            "success": True,
            "elapsed": round(elapsed, 2),
            "text": text[:500],
            "input_tokens": usage.get("inputTokens", 0),
            "output_tokens": usage.get("outputTokens", 0),
            "latency_ms": metrics.get("latencyMs", 0),
            "has_reasoning": "reasoning" in text.lower() or "step" in text.lower() or "therefore" in text.lower() or "first" in text.lower() or "calculate" in text.lower(),
        }

        print(f"\n✅ SUCCESS ({elapsed:.1f}s)")
        print(f"Response: {text[:300]}...")
        print(f"Tokens: {usage.get('inputTokens', 0)} in → {usage.get('outputTokens', 0)} out")

        # Check for reasoning content blocks
        for block in content_blocks:
            if "reasoningContent" in block:
                result["has_reasoning"] = True
                print(f"🔍 FOUND reasoningContent block!")
                break

        return result

    except Exception as e:
        err_str = str(e)
        print(f"\n❌ ERROR: {err_str[:300]}")
        return {
            "format_name": format_name,
            "success": False,
            "error": err_str[:500],
        }


def main():
    client = _make_client()

    # Test 1: No additional fields (baseline)
    r1 = _test_format(client, "1. NO ADDITIONAL FIELDS (baseline)", None)

    # Test 2: thinking.type enabled + budget_tokens (works for Claude)
    r2 = _test_format(client, "2. thinking.type enabled + budget_tokens", {
        "thinking": {"type": "enabled", "budget_tokens": 1024}
    })

    # Test 3: thinking.enabled true + budget_tokens
    r3 = _test_format(client, "3. thinking.enabled true + budget_tokens", {
        "thinking": {"enabled": True, "budget_tokens": 1024}
    })

    # Test 4: enable_thinking + thinking_budget_tokens
    r4 = _test_format(client, "4. enable_thinking + budget_tokens", {
        "enable_thinking": True,
        "thinking_budget_tokens": 1024
    })

    # Test 5: do_reasoning + reasoning_budget_tokens
    r5 = _test_format(client, "5. do_reasoning + budget_tokens", {
        "do_reasoning": True,
        "reasoning_budget_tokens": 1024
    })

    # Test 6: reasoning_config high
    r6 = _test_format(client, "6. reasoning_config high", {
        "reasoning_config": "high"
    })

    # Test 7: reasoning.effort high (OpenRouter format)
    r7 = _test_format(client, "7. reasoning.effort high", {
        "reasoning": {"effort": "high"}
    })

    # Test 8: thinking.type enabled WITHOUT budget_tokens
    r8 = _test_format(client, "8. thinking.type enabled (no budget)", {
        "thinking": {"type": "enabled"}
    })

    # Test 9: thinking.type enabled + budget_tokens (temperature=1.0)
    r9 = _test_format(client, "9. thinking + budget + temp=1.0", {
        "thinking": {"type": "enabled", "budget_tokens": 1024}
    })

    # ── Summary ──
    print(f"\n\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    results = [r1, r2, r3, r4, r5, r6, r7, r8, r9]
    for r in results:
        status = "✅" if r["success"] else "❌"
        reasoning = "🧠" if r.get("has_reasoning") else "  "
        if r["success"]:
            print(f"  {status} {reasoning} {r['format_name']:45s} | {r['elapsed']:5.1f}s | {r.get('output_tokens', 0):5d} tok")
        else:
            print(f"  {status} {reasoning} {r['format_name']:45s} | ERROR: {r.get('error', '')[:80]}")


if __name__ == "__main__":
    main()