"""Minimal, dependency-free Anthropic Messages API client.

Uses only the standard library (urllib) so the CLI runs anywhere without a
`pip install`. If `ANTHROPIC_API_KEY` is not set, callers should fall back to
the offline heuristic scorer.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MODEL = os.environ.get("ASSESSOR_MODEL", "claude-sonnet-4-6")


class LLMUnavailable(RuntimeError):
    """Raised when the model cannot be reached or returns an error."""


def have_api_key() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def complete(
    system: str,
    user: str,
    model: str = DEFAULT_MODEL,
    max_tokens: int = 4096,
    temperature: float = 0.2,
    timeout: int = 90,
) -> str:
    """Send a single-turn message and return the concatenated text content."""
    return complete_messages(
        system, [{"role": "user", "content": user}],
        model=model, max_tokens=max_tokens, temperature=temperature, timeout=timeout,
    )


def complete_messages(
    system: str,
    messages: list[dict],
    model: str = DEFAULT_MODEL,
    max_tokens: int = 4096,
    temperature: float = 0.2,
    timeout: int = 90,
) -> str:
    """Send a multi-turn conversation (list of {role, content}) and return the text.

    This is what lets the negotiator engine share the assessor's dependency-free
    API path: the whole tool runs on one client and one ANTHROPIC_API_KEY.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise LLMUnavailable("ANTHROPIC_API_KEY is not set.")

    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "system": system,
        "messages": messages,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        API_URL,
        data=data,
        headers={
            "content-type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_VERSION,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise LLMUnavailable(f"API HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise LLMUnavailable(f"Network error reaching API: {exc.reason}") from exc

    parts = [blk.get("text", "") for blk in body.get("content", []) if blk.get("type") == "text"]
    text = "".join(parts).strip()
    if not text:
        raise LLMUnavailable("API returned an empty response.")
    return text


def extract_json(text: str) -> dict:
    """Pull the first JSON object out of a model response (tolerant of code fences)."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        # strip leading ```json / ``` and trailing ```
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned
        if cleaned.rstrip().endswith("```"):
            cleaned = cleaned.rsplit("```", 1)[0]
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end != -1 and end > start:
        return json.loads(cleaned[start : end + 1])
    raise ValueError("No JSON object found in model response.")
