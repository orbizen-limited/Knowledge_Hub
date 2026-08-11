"""
v6 media stage — propose, SSRF-safe fetch, Laravel store.

Never raises into the text job: callers wrap attach_media() and drop failures.
Videos are YouTube/Vimeo link-out (oEmbed) only. Images: PNG/JPEG/GIF, no SVG.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import re
import socket
import time
import uuid
from urllib.parse import urljoin, urlparse

import httpx

from . import security

USER_AGENT = "DoctorsHeroKHBot/1.0 (https://doctorshero.com; knowledge-hub-media)"
WIKIMEDIA_SLEEP_SEC = 3.0
FETCH_TIMEOUT = 20.0
DEFAULT_ALLOWLIST = ("upload.wikimedia.org", "commons.wikimedia.org")
VIDEO_HOSTS = (
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "youtu.be",
    "vimeo.com",
    "www.vimeo.com",
    "player.vimeo.com",
)
BLOCKED_HOST_FRAGMENTS = (
    "metadata.google",
    "169.254.169.254",
)

MEDIA_PROPOSE_RULES = """\
You are proposing teaching media for a Knowledge Hub topic that is ALREADY authored.
Return STRICT JSON only: {"media":[...]}  — no topic body, no markdown.

Each item:
{
  "id": "kebab-id",
  "kind": "image"|"gif"|"animation"|"video",
  "sectionKey": "overview"|"background"|"pathophysiology"|"diagnosis"|"management"|"complications"|"monitoring"|"patientEducation",
  "title": "...",
  "caption": "diagram description, not a patient photograph",
  "alt": "...",
  "proposedUrl": "https://...",
  "sourceName": "Wikimedia Commons|NCI|YouTube|Vimeo",
  "license": "CC BY-SA 4.0|CC BY 4.0|CC0|public domain|YouTube ToS|Vimeo ToS",
  "attribution": "...",
  "relevance": "why this belongs on this section"
}

HARD:
- Prefer diagrams / flowcharts / labeled anatomy. Skip patient-photo captions.
- sourceName AND license required or omit the item.
- Images: HTTPS Wikimedia Commons only (upload.wikimedia.org or commons.wikimedia.org).
  Prefer direct file URLs. No SVG.
