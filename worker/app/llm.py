"""
llm.py — multi-provider JSON generation for the KH enrichment worker.

Providers:
  gemini       → Google Generative Language API (generateContent, JSON mime)
  openai       → OpenAI chat completions (response_format json_object)
  openrouter   → OpenAI-compatible via OpenRouter
  huggingface  → OpenAI-compatible via Hugging Face Inference Router
  deepseek     → OpenAI-compatible via DeepSeek
  grok         → OpenAI-compatible via xAI
  qwen         → OpenAI-compatible via DashScope (intl)
  kimi         → OpenAI-compatible via Moonshot
  custom       → any OpenAI-compatible base_url
  anthropic    → Anthropic Messages API (JSON extracted from text)

Per-job config arrives in the enrich payload under job["llm"].
If api_key is missing for gemini, falls back to GEMINI_API_KEY env.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass

import httpx

GEMINI_ENV_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_ENV_MODEL = os.environ.get("KH_WORKER_GEMINI_MODEL", "gemini-2.5-flash")
DEFAULT_INPUT_USD = float(os.environ.get("KH_WORKER_GEMINI_INPUT_USD_PER_M", "0.15") or "0.15")
DEFAULT_OUTPUT_USD = float(os.environ.get("KH_WORKER_GEMINI_OUTPUT_USD_PER_M", "0.60") or "0.60")

DEFAULT_BASE_URLS = {
    "gemini": "https://generativelanguage.googleapis.com/v1beta",
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com",
    "openrouter": "https://openrouter.ai/api/v1",
    "huggingface": "https://router.huggingface.co/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "grok": "https://api.x.ai/v1",
    "qwen": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
    "kimi": "https://api.moonshot.ai/v1",
}

OPENAI_COMPAT_PROVIDERS = frozenset({
    "openai",
    "openrouter",
    "huggingface",
    "deepseek",
    "grok",
    "qwen",
    "kimi",
    "custom",
})


@dataclass
class LlmConfig:
    provider: str
    model: str
    api_key: str | None
    base_url: str | None
    input_usd_per_m: float
    output_usd_per_m: float

    @classmethod
    def from_dict(cls, llm: dict | None, *, default_provider: str = "gemini") -> "LlmConfig":
        llm = llm or {}
        provider = str(llm.get("provider") or default_provider).strip().lower() or default_provider
        model = str(llm.get("model") or "").strip() or (
            GEMINI_ENV_MODEL if provider == "gemini" else ""
        )
        api_key = llm.get("api_key")
        if isinstance(api_key, str):
            api_key = api_key.strip() or None
        else:
            api_key = None
        if not api_key and provider == "gemini":
            api_key = GEMINI_ENV_KEY or None

        base_url = llm.get("base_url")
        if isinstance(base_url, str):
            base_url = base_url.strip().rstrip("/") or None
        else:
            base_url = None
        if not base_url:
            base_url = DEFAULT_BASE_URLS.get(provider)

        try:
            in_rate = float(llm.get("input_usd_per_m") or DEFAULT_INPUT_USD)
        except (TypeError, ValueError):
            in_rate = DEFAULT_INPUT_USD
        try:
            out_rate = float(llm.get("output_usd_per_m") or DEFAULT_OUTPUT_USD)
        except (TypeError, ValueError):
            out_rate = DEFAULT_OUTPUT_USD

        if not model:
            raise RuntimeError(f"LLM model missing for provider={provider}")

        return cls(
            provider=provider,
            model=model,
            api_key=api_key,
            base_url=base_url,
            input_usd_per_m=in_rate,
            output_usd_per_m=out_rate,
        )

    @classmethod
    def from_job(cls, job: dict | None) -> "LlmConfig":
        return cls.from_dict((job or {}).get("llm") or {})

    @classmethod
    def fallback_from_job(cls, job: dict | None) -> "LlmConfig | None":
        """Optional refusal-fallback config nested under job['llm']['fallback']."""
        llm = (job or {}).get("llm") or {}
        fb = llm.get("fallback")
        if not isinstance(fb, dict) or not fb.get("api_key") or not fb.get("provider"):
            return None
        try:
            return cls.from_dict(fb)
        except Exception:
            return None


_REFUSAL_MARKERS = (
    "i cannot",
    "i can't",
    "i’m unable",
    "i'm unable",
    "as an ai",
    "as a language model",
    "i am not able",
    "i'm not able",
    "i won’t",
    "i won't",
    "against my guidelines",
    "violates",
    "i must refuse",
    "cannot assist with",
    "can't assist with",
    "unable to provide",
    "not appropriate",
)


def looks_like_refusal(text: str | None) -> bool:
    """Heuristic: empty, non-JSON prose, or common policy-refusal phrases."""
    if text is None:
        return True
    raw = str(text).strip()
    if not raw:
        return True
    lower = raw.lower()
    # Prefer JSON detection — if it parses as object/array, treat as content
    try:
        candidate = raw
        if candidate.startswith("```"):
            candidate = candidate.strip("`")
            if candidate.lower().startswith("json"):
                candidate = candidate[4:].lstrip()
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start != -1 and end != -1 and end > start:
            parsed = json.loads(candidate[start : end + 1])
            if isinstance(parsed, dict) and len(parsed) > 0:
                return False
    except (ValueError, TypeError):
        pass

    for marker in _REFUSAL_MARKERS:
        if marker in lower:
            return True
    # Short non-JSON prose without braces is almost always a refusal/error
    if "{" not in raw and len(raw) < 400:
        return True
    return False


def generate_json_with_fallback(
    prompt: str,
    primary: LlmConfig,
    fallback: LlmConfig | None,
    usage_tracker=None,
    retries: int = 5,
) -> str:
    """Generate JSON; if primary refuses/empty, retry once with fallback."""
    text = generate_json(prompt, primary, usage_tracker, retries=retries)
    if not looks_like_refusal(text):
        return text
    if fallback is None:
        return text
    if usage_tracker is not None and hasattr(usage_tracker, "mark_primary_refused"):
        usage_tracker.mark_primary_refused()
    text2 = generate_json(prompt, fallback, usage_tracker, retries=max(2, retries // 2))
    if usage_tracker is not None and hasattr(usage_tracker, "mark_fallback_used"):
        usage_tracker.mark_fallback_used()
    return text2


def generate_json(prompt: str, cfg: LlmConfig, usage_tracker=None, retries: int = 5) -> str:
    """Return model text that should be JSON. Tracks usage when tracker provided."""
    if cfg.provider == "gemini":
        return _gemini(prompt, cfg, usage_tracker, retries)
    if cfg.provider == "anthropic":
        return _anthropic(prompt, cfg, usage_tracker, retries)
    if cfg.provider in OPENAI_COMPAT_PROVIDERS:
        return _openai_compat(prompt, cfg, usage_tracker, retries)
    raise RuntimeError(f"Unsupported LLM provider: {cfg.provider}")


def _add_usage(tracker, prompt_tokens: int, output_tokens: int) -> None:
    if tracker is None:
        return
    tracker.add({
        "prompt_tokens": prompt_tokens,
        "output_tokens": output_tokens,
        "total_tokens": prompt_tokens + output_tokens,
    })


def _gemini(prompt: str, cfg: LlmConfig, tracker, retries: int) -> str:
    if not cfg.api_key:
        raise RuntimeError("Gemini API key missing (job llm.api_key and GEMINI_API_KEY both empty)")
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{cfg.model}:generateContent?key={cfg.api_key}"
    )
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.4,
            "maxOutputTokens": 65536,
        },
    }
    delay = 2.0
    last_err = ""
    for _ in range(retries):
        try:
            resp = httpx.post(url, json=payload, timeout=180.0)
            if resp.status_code in (429, 500, 502, 503, 504):
                last_err = f"HTTP {resp.status_code}"
                time.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            if resp.status_code >= 400:
                body = resp.text[:800] if resp.text else ""
                last_err = f"HTTP {resp.status_code}: {body}"
                if resp.status_code in (400, 401, 403, 404):
                    raise RuntimeError(f"Gemini generation failed: {last_err}")
                time.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            resp.raise_for_status()
            data = resp.json()
            meta = data.get("usageMetadata") or {}
            if tracker is not None:
                tracker.add(meta)
            candidates = data.get("candidates", [])
            if not candidates:
                last_err = "no candidates"
                time.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            parts = candidates[0].get("content", {}).get("parts", [])
            return "".join(p.get("text", "") for p in parts)
        except httpx.HTTPError as exc:
            last_err = str(exc)
            time.sleep(delay)
            delay = min(delay * 2, 60)
    raise RuntimeError(f"Gemini generation failed after {retries} attempts: {last_err}")


def _openai_compat(prompt: str, cfg: LlmConfig, tracker, retries: int) -> str:
    if not cfg.api_key:
        raise RuntimeError(f"{cfg.provider} API key missing")
    if not cfg.base_url:
        raise RuntimeError(f"{cfg.provider} base_url missing")

    headers = {
        "Authorization": f"Bearer {cfg.api_key}",
        "Content-Type": "application/json",
    }
    if cfg.provider == "openrouter":
        headers["HTTP-Referer"] = "https://doctorshero.com"
        headers["X-Title"] = "DoctorsHero Knowledge Hub"

    payload = {
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
    url = f"{cfg.base_url}/chat/completions"
    delay = 2.0
    last_err = ""
    for _ in range(retries):
        try:
            resp = httpx.post(url, headers=headers, json=payload, timeout=180.0)
            if resp.status_code in (429, 500, 502, 503, 504):
                last_err = f"HTTP {resp.status_code}: {resp.text[:200]}"
                time.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            if not resp.is_success:
                raise RuntimeError(f"{cfg.provider} HTTP {resp.status_code}: {resp.text[:400]}")
            data = resp.json()
            usage = data.get("usage") or {}
            _add_usage(
                tracker,
                int(usage.get("prompt_tokens") or 0),
                int(usage.get("completion_tokens") or usage.get("output_tokens") or 0),
            )
            choices = data.get("choices") or []
            if not choices:
                last_err = "no choices"
                time.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            msg = choices[0].get("message") or {}
            content = msg.get("content") or ""
            if isinstance(content, list):
                content = "".join(
                    (c.get("text") if isinstance(c, dict) else str(c)) for c in content
                )
            return str(content)
        except httpx.HTTPError as exc:
            last_err = str(exc)
            time.sleep(delay)
            delay = min(delay * 2, 60)
    raise RuntimeError(f"{cfg.provider} generation failed after {retries} attempts: {last_err}")


def _anthropic(prompt: str, cfg: LlmConfig, tracker, retries: int) -> str:
    if not cfg.api_key:
        raise RuntimeError("Anthropic API key missing")
    base = cfg.base_url or DEFAULT_BASE_URLS["anthropic"]
    headers = {
        "x-api-key": cfg.api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": cfg.model,
        "max_tokens": 16384,
        "temperature": 0.4,
        "system": "You are a clinical knowledge author. Reply with valid JSON only. No markdown fences.",
        "messages": [{"role": "user", "content": prompt}],
    }
    url = f"{base}/v1/messages"
    delay = 2.0
    last_err = ""
    for _ in range(retries):
        try:
            resp = httpx.post(url, headers=headers, json=payload, timeout=180.0)
            if resp.status_code in (429, 500, 502, 503, 504):
                last_err = f"HTTP {resp.status_code}: {resp.text[:200]}"
                time.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            if not resp.is_success:
                raise RuntimeError(f"Anthropic HTTP {resp.status_code}: {resp.text[:400]}")
            data = resp.json()
            usage = data.get("usage") or {}
            _add_usage(
                tracker,
                int(usage.get("input_tokens") or 0),
                int(usage.get("output_tokens") or 0),
            )
            blocks = data.get("content") or []
            texts = []
            for b in blocks:
                if isinstance(b, dict) and b.get("type") == "text":
                    texts.append(b.get("text") or "")
            return "".join(texts)
        except httpx.HTTPError as exc:
            last_err = str(exc)
            time.sleep(delay)
            delay = min(delay * 2, 60)
    raise RuntimeError(f"Anthropic generation failed after {retries} attempts: {last_err}")
