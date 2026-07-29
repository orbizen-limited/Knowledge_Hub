"""
pipeline.py — the server-side v4 enrichment pipeline.

Flow for one job:
  research  → Crossref reference discovery + per-DOI re-verification
  generate  → Gemini section-group generation (JSON mode), citing only real refs
  assemble  → merge groups into one v4 topic dict
  validate  → bundled vendor/validate_topic.py as a subprocess (gate)
  repair    → feed validator errors back to Gemini, up to 3 passes
  callback  → signed POST of progress / completed / failed to callback_url

Every network stage emits a signed progress callback. All callbacks (progress,
completed, failed) are signed exactly like ServiceSignature (see security.py):
JWT iss=kh-worker aud=doctorshero-backend + HMAC over the raw body.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx

from . import security

# --- Paths --------------------------------------------------------------------
WORKER_ROOT = Path(__file__).resolve().parent.parent
VALIDATOR = WORKER_ROOT / "vendor" / "validate_topic.py"

# --- Config from env ----------------------------------------------------------
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("KH_WORKER_GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
CROSSREF_MAILTO = os.environ.get("KH_WORKER_CROSSREF_MAILTO", "dev@doctorshero.com")
USER_AGENT = f"DoctorsHero-KH-Worker/1.0 (+https://doctorshero.com; mailto:{CROSSREF_MAILTO})"
MAX_REPAIR_PASSES = int(os.environ.get("KH_WORKER_MAX_REPAIR_PASSES", "3") or "3")


# =============================================================================
# The condensed v4 authoring rules embedded into every generation prompt.
# Distilled from tools/tier1_enhance/prompts/master_topic_prompt.md.
# =============================================================================
V4_RULES = """\
You are a senior clinical editorial physician authoring a Tier-1 reference for the
DoctorsHero RX Clinical Knowledge Hub (Bangladesh EMR). Quality bar: more
decision-dense, current, traceable and machine-actionable than UpToDate/BMJ Best
Practice. Every section must carry something a doctor could not recall precisely
(a threshold, a number+CI, a titration step, a stop rule).

HARD RULES (v4):
- Output STRICT JSON only, matching the requested schema keys EXACTLY. No prose
  outside the JSON. No markdown fences.
- NO fabrication. Cite ONLY the numbered references supplied to you, using inline
  Vancouver markers [N], [N,M], [N-M]. Never invent a refId or a DOI.
- referenceStyle is "vancouver": every evidence-claiming sentence carries an inline
  [N] whose number is a supplied refId. Cite at the sentence making the claim.
- Every managementSections and diagnosisSections ContentBlock MUST contain >=1 [N].
- NO templated/counter padding ("item 1..N", "Day N", "Week N Expectations",
  "micro-message N"). If content cannot be genuinely distinct, make it shorter.
- GRADE every actionable recommendation (grade A/B/C + evidenceLevel + source year).
- Cover the 7 mandatory special populations: pregnancy/lactation, paediatric,
  elderly/frail, renal impairment, hepatic impairment, resource-limited (Bangladesh),
  multimorbid/polypharmacy (with deprescribing).
- Every drug regimen states its STOP/taper/rebound rule, not just how to start.
- ContentPoint objects are {"text": "...", "level": 0}. Points arrays must be non-empty.
- comorbidityManagement / complicationManagement are lists of {"heading","detail"} with
  UNIQUE specific clinical headings (never generic "Management").
- treatmentLines entries are {"line","description","medicineGenericKeys":[]} with
  description >=200 chars.
- managementSections >=5 indication-wise ContentBlocks, each >=4 points of >=150 chars.
- drugRegimens entries need ALL keys non-empty: drug, indication, initialDose,
  titration, maintenanceDose, termination, alternatives, adverseEffectManagement,
  monitoring, genericKeys[].
- preciseDosing entries need ALL 8 fields non-empty: drug, indication, standardDose,
  doseReductionCriteria, renalAdjustment, hepaticAdjustment, administration, onsetOffset.
- prognosisQuantitative entries are {"outcome","estimate","source","doi"} where doi is a
  FULL https://doi.org/... URL taken from the supplied reference list. estimate must be a
  specific numeric statement, never vague.
- differentialDiagnosis entries are {"condition","distinguishingFeature"} with real
  distinguishing features.