- Videos: YouTube or Vimeo watch URLs only. Never a video file download.
- No Google image scrape. No invented filenames. No minimum count.
"""

_PATIENT_PHOTO_RE = re.compile(
    r"\b(patient photo|clinical photo|identifiable|face of|portrait of patient)\b",
    re.I,
)


def attach_media(topic: dict, job: dict, callback_url: str, job_id: str, topic_id: str) -> dict:
    """Stamp contentStandard and optionally attach validated media[]. Never raises."""
    from . import pipeline  # late import: pipeline.py imports this module

    llm_block = job.get("llm") if isinstance(job.get("llm"), dict) else {}
    std = str((llm_block or {}).get("content_standard") or "v5").strip().lower()
    if std != "v6":
        topic["contentStandard"] = "v5"
        topic["media"] = []
        return topic

    topic["contentStandard"] = "v6"
    try:
        pipeline._progress(callback_url, job_id, topic_id, "media", 92)
        topic["media"] = _run_media_stage(
            topic, llm_block or {}, callback_url, job_id, topic_id
        )
    except Exception as exc:  # noqa: BLE001 — media must never fail the text job
        print(f"[media] stage failed (text job continues): {exc}", flush=True)
        topic["media"] = []
    return topic


def _run_media_stage(topic: dict, llm_block: dict, callback_url: str, job_id: str, topic_id: str) -> list[dict]:
    allowlist = _parse_allowlist(llm_block.get("media_host_allowlist"))
    allowlist = _parse_allowlist(llm_block.get("media_host_allowlist"))
    max_topic = _count_limit(llm_block.get("media_max_per_topic"))
    max_section = _count_limit(llm_block.get("media_max_per_section"))
    max_bytes = int(llm_block.get("media_max_bytes") or 2097152)
    if max_bytes <= 0:
        max_bytes = 2097152

    candidates = _propose(topic, llm_block)
    candidates = _apply_count_limits(candidates, max_topic, max_section)

    media_url = _media_ingest_url(callback_url)
    out: list[dict] = []
    last_wm = 0.0

    for cand in candidates:
        try:
            item = _process_candidate(
                cand,
                allowlist=allowlist,
                max_bytes=max_bytes,
                media_url=media_url,
                job_id=job_id,
                topic_id=topic_id,
                last_wm=last_wm,
            )
            if item is None:
                continue
            if item.pop("_wikimedia", False):
                last_wm = time.time()
            out.append(item)
        except Exception as exc:  # noqa: BLE001
            print(f"[media] skip item: {exc}", flush=True)
            continue
    return out


def _propose(topic: dict, llm_block: dict) -> list[dict]:
    from . import pipeline

    rules = str(llm_block.get("v6_prompt") or "").strip() or MEDIA_PROPOSE_RULES
    slim = {
        "topicId": topic.get("topicId"),
        "title": topic.get("title"),
        "specialty": topic.get("specialty"),
        "chapter": topic.get("chapter"),
        "bottomLine": (topic.get("bottomLine") or "")[:800],
        "managementHeadings": [
            (b.get("heading") if isinstance(b, dict) else None)
            for b in (topic.get("managementSections") or [])[:12]
        ],
        "diagnosisHeadings": [
            (b.get("heading") if isinstance(b, dict) else None)
            for b in (topic.get("diagnosisSections") or [])[:8]
        ],
    }
    prompt = (
        f"{rules}\n\nTOPIC CONTEXT (do not rewrite):\n"
        f"{json.dumps(slim, ensure_ascii=False)}\n\n"
        "Return ONLY {\"media\":[...]}."
    )
    raw = pipeline._llm_generate(prompt)
    data = pipeline._extract_json(raw)
    items = data.get("media") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    return [i for i in items if isinstance(i, dict)]


def _process_candidate(
    cand: dict,
    *,
    allowlist: tuple[str, ...],
    max_bytes: int,
    media_url: str,
    job_id: str,
    topic_id: str,
    last_wm: float,
) -> dict | None:
    source = str(cand.get("sourceName") or cand.get("source") or "").strip()
    license_ = str(cand.get("license") or "").strip()
    if not source or not license_:
        return None
    caption = str(cand.get("caption") or "")
    if _PATIENT_PHOTO_RE.search(caption):
        return None

    kind = str(cand.get("kind") or cand.get("type") or "image").strip().lower()
    url = str(cand.get("proposedUrl") or cand.get("url") or cand.get("src") or "").strip()
    if not url:
        return None

    media_id = str(cand.get("id") or "").strip() or f"m-{uuid.uuid4().hex[:10]}"
    base = {
        "id": media_id,
        "kind": kind if kind in {"image", "gif", "animation", "video"} else "image",
        "sectionKey": str(cand.get("sectionKey") or cand.get("section") or "overview").strip().lower(),
        "title": str(cand.get("title") or "").strip(),
        "caption": caption.strip(),
        "alt": str(cand.get("alt") or cand.get("altText") or "").strip(),
        "sourceName": source,
        "license": license_,
        "attribution": str(cand.get("attribution") or "").strip(),
        "relevance": str(cand.get("relevance") or "").strip(),
        "posterUrl": None,
        "validated": False,
    }

    if base["kind"] == "video":
        watch = _validate_video_url(url)
        if not watch:
            return None
        base["url"] = watch
        base["validated"] = True
        return base

    fetched = _fetch_image(url, allowlist, max_bytes, last_wm)
    if fetched is None:
        return None
    stored = _store_via_laravel(
        media_url, job_id, topic_id, media_id, fetched["bytes"], fetched["mime"]
    )
    if not stored:
        return None
    if fetched["mime"] == "image/gif":
        base["kind"] = "gif"
    base["url"] = stored
    base["validated"] = True
    base["_wikimedia"] = True
    return base


def _validate_video_url(url: str) -> str | None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        return None
    host = parsed.hostname.lower()
    if host not in VIDEO_HOSTS:
        return None
    oembed = None
    if "youtu" in host:
        oembed = "https://www.youtube.com/oembed"
    elif "vimeo" in host:
        oembed = "https://vimeo.com/api/oembed.json"
    if not oembed:
        return None
    try:
        resp = httpx.get(
            oembed,
            params={"url": url, "format": "json"},
            headers={"User-Agent": USER_AGENT},
            timeout=15.0,
            follow_redirects=True,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        if not isinstance(data, dict) or not data.get("type"):
            return None
    except Exception:
        return None
    return url


def _fetch_image(url: str, allowlist: tuple[str, ...], max_bytes: int, last_wm: float) -> dict | None:
    current = url
    for _hop in range(4):
        if not _ssrf_ok(current, allowlist):
            return None
        host = (urlparse(current).hostname or "").lower()
        if host.endswith("wikimedia.org") or host.endswith("wikipedia.org"):
            wait = WIKIMEDIA_SLEEP_SEC - (time.time() - last_wm)
            if wait > 0:
                time.sleep(wait)
        headers = {"User-Agent": USER_AGENT, "Accept": "image/png,image/jpeg,image/gif,*/*"}
        try:
            with httpx.Client(follow_redirects=False, timeout=FETCH_TIMEOUT, headers=headers) as client:
                with client.stream("GET", current) as resp:
                    if resp.status_code in (301, 302, 303, 307, 308):
                        loc = resp.headers.get("location")
                        if not loc:
                            return None
                        current = urljoin(current, loc)
                        continue
                    if resp.status_code == 429:
                        print("[media] 429 — skip item", flush=True)
                        return None
                    if resp.status_code >= 400:
                        return None
                    cl = resp.headers.get("content-length")
                    if cl and cl.isdigit() and int(cl) > max_bytes:
                        return None
                    buf = bytearray()
                    for chunk in resp.iter_bytes():
                        buf.extend(chunk)
                        if len(buf) > max_bytes:
                            return None
        except httpx.HTTPError:
            return None
        mime = _magic_mime(bytes(buf))
        if mime is None:
            return None
        return {"bytes": bytes(buf), "mime": mime}
    return None


def _store_via_laravel(
    media_url: str, job_id: str, topic_id: str, media_id: str, raw: bytes, mime: str
) -> str | None:
    payload = {
        "job_id": job_id,
        "topic_id": topic_id,
        "media_id": media_id,
        "content_type": mime,
        "bytes_base64": base64.b64encode(raw).decode("ascii"),
    }
    body = json.dumps(payload, ensure_ascii=False)
    path = urlparse(media_url).path or "/"
    headers = security.build_outbound_headers("POST", path, body)
    try:
        resp = httpx.post(media_url, content=body.encode("utf-8"), headers=headers, timeout=60.0)
        if resp.status_code >= 400:
            print(f"[media] ingest HTTP {resp.status_code}: {resp.text[:300]}", flush=True)
            return None
        data = resp.json()
        url = ((data.get("data") or {}) if isinstance(data, dict) else {}).get("url")
        if isinstance(url, str) and url.startswith(("https://", "http://")):
            return url
    except Exception as exc:  # noqa: BLE001
        print(f"[media] ingest failed: {exc}", flush=True)
    return None


def _ssrf_ok(url: str, allowlist: tuple[str, ...]) -> bool:
    parsed = urlparse(url)
    if parsed.scheme != "https":
        return False
    if parsed.username or parsed.password:
        return False
    host = (parsed.hostname or "").lower()
    if not host or host in {"localhost", "127.0.0.1", "::1"}:
        return False
    for frag in BLOCKED_HOST_FRAGMENTS:
        if frag in host:
            return False
    if not _host_allowed(host, allowlist):
        return False
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return False
    for info in infos:
        ip_s = info[4][0]
        try:
            ip = ipaddress.ip_address(ip_s)
        except ValueError:
            return False
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            return False
        if str(ip) in {"169.254.169.254", "fd00:ec2::254"}:
            return False
    return True


def _host_allowed(host: str, allowlist: tuple[str, ...]) -> bool:
    host = host.lower().rstrip(".")
    for entry in allowlist:
        e = entry.lower().rstrip(".")
        if host == e or host.endswith("." + e):
            return True
    return False


def _parse_allowlist(raw) -> tuple[str, ...]:
    text = str(raw or "").strip()
    if not text:
        return DEFAULT_ALLOWLIST
    hosts = []
    for part in re.split(r"[\s,]+", text):
        p = part.strip().lower()
        p = re.sub(r"^https?://", "", p)
        p = p.split("/")[0].split(":")[0]
        if p and re.match(r"^[a-z0-9.-]+$", p):
            hosts.append(p)
    return tuple(hosts) if hosts else DEFAULT_ALLOWLIST


def _count_limit(raw) -> int | None:
    """None = unlimited. Positive int = cap. 0 = unlimited."""
    if raw is None or raw == "":
        return None
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    return n


def _apply_count_limits(items: list[dict], max_topic: int | None, max_section: int | None) -> list[dict]:
    out: list[dict] = []
    per_section: dict[str, int] = {}
    for item in items:
        if max_topic is not None and len(out) >= max_topic:
            break
        section = str(item.get("sectionKey") or item.get("section") or "").lower()
        if max_section is not None:
            used = per_section.get(section, 0)
            if used >= max_section:
                continue
            per_section[section] = used + 1
        out.append(item)
    return out


def _magic_mime(raw: bytes) -> str | None:
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if raw.startswith(b"GIF87a") or raw.startswith(b"GIF89a"):
        return "image/gif"
    return None


def _media_ingest_url(callback_url: str) -> str:
    parsed = urlparse(callback_url)
    path = parsed.path or ""
    if path.endswith("/callback"):
        path = path[: -len("/callback")] + "/media"
    else:
        path = path.rstrip("/") + "/media"
    return parsed._replace(path=path, query="", fragment="").geturl()
