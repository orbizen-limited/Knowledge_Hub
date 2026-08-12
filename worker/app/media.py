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
import sys
import time
import uuid
from urllib.parse import unquote, urljoin, urlparse

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
COMMONS_API = "https://commons.wikimedia.org/w/api.php"

MEDIA_PROPOSE_RULES = """\
You are proposing teaching media for a Knowledge Hub topic that is ALREADY authored.
Return STRICT JSON only: {"media":[...]}  — no topic body, no markdown.

The worker ALSO searches Wikimedia Commons itself. Your job is optional extras:
real File: titles you are sure exist, and real YouTube/Vimeo watch URLs.

Each item:
{
  "id": "kebab-id",
  "kind": "image"|"gif"|"animation"|"video",
  "sectionKey": "overview"|"background"|"pathophysiology"|"presentation"|"diagnosis"|"management"|"complications"|"monitoring"|"patientEducation",
  "title": "...",
  "caption": "diagram description, not a patient photograph",
  "alt": "...",
  "fileTitle": "File:ExactExistingName.png",
  "proposedUrl": "https://commons.wikimedia.org/wiki/File:ExactExistingName.png",
  "sourceName": "Wikimedia Commons",
  "license": "CC BY-SA 4.0|CC BY 4.0|CC0|public domain|YouTube ToS|Vimeo ToS",
  "attribution": "...",
  "relevance": "why this belongs on this section"
}

HARD:
- Prefer diagrams / flowcharts / labeled anatomy. Skip patient photographs.
- NEVER invent upload.wikimedia.org hash paths (they 404). Use File: titles only.
- NEVER invent YouTube video IDs. If you are not certain the watch URL exists, omit it.
- Images: Commons File: page or File: title. SVG is ok (worker renders PNG thumb).
- Videos: real YouTube/Vimeo watch URLs only. Worker verifies via oEmbed.
- No Google Images. No minimum count. Omit when unsure.
"""

_PATIENT_PHOTO_RE = re.compile(
    r"\b(patient photo|clinical photo|identifiable|face of|portrait of|"
    r"photograph of|clinical photograph)\b",
    re.I,
)
_PHOTO_TITLE_RE = re.compile(
    r"\b(photo|photograph|selfie|headshot|portrait)\b",
    re.I,
)
_FILE_TITLE_RE = re.compile(r"(?:File:|Datei:|Fichier:)([^?#]+)", re.I)
_YT_ID_RE = re.compile(r"(?:v=|/embed/|/shorts/|youtu\.be/)([A-Za-z0-9_-]{11})")
_IMAGE_MIMES = {"image/png", "image/jpeg", "image/gif"}
_SVG_MIMES = {"image/svg+xml", "image/svg"}


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def attach_media(topic: dict, job: dict, callback_url: str, job_id: str, topic_id: str) -> dict:
    """Stamp contentStandard and optionally attach validated media[]. Never raises."""
    from . import pipeline  # late import: pipeline.py imports this module

    llm_block = job.get("llm") if isinstance(job.get("llm"), dict) else {}
    std = str((llm_block or {}).get("content_standard") or "v5").strip().lower()
    if std != "v6":
        _log(f"[media] skip stage (content_standard={std or 'missing'})")
        topic["contentStandard"] = "v5"
        topic["media"] = []
        return topic

    topic["contentStandard"] = "v6"
    try:
        # Never ping progress for offline backfill (would reopen a finished job).
        if not str(job_id).startswith("backfill"):
            pipeline._progress(callback_url, job_id, topic_id, "media", 92)
        topic["media"] = _run_media_stage(
            topic, llm_block or {}, callback_url, job_id, topic_id
        )
        _log(f"[media] stored {len(topic['media'])} item(s) for {topic_id}")
    except Exception as exc:  # noqa: BLE001 — media must never fail the text job
        _log(f"[media] stage failed (text job continues): {exc}")
        topic["media"] = []
    return topic


