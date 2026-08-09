"""
gemini_batch.py — Gemini Batch API (batchGenerateContent) via REST.

Uses the same API key as live Gemini. ~50% discount; results within ~24h.
Supports chunked inline batches (keeps each POST under the ~20MB limit).
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from .llm import LlmConfig

TERMINAL = frozenset({
    "JOB_STATE_SUCCEEDED",
    "JOB_STATE_FAILED",
    "JOB_STATE_CANCELLED",
    "JOB_STATE_EXPIRED",
})

# Stay well under the 20MB inline payload limit and provider request caps.
MAX_REQUESTS_PER_CHUNK = 40


def _headers(cfg: LlmConfig) -> dict[str, str]:
    return {
        "x-goog-api-key": cfg.api_key or "",
        "Content-Type": "application/json",
    }


def _build_inlined_request(key: str, prompt: str) -> dict[str, Any]:
    return {
        "request": {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "temperature": 0.4,
                "maxOutputTokens": 65536,
            },
        },
        "metadata": {"key": key},
    }


def submit_batch(
    requests: list[tuple[str, str]],
    cfg: LlmConfig,
    display_name: str = "kh-enrich-batch",
) -> list[str]:
    """
    Submit one or more Gemini batches.

    requests: list of (custom_key, prompt)
    Returns list of batch resource names (e.g. batches/123).
    """
    if not cfg.api_key:
        raise RuntimeError("Gemini API key missing for batch submit")
    if not requests:
        raise RuntimeError("No requests to submit")

    names: list[str] = []
    for i in range(0, len(requests), MAX_REQUESTS_PER_CHUNK):
        chunk = requests[i : i + MAX_REQUESTS_PER_CHUNK]
        names.append(_submit_chunk(chunk, cfg, f"{display_name}-{i // MAX_REQUESTS_PER_CHUNK + 1}"))
    return names


def _submit_chunk(
    chunk: list[tuple[str, str]],
    cfg: LlmConfig,
    display_name: str,
) -> str:
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{cfg.model}:batchGenerateContent"
    )
    payload = {
        "batch": {
            "display_name": display_name[:128],
            "input_config": {
                "requests": {
                    "requests": [_build_inlined_request(k, p) for k, p in chunk],
                }
            },
        }
    }
    resp = httpx.post(url, headers=_headers(cfg), json=payload, timeout=120.0)
    if not resp.is_success:
        raise RuntimeError(f"Gemini batch submit HTTP {resp.status_code}: {resp.text[:600]}")
    data = resp.json()
    # Response may be the batch object directly or wrapped
    name = data.get("name") or (data.get("batch") or {}).get("name")
    if not name:
        raise RuntimeError(f"Gemini batch submit missing name: {str(data)[:400]}")
    return str(name)


def get_batch(name: str, cfg: LlmConfig) -> dict[str, Any]:
    url = f"https://generativelanguage.googleapis.com/v1beta/{name}"
    resp = httpx.get(url, headers=_headers(cfg), timeout=60.0)
    if not resp.is_success:
        raise RuntimeError(f"Gemini batch get HTTP {resp.status_code}: {resp.text[:400]}")
    return resp.json()


def _state(batch: dict[str, Any]) -> str:
    state = batch.get("state") or batch.get("batch", {}).get("state") or ""
    if isinstance(state, dict):
        return str(state.get("name") or state.get("state") or "")
    return str(state)


def poll_batches(
    names: list[str],
    cfg: LlmConfig,
    poll_interval_sec: int = 60,
    poll_timeout_sec: int = 86400,
    on_tick=None,
) -> dict[str, str]:
    """
    Poll until all batches terminal. Returns map custom_key → response text.
    """
    deadline = time.time() + max(60, poll_timeout_sec)
    pending = set(names)
    texts: dict[str, str] = {}

    while pending:
        if time.time() > deadline:
            raise RuntimeError(f"Gemini batch poll timed out; still pending: {sorted(pending)}")

        for name in list(pending):
            batch = get_batch(name, cfg)
            state = _state(batch)
            if on_tick:
                on_tick(name, state, batch)
            if state not in TERMINAL:
                continue
            if state != "JOB_STATE_SUCCEEDED":
                err = batch.get("error") or batch.get("batch", {}).get("error") or state
                raise RuntimeError(f"Gemini batch {name} ended {state}: {err}")
            texts.update(_extract_texts(batch))
            pending.discard(name)

        if pending:
            time.sleep(max(15, poll_interval_sec))

    return texts


def _extract_texts(batch: dict[str, Any]) -> dict[str, str]:
    """Parse inlined responses into key → text."""
    out: dict[str, str] = {}
    # Common shapes across API versions
    dest = (
        batch.get("dest")
        or batch.get("output")
        or batch.get("response")
        or (batch.get("batch") or {}).get("dest")
        or {}
    )
    inlined = (
        dest.get("inlinedResponses")
        or dest.get("inlined_responses")
        or batch.get("inlinedResponses")
        or {}
    )
    responses = inlined.get("inlinedResponses") or inlined.get("responses") or []
    if isinstance(inlined, list):
        responses = inlined

    for idx, item in enumerate(responses):
        if not isinstance(item, dict):
            continue
        meta = item.get("metadata") or {}
        key = meta.get("key") or item.get("key") or f"idx-{idx}"
        # error on this line?
        if item.get("error"):
            out[str(key)] = ""
            continue
        resp = item.get("response") or item.get("inlineResponse") or item
        candidates = resp.get("candidates") or []
        if not candidates:
            out[str(key)] = ""
            continue
        parts = (candidates[0].get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
        out[str(key)] = text

        # Best-effort usage — caller may sum later if usageMetadata present
        _ = resp.get("usageMetadata")

    return out