"""


# =============================================================================
# Section-group definitions. Each group is a separate Gemini call (to stay
# within output limits) that must return a JSON object with exactly its keys.
# =============================================================================
GROUPS = [
    {
        "name": "core",
        "instruction": (
            "Generate the CORE META group. Return a JSON object with EXACTLY these keys:\n"
            "  bottomLine (string, 3-6 dense sentences with inline [N]),\n"
            "  summaryParagraphs (array of >=4 long paragraphs, each with inline [N]),\n"
            "  keywords (array of 10-25 strings),\n"
            "  careSettings (array from: outpatient, inpatient, resource_limited, paediatric, emergency),\n"
            "  etiologyEpidemiology (array of >=6 paragraphs with [N]),\n"
            "  pathophysiology (array of >=6 paragraphs with [N]),\n"
            "  clinicalPresentation (array of >=8 paragraphs with [N])."
        ),
    },
    {
        "name": "diagnosis",
        "instruction": (
            "Generate the DIAGNOSIS group. Return a JSON object with EXACTLY these keys:\n"
            "  diagnosticWorkup (array of >=6 paragraphs with [N]),\n"
            "  differentialDiagnosis (array of >=8 objects {condition, distinguishingFeature} with [N] in the feature),\n"
            "  diagnosisSections (array of >=4 ContentBlock objects {heading, points:[{text,level}]}; "
            "each block must contain >=1 inline [N])."
        ),
    },
    {
        "name": "management",
        "instruction": (
            "Generate the MANAGEMENT group. Return a JSON object with EXACTLY these keys:\n"
            "  managementSections (array of >=5 ContentBlock objects {heading, points:[{text,level}]}, "
            "organised indication-wise; each block >=4 points of >=150 chars; each block >=1 inline [N]),\n"
            "  treatmentLines (array of >=4 objects {line, description, medicineGenericKeys:[]}; "
            "description >=200 chars with [N]),\n"
            "  recommendations (array of >=8 objects {text, grade:'A'|'B'|'C', source, sourceUrl, evidenceLevel} "
            "with inline [N] in text)."
        ),
    },
    {
        "name": "drugs",
        "instruction": (
            "Generate the DRUGS group. Return a JSON object with EXACTLY these keys:\n"
            "  drugRegimens (array of >=8 objects, EACH with all keys non-empty: drug, indication, initialDose, "
            "titration, maintenanceDose, termination, alternatives, adverseEffectManagement, monitoring, genericKeys:[]),\n"
            "  preciseDosing (array of >=4 objects, EACH with all 8 fields non-empty: drug, indication, standardDose, "
            "doseReductionCriteria, renalAdjustment, hepaticAdjustment, administration, onsetOffset)."
        ),
    },
    {
        "name": "followup",
        "instruction": (
            "Generate the FOLLOW-UP group. Return a JSON object with EXACTLY these keys:\n"
            "  monitoringFollowUp (array of >=6 paragraphs with [N]),\n"
            "  complicationsPrognosis (array of >=6 paragraphs with [N]),\n"
            "  complicationSections (array of >=3 ContentBlock objects {heading, points:[{text,level}]} with [N]),\n"
            "  specialPopulations (array of >=7 objects {population, considerations} covering pregnancy/lactation, "
            "paediatric, elderly, renal, hepatic, resource-limited Bangladesh, multimorbid),\n"
            "  patientEducation (array of >=10 plain-language strings with [N]),\n"
            "  relapseRemission (array of >=6 paragraphs with [N]),\n"
            "  prognosisQuantitative (array of >=10 objects {outcome, estimate, source, doi} where doi is a full "
            "https://doi.org/... URL from the reference list; estimate is a specific numeric statement),\n"
            "  comorbidityManagement (array of >=6 objects {heading, detail} with UNIQUE specific headings and [N]),\n"
            "  complicationManagement (array of >=5 objects {heading, detail} with UNIQUE specific headings and [N]),\n"
            "  crossReferences (array of 4-10 related topic title strings)."
        ),
    },
]


# =============================================================================
# Signed callbacks
# =============================================================================
def _post_signed(callback_url: str, payload: dict, retries: int = 3, timeout: float = 30.0) -> bool:
    body = json.dumps(payload, ensure_ascii=False)
    path = urlparse(callback_url).path or "/"
    headers = security.build_outbound_headers("POST", path, body)
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            resp = httpx.post(callback_url, content=body.encode("utf-8"), headers=headers, timeout=timeout)
            if resp.status_code < 400:
                return True
            # regenerate signature per attempt (timestamp freshness)
            headers = security.build_outbound_headers("POST", path, body)
        except httpx.HTTPError as exc:
            last_exc = exc
            headers = security.build_outbound_headers("POST", path, body)
        time.sleep(min(2 ** attempt, 8))
    if last_exc:
        print(f"[callback] failed after {retries} attempts: {last_exc}", file=sys.stderr)
    return False


def _progress(callback_url: str, job_id: str, topic_id: str, stage: str, progress: int) -> None:
    _post_signed(
        callback_url,
        {
            "job_id": job_id,
            "topic_id": topic_id,
            "status": "progress",
            "stage": stage,
            "progress": progress,
        },
    )


# =============================================================================
# Research stage — Crossref
# =============================================================================
def _crossref_search(query: str, rows: int = 30) -> list[dict]:
    params = {
        "query.bibliographic": query,
        "rows": str(rows),
        "filter": "type:journal-article",
    }
    try:
        resp = httpx.get(
            "https://api.crossref.org/works",
            params=params,
            headers={"User-Agent": USER_AGENT},
            timeout=30.0,
        )
        resp.raise_for_status()
        return resp.json().get("message", {}).get("items", [])
    except httpx.HTTPError as exc:
        print(f"[crossref] search failed for '{query}': {exc}", file=sys.stderr)
        return []


def _verify_doi(doi: str) -> dict | None:
    """Re-verify a DOI directly against Crossref; drop on any failure."""
    try:
        resp = httpx.get(
            f"https://api.crossref.org/works/{doi}",
            headers={"User-Agent": USER_AGENT},
            timeout=20.0,
        )
        if resp.status_code != 200:
            return None
        return resp.json().get("message")
    except httpx.HTTPError:
        return None


def _year_of(item: dict) -> int | None:
    for key in ("published-print", "published-online", "issued", "created"):
        parts = item.get(key, {}).get("date-parts", [[None]])
        if parts and parts[0] and parts[0][0]:
            try:
                return int(parts[0][0])
            except (TypeError, ValueError):
                continue
    return None


def _authors_of(item: dict) -> str:
    authors = item.get("author", []) or []
    names = []
    for a in authors[:6]:
        family = a.get("family", "")
        given = a.get("given", "")
        initials = "".join(p[0] for p in given.split() if p) if given else ""
        names.append(f"{family} {initials}".strip())
    if len(authors) > 6:
        names.append("et al.")
    return ", ".join(n for n in names if n)


def _vancouver(item: dict) -> str:
    authors = _authors_of(item)
    title = (item.get("title") or [""])[0]
    container = (item.get("container-title") or [""])[0]
    year = _year_of(item) or ""
    vol = item.get("volume", "")
    page = item.get("page", "")
    bits = [b for b in [authors, title, container] if b]
    tail = str(year)
    if vol:
        tail += f";{vol}"
    if page:
        tail += f":{page}"
    citation = ". ".join(bits)
    if tail:
        citation += f". {tail}."
    return citation.strip()


def research_references(title: str, specialty: str, chapter: str) -> list[dict]:
    """Discover, score, dedupe and DOI-verify references. Returns the numbered
    reference list [{refId, citation, url}] plus discovery metadata used by the
    prompt (title/container/year kept internally for scoring)."""
    queries = [
        f"{title} {specialty}",
        f"{title} guideline recommendations",
        f"{title} {specialty} management",
        f"{title} diagnosis treatment review",
    ]
    seen_doi: dict[str, dict] = {}
    for q in queries:
        for item in _crossref_search(q, rows=30):
            doi = (item.get("DOI") or "").lower().strip()
            if not doi or doi in seen_doi:
                continue
            seen_doi[doi] = item

    def score(item: dict) -> tuple:
        year = _year_of(item) or 0
        typ = " ".join(item.get("type", "").split("-"))
        title_l = " ".join((item.get("title") or [""])[0].lower().split())
        is_review = "review" in title_l or "review" in typ
        is_guideline = any(k in title_l for k in ("guideline", "recommendation", "consensus", "statement"))
        recent = year >= 2015
        return (is_guideline, is_review, recent, year)

    candidates = sorted(seen_doi.values(), key=score, reverse=True)

    references: list[dict] = []
    ref_id = 1
    for item in candidates:
        if len(references) >= 36:
            break
        doi = (item.get("DOI") or "").strip()
        verified = _verify_doi(doi)
        if not verified:
            continue  # drop unverifiable DOIs
        citation = _vancouver(verified)
        if not citation:
            continue
        references.append(
            {
                "refId": ref_id,
                "citation": citation,
                "url": f"https://doi.org/{doi}",
            }
        )
        ref_id += 1
        if len(references) >= 24 and ref_id > 30 and score(item)[3] < 2015:
            # enough refs collected and dropping into older material
            break

    return references


# =============================================================================
# Generation stage — Gemini
# =============================================================================
def _gemini_generate(prompt: str, retries: int = 5) -> str:
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is not set")
    url = f"{GEMINI_BASE}/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
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
    for attempt in range(retries):
        try:
            resp = httpx.post(url, json=payload, timeout=180.0)
            if resp.status_code in (429, 500, 502, 503, 504):
                last_err = f"HTTP {resp.status_code}"
                time.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            resp.raise_for_status()
            data = resp.json()
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


def _references_block(references: list[dict]) -> str:
    lines = [f"[{r['refId']}] {r['citation']} {r['url']}" for r in references]
    return "\n".join(lines)


def _build_prompt(group: dict, title: str, specialty: str, chapter: str,
                  references: list[dict], repair_note: str = "") -> str:
    return (
        f"{V4_RULES}\n\n"
        f"TOPIC TITLE: {title}\n"
        f"SPECIALTY: {specialty}\n"
        f"CHAPTER: {chapter}\n\n"
        f"VERIFIED REFERENCES (cite ONLY these by refId as inline [N]):\n"
        f"{_references_block(references)}\n\n"
        f"{group['instruction']}\n\n"
        f"{repair_note}"
        f"Return ONLY the JSON object for this group. No commentary, no code fences."
    )


def _extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    # find first { and last }
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1:
        text = text[start : end + 1]
    return json.loads(text)


def generate_groups(title: str, specialty: str, chapter: str, references: list[dict],
                    callback_url: str, job_id: str, topic_id: str) -> dict:
    merged: dict = {}
    n = len(GROUPS)
    for idx, group in enumerate(GROUPS):
        prompt = _build_prompt(group, title, specialty, chapter, references)
        raw = _gemini_generate(prompt)
        part = _extract_json(raw)
        merged.update(part)
        pct = 30 + int(40 * (idx + 1) / n)  # generation spans ~30-70%
        _progress(callback_url, job_id, topic_id, f"generate:{group['name']}", pct)
    return merged


# =============================================================================
# Assembly
# =============================================================================
def assemble_topic(topic_id: str, title: str, specialty: str, chapter: str,
                   references: list[dict], groups: dict) -> dict:
    topic: dict = {
        "topicId": topic_id,
        "title": title,
        "specialty": specialty,
        "chapter": chapter,
        "category": chapter,
        "tier": "tier1",
        "contentVersion": 2,
        "reviewStatus": 0,
        "referenceStyle": "vancouver",
        "lastUpdated": datetime.now(timezone.utc).date().isoformat(),
        "agentGenerated": True,
        "references": references,
    }
    topic.update(groups)
    # keep metadata keys authoritative even if the model echoed them
    topic["topicId"] = topic_id
    topic["title"] = title
    topic["specialty"] = specialty
    topic["chapter"] = chapter
    topic["tier"] = "tier1"
    topic["contentVersion"] = 2
    topic["reviewStatus"] = 0
    topic["referenceStyle"] = "vancouver"
    topic["agentGenerated"] = True
    topic["references"] = references
    return topic


# =============================================================================
# Validation gate
# =============================================================================
def run_validator(topic: dict) -> dict:
    """Write the topic to a temp file and run vendor/validate_topic.py.
    The bundled validator has NO --json flag; it prints a text report and
    exits 0 (pass) or 1 (errors). We parse its stdout."""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as fh:
        json.dump(topic, fh, ensure_ascii=False)
        tmp_path = fh.name
    try:
        proc = subprocess.run(
            [sys.executable, str(VALIDATOR), tmp_path],
            capture_output=True,
            text=True,
            timeout=120,
        )
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    return _parse_validator_output(proc.stdout, proc.returncode)


def _parse_validator_output(stdout: str, exit_code: int) -> dict:
    errors: list[str] = []
    warnings: list[str] = []
    err_count = 0
    warn_count = 0
    mode = None
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("errors:"):
            mode = "errors"
            try:
                err_count = int(stripped.split(":", 1)[1].strip())
            except ValueError:
                err_count = 0
            continue
        if stripped.startswith("warnings:"):
            mode = "warnings"
            try:
                warn_count = int(stripped.split(":", 1)[1].strip())
            except ValueError:
                warn_count = 0
            continue
        if stripped.startswith("- "):
            (errors if mode == "errors" else warnings).append(stripped[2:])
    return {
        "passed": exit_code == 0 and err_count == 0,
        "exit_code": exit_code,
        "errors": err_count,
        "warnings": warn_count,
        "error_list": errors,
        "warning_list": warnings,
        "raw": stdout,
    }


def _repair(title: str, specialty: str, chapter: str, references: list[dict],
            report: dict, callback_url: str, job_id: str, topic_id: str) -> dict:
    """Regenerate ALL groups with the validator errors fed back in. Simpler and
    more robust than trying to map each error to a single group; the model is
    told exactly what failed and to fix it while keeping everything else valid."""
    error_note = (
        "The previous draft FAILED validation with these errors — fix ALL of them "
        "in this regeneration:\n- " + "\n- ".join(report["error_list"]) + "\n\n"
    )
    merged: dict = {}
    for group in GROUPS:
        prompt = _build_prompt(group, title, specialty, chapter, references, repair_note=error_note)
        raw = _gemini_generate(prompt)
        merged.update(_extract_json(raw))
    return merged


# =============================================================================
# Orchestrator
# =============================================================================
def run_pipeline(job: dict, on_done=None) -> None:
    """Execute the full pipeline for one job dict:
    {job_id, topic_id, title, specialty, chapter, callback_url}."""
    job_id = job["job_id"]
    topic_id = job["topic_id"]
    title = job["title"]
    specialty = job.get("specialty", "")
    chapter = job.get("chapter", specialty)
    callback_url = job["callback_url"]

    try:
        _progress(callback_url, job_id, topic_id, "research", 5)
        references = research_references(title, specialty, chapter)
        if len(references) < 10:
            _post_signed(
                callback_url,
                {
                    "job_id": job_id,
                    "topic_id": topic_id,
                    "status": "failed",
                    "error": f"insufficient verified references ({len(references)} found, need >=10)",
                    "validator_report": None,
                },
            )
            return
        _progress(callback_url, job_id, topic_id, "research:done", 25)

        groups = generate_groups(title, specialty, chapter, references, callback_url, job_id, topic_id)

        _progress(callback_url, job_id, topic_id, "assemble", 72)
        topic = assemble_topic(topic_id, title, specialty, chapter, references, groups)

        _progress(callback_url, job_id, topic_id, "validate", 80)
        report = run_validator(topic)

        passes = 0
        while not report["passed"] and passes < MAX_REPAIR_PASSES:
            passes += 1
            _progress(callback_url, job_id, topic_id, f"repair:{passes}", 82 + passes * 4)
            groups = _repair(title, specialty, chapter, references, report,
                             callback_url, job_id, topic_id)
            topic = assemble_topic(topic_id, title, specialty, chapter, references, groups)
            report = run_validator(topic)

        if not report["passed"]:
            summary = f"{report['errors']} validator error(s) after {passes} repair pass(es)"
            _post_signed(
                callback_url,
                {
                    "job_id": job_id,
                    "topic_id": topic_id,
                    "status": "failed",
                    "error": summary,
                    "validator_report": report,
                },
            )
            return

        _post_signed(
            callback_url,
            {
                "job_id": job_id,
                "topic_id": topic_id,
                "status": "completed",
                "progress": 100,
                "topic": topic,
                "validator_report": report,
            },
        )
    except Exception as exc:  # noqa: BLE001 — final safety net; report and move on
        print(f"[pipeline] job {job_id} crashed: {exc}", file=sys.stderr)
        _post_signed(
            callback_url,
            {
                "job_id": job_id,
                "topic_id": topic_id,
                "status": "failed",
                "error": f"worker exception: {exc}",
                "validator_report": None,
            },
        )
    finally:
        if on_done:
            on_done(job_id)
