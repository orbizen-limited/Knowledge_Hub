"""
openai_compat_batch.py — OpenAI-compatible Batch API (JSONL file upload).

Used for Qwen (DashScope compatible-mode) and reusable later for OpenAI.
Flow: write JSONL → POST /files (purpose=batch) → POST /batches → poll → download output.
"""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx

from .llm import LlmConfig

TERMINAL = frozenset({
    "completed",
    "failed",
    "expired",
    "cancelled",
    "canceled",
})


def _headers(cfg: LlmConfig) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {cfg.api_key}",
    }


def _base(cfg: LlmConfig) -> str:
    base = (cfg.base_url or "").rstrip("/")
    if not base:
        raise RuntimeError(f"{cfg.provider} base_url missing for batch")
    return base


def submit_batch(
    requests: list[tuple[str, str]],
    cfg: LlmConfig,
    display_name: str = "kh-enrich-batch",
) -> str:
    """
    Submit one OpenAI-compat batch.
    requests: list of (custom_id, prompt)
    Returns batch id (e.g. batch_xxx).
    """
    if not cfg.api_key:
        raise RuntimeError(f"{cfg.provider} API key missing for batch submit")
    if not requests:
        raise RuntimeError("No requests to submit")

    base = _base(cfg)
    jsonl_lines: list[str] = []
    for custom_id, prompt in requests:
        body = {
            "model": cfg.model,
            "temperature": 0.4,
            "max_tokens": 16384,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": "You are a clinical knowledge author. Reply with valid JSON only.",
                },
                {"role": "user", "content": prompt},
            ],
        }
        jsonl_lines.append(json.dumps({
            "custom_id": custom_id,
            "method": "POST",
            "url": "/v1/chat/completions",
            "body": body,
        }, ensure_ascii=False))

    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8") as fh:
        fh.write("\n".join(jsonl_lines) + "\n")
        path = Path(fh.name)

    try:
        with path.open("rb") as fh:
            resp = httpx.post(
                f"{base}/files",
                headers=_headers(cfg),
                data={"purpose": "batch"},
                files={"file": (path.name, fh, "application/jsonl")},
                timeout=120.0,
            )
        if not resp.is_success:
            raise RuntimeError(
                f"{cfg.provider} file upload HTTP {resp.status_code}: {resp.text[:600]}"
            )
        file_id = (resp.json() or {}).get("id")
        if not file_id:
            raise RuntimeError(f"{cfg.provider} file upload missing id: {resp.text[:400]}")

        batch_resp = httpx.post(
            f"{base}/batches",
            headers={**_headers(cfg), "Content-Type": "application/json"},
            json={
                "input_file_id": file_id,
                "endpoint": "/v1/chat/completions",
                "completion_window": "24h",
                "metadata": {"description": display_name[:64]},
            },
            timeout=60.0,
        )
        if not batch_resp.is_success:
            raise RuntimeError(
                f"{cfg.provider} batch create HTTP {batch_resp.status_code}: {batch_resp.text[:600]}"
            )
        batch_id = (batch_resp.json() or {}).get("id")
        if not batch_id:
            raise RuntimeError(f"{cfg.provider} batch create missing id: {batch_resp.text[:400]}")
        return str(batch_id)
    finally:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def get_batch(batch_id: str, cfg: LlmConfig) -> dict[str, Any]:
    base = _base(cfg)
    resp = httpx.get(
        f"{base}/batches/{batch_id}",
        headers=_headers(cfg),
        timeout=60.0,
    )
    if not resp.is_success:
        raise RuntimeError(
            f"{cfg.provider} batch get HTTP {resp.status_code}: {resp.text[:400]}"
        )
    return resp.json()


def _download_file(file_id: str, cfg: LlmConfig) -> str:
    base = _base(cfg)
    resp = httpx.get(
        f"{base}/files/{file_id}/content",
        headers=_headers(cfg),
        timeout=120.0,
    )
    if not resp.is_success:
        raise RuntimeError(
            f"{cfg.provider} file content HTTP {resp.status_code}: {resp.text[:400]}"
        )
    return resp.text


def poll_batches(
    batch_ids: list[str],
    cfg: LlmConfig,
    poll_interval_sec: int = 60,
    poll_timeout_sec: int = 86400,
    on_tick=None,
) -> dict[str, str]:
    """Poll until all batches terminal. Returns map custom_id → response text."""
    deadline = time.time() + max(60, poll_timeout_sec)
    pending = set(batch_ids)
    texts: dict[str, str] = {}

    while pending:
        if time.time() > deadline:
            raise RuntimeError(
                f"{cfg.provider} batch poll timed out; still pending: {sorted(pending)}"
            )

        for batch_id in list(pending):
            batch = get_batch(batch_id, cfg)
            state = str(batch.get("status") or "").lower()
            if on_tick:
                on_tick(batch_id, state, batch)
            if state not in TERMINAL:
                continue
            if state != "completed":
                err = batch.get("errors") or batch.get("error") or state
                raise RuntimeError(f"{cfg.provider} batch {batch_id} ended {state}: {err}")
            out_file = batch.get("output_file_id")
            if not out_file:
                raise RuntimeError(f"{cfg.provider} batch {batch_id} completed without output_file_id")
            content = _download_file(str(out_file), cfg)
            texts.update(_parse_output_jsonl(content))
            pending.discard(batch_id)

        if pending:
            time.sleep(max(15, poll_interval_sec))

    return texts


def _parse_output_jsonl(content: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        custom_id = str(row.get("custom_id") or "")
        if not custom_id:
            continue
        if row.get("error"):
            out[custom_id] = ""
            continue
        resp = row.get("response") or {}
        body = resp.get("body") if isinstance(resp, dict) else None
        if body is None and isinstance(resp, dict) and "choices" in resp:
            body = resp
        if not isinstance(body, dict):
            out[custom_id] = ""
            continue
        choices = body.get("choices") or []
        if not choices:
            out[custom_id] = ""
            continue
        msg = (choices[0] or {}).get("message") or {}
        content_text = msg.get("content") or ""
        if isinstance(content_text, list):
            content_text = "".join(
                (c.get("text") if isinstance(c, dict) else str(c)) for c in content_text
            )
        out[custom_id] = str(content_text)
    return out