def _run_media_stage(topic: dict, llm_block: dict, callback_url: str, job_id: str, topic_id: str) -> list[dict]:
    allowlist = _parse_allowlist(llm_block.get("media_host_allowlist"))
    max_topic = _count_limit(llm_block.get("media_max_per_topic"))
    max_section = _count_limit(llm_block.get("media_max_per_section"))
    max_bytes = int(llm_block.get("media_max_bytes") or 2097152)
    if max_bytes <= 0:
        max_bytes = 2097152

    proposed = _propose(topic, llm_block)
    _log(f"[media] llm proposed {len(proposed)} candidate(s)")
    searched = _commons_image_search(topic)
    _log(f"[media] commons search {len(searched)} candidate(s)")

    # Commons first (real files). Then LLM extras (YouTube + any File: that resolve).
    videos = [c for c in proposed if str(c.get("kind") or "").lower() == "video"]
    others = [c for c in proposed if str(c.get("kind") or "").lower() != "video"]
    candidates = _dedupe_candidates(searched + videos + others)
    candidates = _spread_section_keys(candidates, topic)
    candidates = _apply_count_limits(candidates, max_topic, max_section)

    media_url, host_header = _media_ingest_target(callback_url)
    _log(f"[media] ingest {media_url}")
    out: list[dict] = []
    last_wm = 0.0
    seen_urls: set[str] = set()

    for cand in candidates:
        try:
            item, last_wm = _process_candidate(
                cand,
                allowlist=allowlist,
                max_bytes=max_bytes,
                media_url=media_url,
                host_header=host_header,
                job_id=job_id,
                topic_id=topic_id,
                last_wm=last_wm,
            )
            if item is None:
                continue
            key = (item.get("url") or "").split("?")[0]
            if key in seen_urls:
                continue
            seen_urls.add(key)
            out.append(item)
        except Exception as exc:  # noqa: BLE001
            _log(f"[media] skip item: {exc}")
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
    try:
        raw = pipeline._llm_generate(prompt)
    except Exception as exc:  # noqa: BLE001
        _log(f"[media] propose LLM failed: {exc}")
        return []
    try:
        data = pipeline._extract_json(raw)
    except Exception as exc:  # noqa: BLE001
        _log(f"[media] propose JSON parse failed: {exc}; raw[:240]={str(raw)[:240]!r}")
        return []
    items = data.get("media") if isinstance(data, dict) else None
    if not isinstance(items, list):
        _log("[media] propose returned no media[] array")
        return []
    return [i for i in items if isinstance(i, dict)]


def _process_candidate(
    cand: dict,
    *,
    allowlist: tuple[str, ...],
    max_bytes: int,
    media_url: str,
    host_header: str | None,
    job_id: str,
    topic_id: str,
    last_wm: float,
) -> tuple[dict | None, float]:
    source = str(cand.get("sourceName") or cand.get("source") or "").strip()
    license_ = str(cand.get("license") or "").strip()
    if not source or not license_:
        source = source or "Wikimedia Commons"
        license_ = license_ or "CC BY-SA 4.0"
    caption = str(cand.get("caption") or "")
    if _PATIENT_PHOTO_RE.search(caption):
        _log("[media] drop patient-photo caption")
        return None, last_wm

    kind = str(cand.get("kind") or cand.get("type") or "image").strip().lower()
    url = str(cand.get("proposedUrl") or cand.get("url") or cand.get("src") or "").strip()
    file_title = str(cand.get("fileTitle") or cand.get("commonsTitle") or "").strip()
    if not url and not file_title:
        _log("[media] drop candidate with no url/fileTitle")
        return None, last_wm

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
        watch, poster = _validate_video_url(url)
        if not watch:
            _log(f"[media] video rejected (invented or oembed miss): {url[:120]}")
            return None, last_wm
        base["url"] = watch
        base["posterUrl"] = poster
        base["sourceName"] = source if "youtube" in watch or "vimeo" in watch else source
        if "youtu" in watch:
            base["sourceName"] = "YouTube"
            base["license"] = license_ or "YouTube ToS"
        elif "vimeo" in watch:
            base["sourceName"] = "Vimeo"
            base["license"] = license_ or "Vimeo ToS"
        base["validated"] = True
        return base, last_wm

    # Prefer a File: title already verified by Commons generator search.
    resolved = _resolve_commons_file(url, file_title, last_wm)
    last_wm = resolved[1]
    direct = resolved[0]
    if not direct:
        _log(f"[media] unresolved (skip invented url): {(file_title or url)[:160]}")
        return None, last_wm

    fetched = _fetch_image(direct, allowlist, max_bytes, last_wm)
    last_wm = time.time()
    if fetched is None:
        _log(f"[media] fetch failed: {direct[:160]}")
        return None, last_wm
    stored = _store_via_laravel(
        media_url, job_id, topic_id, media_id, fetched["bytes"], fetched["mime"], host_header
    )
    if not stored:
        return None, last_wm
    if fetched["mime"] == "image/gif":
        base["kind"] = "gif"
    base["url"] = stored
    base["validated"] = True
    if not base["title"]:
        base["title"] = file_title or "Diagram"
    return base, last_wm


def _short_title(topic: dict) -> str:
    t = str(topic.get("title") or "").strip()
    t = re.split(r"\s+[—–-]\s+", t, maxsplit=1)[0]
    t = re.sub(r"[&/+]", " ", t)
    t = re.sub(r"[^\w\s-]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return " ".join(t.split()[:6]).strip()


_SECTION_FALLBACK = (
    "overview",
    "pathophysiology",
    "presentation",
    "diagnosis",
    "management",
    "complications",
    "background",
    "monitoring",
    "patientEducation",
)


def _topic_section_keys(topic: dict) -> list[str]:
    """Sections that actually have body text — used to spread figures."""
    checks = [
        ("overview", topic.get("summaryParagraphs") or topic.get("bottomLine")),
        ("background", topic.get("backgroundInformation") or topic.get("etiologyEpidemiology")),
        ("pathophysiology", topic.get("pathophysiology")),
        ("presentation", topic.get("clinicalPresentation")),
        ("diagnosis", topic.get("diagnosisSections") or topic.get("differentialDiagnosis")),
        ("management", topic.get("managementSections") or topic.get("treatmentLines") or topic.get("drugRegimens")),
        ("complications", topic.get("complicationSections") or topic.get("complicationsPrognosis")),
        ("monitoring", topic.get("monitoringFollowUp")),
        ("patientEducation", topic.get("patientEducation")),
    ]
    keys = [k for k, v in checks if v]
    return keys or list(_SECTION_FALLBACK[:4])


def _spread_section_keys(items: list[dict], topic: dict) -> list[dict]:
    """Round-robin stills across real topic sections so figures are not all Overview."""
    keys = _topic_section_keys(topic)
    still_i = 0
    for item in items:
        if str(item.get("kind") or "").lower() == "video":
            continue
        item["sectionKey"] = keys[still_i % len(keys)]
        still_i += 1
    return items


def _dedupe_candidates(items: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for item in items:
        ft = str(item.get("fileTitle") or item.get("proposedUrl") or item.get("url") or "").lower()
        if not ft or ft in seen:
            continue
        seen.add(ft)
        out.append(item)
    return out


def _commons_image_search(topic: dict) -> list[dict]:
    """Find real Commons bitmaps via generator=search + imageinfo (not LLM URLs)."""
    title = _short_title(topic)
    if not title:
        return []
    words = title.split()
    stem = " ".join(words[:2]) if len(words) >= 2 else title
    queries = [
        (stem, "overview"),
        (f"{stem} diagram", "pathophysiology"),
        (f"{stem} illustration", "overview"),
        (title, "management"),
        (f"{stem} filemime:image/gif", "pathophysiology"),
    ]
    stem_words = stem.split()
    last = stem_words[-1] if stem_words else ""
    if re.search(r"(ors|ers|ings)$", last, re.I):
        singular = " ".join(stem_words[:-1] + [last[:-1]])
        queries.insert(0, (singular, "overview"))
        queries.insert(1, (f"{singular} diagram", "pathophysiology"))
    if re.search(r"\bACE\b", title, re.I):
        queries.extend([
            ("ACE inhibitor", "pathophysiology"),
            ("renin-angiotensin", "overview"),
        ])
    seen: set[str] = set()
    out: list[dict] = []
    last_wm = 0.0
    for q, section in queries:
        wait = WIKIMEDIA_SLEEP_SEC - (time.time() - last_wm)
        if wait > 0:
            time.sleep(wait)
        try:
            resp = httpx.get(
                COMMONS_API,
                params={
                    "action": "query",
                    "generator": "search",
                    "gsrsearch": q,
                    "gsrnamespace": 6,
                    "gsrlimit": 5,
                    "prop": "imageinfo",
                    "iiprop": "url|mime|size",
                    "iiurlwidth": 1280,
                    "format": "json",
                },
                headers={"User-Agent": USER_AGENT},
                timeout=20.0,
            )
            last_wm = time.time()
            if resp.status_code == 429:
                _log("[media] commons search 429")
                break
            if resp.status_code >= 400:
                _log(f"[media] commons search HTTP {resp.status_code}")
                continue
            pages = ((resp.json().get("query") or {}).get("pages") or {})
            _log(f"[media] search {q!r} pages={len(pages)}")
        except Exception as exc:  # noqa: BLE001
            _log(f"[media] commons search failed: {exc}")
            continue
        for page in pages.values():
            ft = str(page.get("title") or "").strip()
            if not ft or ft in seen:
                continue
            if _PHOTO_TITLE_RE.search(ft):
                continue
            low = ft.lower()
            if low.endswith((".pdf", ".djvu", ".tiff", ".tif", ".webp", ".ogv", ".ogg", ".webm")):
                continue
            infos = page.get("imageinfo") or []
            if not infos:
                continue
            info = infos[0]
            mime = str(info.get("mime") or "")
            direct = _pick_direct_url(info, mime)
            if not direct:
                continue
            seen.add(ft)
            pretty = ft.replace("File:", "").rsplit(".", 1)[0].replace("_", " ")
            kind = "gif" if mime == "image/gif" or low.endswith(".gif") else "image"
            out.append({
                "id": f"commons-{len(out)+1}",
                "kind": kind,
                "sectionKey": section,
                "title": pretty,
                "caption": f"Teaching figure from Wikimedia Commons related to {title}.",
                "alt": pretty,
                "fileTitle": ft if ft.lower().startswith("file:") else f"File:{ft}",
                "proposedUrl": direct,
                "sourceName": "Wikimedia Commons",
                "license": "CC BY-SA 4.0",
                "attribution": "Wikimedia Commons contributors",
                "relevance": q,
            })
            if len(out) >= 8:
                return out
    return out


def _pick_direct_url(info: dict, mime: str) -> str | None:
    raw = str(info.get("url") or "")
    thumb = str(info.get("thumburl") or "")
    if mime in _IMAGE_MIMES and raw.startswith("https://"):
        return _clean_wm_url(raw)
    if mime in _SVG_MIMES and thumb.startswith("https://"):
        return _clean_wm_url(thumb)
    if thumb.startswith("https://") and mime in _IMAGE_MIMES:
        return _clean_wm_url(thumb)
    return None


def _clean_wm_url(url: str) -> str:
    parsed = urlparse(url)
    return parsed._replace(query="", fragment="").geturl()


def _file_title_from_url(url: str) -> str:
    if not url:
        return ""
    decoded = unquote(url)
    m = _FILE_TITLE_RE.search(decoded)
    if m:
        return "File:" + m.group(1).replace("_", " ").strip()
    if "upload.wikimedia.org" in decoded:
        name = decoded.rstrip("/").split("/")[-1].split("?")[0]
        if name and "." in name:
            return "File:" + name.replace("_", " ")
    return ""


def _resolve_commons_file(url: str, file_title: str, last_wm: float) -> tuple[str | None, float]:
    title = (file_title or "").strip()
    if not title:
        title = _file_title_from_url(url)
    if not title:
        return None, last_wm
    if not title.lower().startswith("file:"):
        title = "File:" + title
    wait = WIKIMEDIA_SLEEP_SEC - (time.time() - last_wm)
    if wait > 0:
        time.sleep(wait)
    try:
        resp = httpx.get(
            COMMONS_API,
            params={
                "action": "query",
                "titles": title,
                "prop": "imageinfo",
                "iiprop": "url|mime|size",
                "iiurlwidth": 1280,
                "format": "json",
            },
            headers={"User-Agent": USER_AGENT},
            timeout=20.0,
        )
        last_wm = time.time()
        if resp.status_code == 429:
            _log("[media] commons imageinfo 429")
            return None, last_wm
        if resp.status_code >= 400:
            return None, last_wm
        pages = ((resp.json().get("query") or {}).get("pages") or {})
        for page in pages.values():
            if page.get("missing") is not None:
                continue
            infos = page.get("imageinfo") or []
            if not infos:
                continue
            info = infos[0]
            mime = str(info.get("mime") or "")
            direct = _pick_direct_url(info, mime)
            if direct:
                return direct, last_wm
    except Exception as exc:  # noqa: BLE001
        _log(f"[media] commons imageinfo failed: {exc}")
    return None, last_wm


def _validate_video_url(url: str) -> tuple[str | None, str | None]:
    """Return (watch_url, poster_url) or (None, None) if the video does not exist."""
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        return None, None
    host = parsed.hostname.lower()
    if host not in VIDEO_HOSTS:
        return None, None
    oembed = None
    if "youtu" in host:
        oembed = "https://www.youtube.com/oembed"
    elif "vimeo" in host:
        oembed = "https://vimeo.com/api/oembed.json"
    if not oembed:
        return None, None
    try:
        resp = httpx.get(
            oembed,
            params={"url": url, "format": "json"},
            headers={"User-Agent": USER_AGENT},
            timeout=15.0,
            follow_redirects=True,
        )
        _log(f"[media] oembed {resp.status_code} for {url[:80]}")
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, dict) and data.get("type"):
                poster = data.get("thumbnail_url")
                poster_s = poster if isinstance(poster, str) and poster.startswith("https://") else None
                return url, poster_s
        if resp.status_code in {401, 403, 429} and "youtu" in host:
            yt_id = _youtube_id(url)
            if yt_id and _youtube_thumb_exists(yt_id):
                return url, f"https://img.youtube.com/vi/{yt_id}/hqdefault.jpg"
    except Exception as exc:  # noqa: BLE001
        _log(f"[media] oembed error: {exc}")
    return None, None


def _youtube_id(url: str) -> str | None:
    m = _YT_ID_RE.search(url)
    return m.group(1) if m else None


def _youtube_thumb_exists(video_id: str) -> bool:
    try:
        resp = httpx.get(
            f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg",
            headers={"User-Agent": USER_AGENT},
            timeout=10.0,
            follow_redirects=True,
        )
        if resp.status_code != 200:
            return False
        raw = resp.content or b""
        return len(raw) > 2000 and raw.startswith(b"\xff\xd8\xff")
    except Exception:
        return False


def _fetch_image(url: str, allowlist: tuple[str, ...], max_bytes: int, last_wm: float) -> dict | None:
    current = url
    for _hop in range(4):
        if not _ssrf_ok(current, allowlist):
            _log(f"[media] SSRF/allowlist reject: {current[:160]}")
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
                        _log("[media] 429 — skip item")
                        return None
                    if resp.status_code >= 400:
                        _log(f"[media] fetch HTTP {resp.status_code}")
                        return None
                    cl = resp.headers.get("content-length")
                    if cl and cl.isdigit() and int(cl) > max_bytes:
                        return None
                    buf = bytearray()
                    for chunk in resp.iter_bytes():
                        buf.extend(chunk)
                        if len(buf) > max_bytes:
                            return None
        except httpx.HTTPError as exc:
            _log(f"[media] fetch error: {exc}")
            return None
        mime = _magic_mime(bytes(buf))
        if mime is None:
            _log("[media] magic-bytes reject (not png/jpeg/gif)")
            return None
        return {"bytes": bytes(buf), "mime": mime}
    return None


def _store_via_laravel(
    media_url: str,
    job_id: str,
    topic_id: str,
    media_id: str,
    raw: bytes,
    mime: str,
    host_header: str | None,
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
    if host_header:
        headers["Host"] = host_header
    try:
        resp = httpx.post(
            media_url,
            content=body.encode("utf-8"),
            headers=headers,
            timeout=60.0,
            follow_redirects=False,
        )
        if resp.status_code in {301, 302, 303, 307, 308}:
            loc = resp.headers.get("location") or ""
            _log(f"[media] ingest redirect {resp.status_code} -> {loc[:160]}")
            return None
        if resp.status_code >= 400:
            _log(f"[media] ingest HTTP {resp.status_code}: {resp.text[:300]}")
            return None
        try:
            data = resp.json()
        except Exception:
            _log(f"[media] ingest non-json HTTP {resp.status_code}: {resp.text[:200]!r}")
            return None
        url = ((data.get("data") or {}) if isinstance(data, dict) else {}).get("url")
        if isinstance(url, str) and url.startswith(("https://", "http://")):
            _log(f"[media] ingest ok {url[:120]}")
            return url
        _log(f"[media] ingest missing url in {str(data)[:200]}")
    except Exception as exc:  # noqa: BLE001
        _log(f"[media] ingest failed: {exc}")
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


def _media_ingest_target(callback_url: str) -> tuple[str, str | None]:
    """Same host/scheme as the working topic callback — loopback HTTP 301s to HTML."""
    parsed = urlparse(callback_url)
    path = parsed.path or ""
    if path.endswith("/callback"):
        path = path[: -len("/callback")] + "/media"
    else:
        path = path.rstrip("/") + "/media"
    return parsed._replace(path=path, query="", fragment="").geturl(), None
