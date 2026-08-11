"""
pipeline.py — the server-side v5 enrichment pipeline.

Flow for one job:
  research  → Crossref reference discovery + per-DOI re-verification
  generate  → LLM section-group generation (JSON mode), citing only real refs
              (provider/model/key from job["llm"], else Gemini env fallback)
  assemble  → merge groups into one v5 topic dict (contentStandard "v5":
              doseSpec on every drugRegimens entry, drugInteractionFlags,
              no worker-authored cross-topic links)
  validate  → bundled vendor/validate_topic.py as a subprocess (gate)
  repair    → feed validator errors back to the LLM, up to MAX_REPAIR_PASSES (default 3)
  bypass    → if still failing after max repairs: ingest anyway for board review
              (KH_WORKER_VALIDATOR_BYPASS=1, default on) with bypass flag on report
  media     → v6 only: propose + SSRF-safe fetch + Laravel store (never fails text)
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

from . import llm, media, security

# --- Paths --------------------------------------------------------------------
WORKER_ROOT = Path(__file__).resolve().parent.parent
VALIDATOR = WORKER_ROOT / "vendor" / "validate_topic.py"

# --- Config from env (fallback when job has no llm block) ---------------------
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("KH_WORKER_GEMINI_MODEL", "gemini-2.5-flash")
CROSSREF_MAILTO = os.environ.get("KH_WORKER_CROSSREF_MAILTO", "dev@doctorshero.com")
USER_AGENT = f"DoctorsHero-KH-Worker/1.0 (+https://doctorshero.com; mailto:{CROSSREF_MAILTO})"
MAX_REPAIR_PASSES = int(os.environ.get("KH_WORKER_MAX_REPAIR_PASSES", "3") or "3")
# After all repair passes still fail validation, ingest anyway for human review
# instead of failing the job. Set KH_WORKER_VALIDATOR_BYPASS=0 to disable.
VALIDATOR_BYPASS_AFTER_REPAIRS = os.environ.get(
    "KH_WORKER_VALIDATOR_BYPASS", "1"
).strip().lower() not in ("0", "false", "no", "off")

# Default pricing (overridden per-job from llm.input/output_usd_per_m).
INPUT_USD_PER_M = float(os.environ.get("KH_WORKER_GEMINI_INPUT_USD_PER_M", "0.15") or "0.15")
OUTPUT_USD_PER_M = float(os.environ.get("KH_WORKER_GEMINI_OUTPUT_USD_PER_M", "0.60") or "0.60")


class UsageTracker:
    """Accumulate LLM token usage for one enrichment job."""

    def __init__(self, cfg: "llm.LlmConfig | None" = None) -> None:
        self.prompt_tokens = 0
        self.output_tokens = 0
        self.total_tokens = 0
        self.calls = 0
        self.cfg = cfg
        self.primary_refused = False
        self.fallback_used = False

    def add(self, usage: dict | None) -> None:
        if not usage:
            return
        prompt = int(usage.get("promptTokenCount") or usage.get("prompt_tokens") or 0)
        output = int(usage.get("candidatesTokenCount") or usage.get("output_tokens") or 0)
        total = int(usage.get("totalTokenCount") or (prompt + output))
        self.prompt_tokens += prompt
        self.output_tokens += output
        self.total_tokens += total
        self.calls += 1

    def mark_primary_refused(self) -> None:
        self.primary_refused = True

    def mark_fallback_used(self) -> None:
        self.fallback_used = True

    @property
    def cost_usd(self) -> float:
        in_rate = self.cfg.input_usd_per_m if self.cfg else INPUT_USD_PER_M
        out_rate = self.cfg.output_usd_per_m if self.cfg else OUTPUT_USD_PER_M
        return round(
            (self.prompt_tokens / 1_000_000.0) * in_rate
            + (self.output_tokens / 1_000_000.0) * out_rate,
            6,
        )

    def as_dict(self) -> dict:
        model = self.cfg.model if self.cfg else GEMINI_MODEL
        provider = self.cfg.provider if self.cfg else "gemini"
        in_rate = self.cfg.input_usd_per_m if self.cfg else INPUT_USD_PER_M
        out_rate = self.cfg.output_usd_per_m if self.cfg else OUTPUT_USD_PER_M
        return {
            "prompt_tokens": self.prompt_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "gemini_calls": self.calls,
            "llm_calls": self.calls,
            "cost_usd": self.cost_usd,
            "provider": provider,
            "model": model,
            "primary_refused": self.primary_refused,
            "fallback_used": self.fallback_used,
            "pricing": {
                "input_usd_per_million": in_rate,
                "output_usd_per_million": out_rate,
            },
        }


# Thread-local-ish: set per run_pipeline invocation.
_CURRENT_USAGE: UsageTracker | None = None
_CURRENT_LLM: "llm.LlmConfig | None" = None
_CURRENT_FALLBACK: "llm.LlmConfig | None" = None
# Admin-editable v5 rules from Laravel (job["llm"]["v5_prompt"]); None → V5_RULES.
_CURRENT_V5_RULES: str | None = None


def estimate_enrichment_cost() -> dict:
    """Pre-flight estimate for admin UI (section groups + typical repair passes)."""
    groups = 5  # keep in sync with GROUPS length below
    est_calls = groups + max(1, MAX_REPAIR_PASSES // 2)
    # Empirical averages from production runs (~17 min / topic).
    est_prompt = int(os.environ.get("KH_WORKER_EST_PROMPT_TOKENS", "90000") or "90000")
    est_output = int(os.environ.get("KH_WORKER_EST_OUTPUT_TOKENS", "140000") or "140000")
    cost = round(
        (est_prompt / 1_000_000.0) * INPUT_USD_PER_M
        + (est_output / 1_000_000.0) * OUTPUT_USD_PER_M,
        4,
    )
    return {
        "estimated_prompt_tokens": est_prompt,
        "estimated_output_tokens": est_output,
        "estimated_gemini_calls": est_calls,
        "estimated_llm_calls": est_calls,
        "estimated_cost_usd": cost,
        "estimated_cost_usd_min": round(cost * 0.6, 4),
        "estimated_cost_usd_max": round(cost * 1.8, 4),
        "provider": "gemini",
        "model": GEMINI_MODEL,
        "currency": "USD",
        "note": "Worker env fallback estimate. Admin UI prefers Laravel kh_llm_settings rates.",
        "pricing": {
            "input_usd_per_million": INPUT_USD_PER_M,
            "output_usd_per_million": OUTPUT_USD_PER_M,
        },
    }


# =============================================================================
# The condensed v5 authoring rules embedded into every generation prompt.
# Distilled from tools/tier1_enhance/prompts/master_topic_prompt.md plus the
# v5 additions (skills/medical-content-quality-framework Part M): structured
# doseSpec on every drug regimen, topic-level drugInteractionFlags, and
# contentStandard "v5" (which makes every v4 gate a hard error too).
# =============================================================================
V5_RULES = """\
You are a senior clinical editorial physician authoring a Tier-1 reference for the
DoctorsHero RX Clinical Knowledge Hub (Bangladesh EMR). Quality bar: more
decision-dense, current, traceable and machine-actionable than UpToDate/BMJ Best
Practice. Every section must carry something a doctor could not recall precisely
(a threshold, a number+CI, a titration step, a stop rule).

This topic is authored to contentStandard "v5" — every v4 rule below is a HARD
error, plus the v5 structured-dosing rules at the end.

HARD RULES (v4 base):
- Output STRICT JSON only, matching the requested schema keys EXACTLY. No prose
  outside the JSON. No markdown fences.
- NO fabrication. Cite ONLY the numbered references supplied to you, using inline
  Vancouver markers [N], [N,M], [N-M]. Never invent a refId or a DOI.
- referenceStyle is "vancouver": every evidence-claiming sentence carries an inline
  [N] whose number is a supplied refId. Cite at the sentence making the claim.
- Every managementSections and diagnosisSections ContentBlock MUST contain >=1 [N].
- NO templated/counter padding ("item 1..N", "Day N", "Week N Expectations",
  "micro-message N", "lorem ipsum", "as an ai model", "placeholder"). If content
  cannot be genuinely distinct, make it shorter — never pad.
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

VALIDATOR HARD FLOORS (validate_topic.py — failing ANY of these fails the job):
- Whole-topic JSON serialized length MUST be >= 100,000 characters.
- references[]: every entry needs citation + url starting with https://doi.org/...;
  each needs a unique positive integer refId; EVERY refId MUST appear as [N] in main text;
  every [N] in text MUST match a refId (no dangling citations).
- At least one society guideline family in references (e.g. AAP/AHA/ESC/NICE/WHO/IDSA).
- differentialDiagnosis >= 3 entries with condition + distinguishingFeature.
- prognosisQuantitative >= 5 entries with outcome + numeric estimate.
- preciseDosing >= 2 entries, ALL 8 fields non-empty on each.
- drugRegimens >= 6 entries; ALL 10 fields non-empty on each (incl. non-empty genericKeys[]).
- treatmentLines >= 4 entries; each description >= 200 chars.
- managementSections >= 5 blocks; each block >= 4 points; each point text >= 150 chars.
- Combined management text (managementSections + treatmentLines + drugRegimens) >= 30,000 chars.
- comorbidityManagement and complicationManagement: non-empty lists of {heading, detail}
  with unique specific headings (no duplicate/generic placeholders).
- Vancouver [N] required in EVERY managementSections / diagnosisSections ContentBlock.
- Aggregate sections that claim evidence (summary, etiology, pathophys, presentation,
  workup, treatmentLines, monitoring, complications, relapse, comorbidity/complication
  management) should carry [N] markers where claims are made.
- ContentBlock headings must NOT be artificial numbered labels ("Item 1", "Day 3", etc.).

V5 STRUCTURED DOSING (hard errors — validate_topic.py v5 gates):
- EVERY drugRegimens entry MUST also carry a "doseSpec" object:
    {"amount": <positive number>, "unit": "mg"|"mcg"|"g"|"mL"|"units"|...,
     "route": "PO"|"IV"|"IM"|"SC"|..., "frequency": "OD"|"BD"|"TDS"|"q8h"|...,
     "durationDays": <number of days, or null ONLY if genuinely open-ended
       (e.g. lifelong therapy) — the KEY must always be present>,
     "maxDosePerDay": "<non-empty string, e.g. '40 mg/day'>",
     "taperSchedule": "<taper description string>" or null,
     "renalAdjustment": [{"egfrBand": "<e.g. eGFR 30-59>", "action": "<what to do>"}, ...]
       (empty array [] allowed ONLY when the drug truly needs no renal adjustment),
     "hepaticAdjustment": [{"severityBand": "<e.g. Child-Pugh B>", "action": "<what to do>"}, ...]
       (empty array [] allowed ONLY when no hepatic adjustment is needed),
     "genericKey": "<bare lowercase generic name WITHOUT salt, e.g. 'warfarin',
       'amlodipine', 'metformin' — must be a real generic marketed in Bangladesh>",
     "refIds": [<integer refIds from the supplied reference list backing this dose>]}
  doseSpec.amount is the starting/standard single dose amount matching initialDose.
- The topic MUST carry a top-level "drugInteractionFlags" array (>=4 entries) of:
    {"type": "drug-drug"|"drug-disease"|"drug-pregnancy"|"drug-lactation"|
       "drug-renal"|"drug-hepatic",
     "subject": "<the interacting drug/condition>",
     "action": "<specific clinical action: avoid / adjust dose / monitor X>",
     "severity": "contraindicated"|"major"|"moderate"|"minor",
     "refIds": [<integer refIds>]}
  covering the clinically important interactions of the drugs you listed.
- Do NOT output crossReferences or relatedTopicIds — cross-topic links are added
  later against the deployed corpus index; unresolvable links fail validation.
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
            "titration, maintenanceDose, termination, alternatives, adverseEffectManagement, monitoring, genericKeys:[], "
            "AND a doseSpec object exactly as specified in the V5 STRUCTURED DOSING rules — amount, unit, route, "
            "frequency, durationDays (key always present; null only if open-ended), maxDosePerDay, taperSchedule, "
            "renalAdjustment:[{egfrBand,action}], hepaticAdjustment:[{severityBand,action}], genericKey (bare "
            "lowercase generic, no salt), refIds:[int]),\n"
            "  preciseDosing (array of >=4 objects, EACH with all 8 fields non-empty: drug, indication, standardDose, "
            "doseReductionCriteria, renalAdjustment, hepaticAdjustment, administration, onsetOffset),\n"
            "  drugInteractionFlags (array of >=4 objects {type, subject, action, severity, refIds:[int]} per the "
            "V5 rules, covering the clinically important interactions of the drugs above)."
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
            "  complicationManagement (array of >=5 objects {heading, detail} with UNIQUE specific headings and [N]).\n"
            "Do NOT output crossReferences or relatedTopicIds."
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
    payload = {
        "job_id": job_id,
        "topic_id": topic_id,
        "status": "progress",
        "stage": stage,
        "progress": progress,
    }
    if _CURRENT_USAGE is not None:
        payload["usage"] = _CURRENT_USAGE.as_dict()
    _post_signed(callback_url, payload)


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
# Generation stage — multi-provider LLM (see llm.py)
# =============================================================================
def _llm_generate(prompt: str, retries: int = 5) -> str:
    if _CURRENT_LLM is None:
        raise RuntimeError("LLM config not set for this job")
    return llm.generate_json_with_fallback(
        prompt,
        _CURRENT_LLM,
        _CURRENT_FALLBACK,
        _CURRENT_USAGE,
        retries=retries,
    )


def _active_v5_rules() -> str:
    """Prefer admin-supplied prompt from the job payload; else builtin V5_RULES."""
    custom = (_CURRENT_V5_RULES or "").strip()
    return custom if custom else V5_RULES


def _references_block(references: list[dict]) -> str:
    lines = [f"[{r['refId']}] {r['citation']} {r['url']}" for r in references]
    return "\n".join(lines)


def _build_prompt(group: dict, title: str, specialty: str, chapter: str,
                  references: list[dict], repair_note: str = "") -> str:
    return (
        f"{_active_v5_rules()}\n\n"
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
        raw = _llm_generate(prompt)
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
        "contentStandard": "v5",
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
    topic["contentStandard"] = "v5"
    topic["referenceStyle"] = "vancouver"
    topic["agentGenerated"] = True
    topic["references"] = references
    # The worker cannot see the deployed corpus, so it must never author
    # cross-topic links: any unresolvable entry is a hard v5 error.
    topic["crossReferences"] = []
    topic["relatedTopicIds"] = []
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
            report: dict, callback_url: str, job_id: str, topic_id: str,
            pass_num: int = 1) -> dict:
    """Regenerate ALL groups with the validator errors fed back in. Simpler and
    more robust than trying to map each error to a single group; the model is
    told exactly what failed and to fix it while keeping everything else valid."""
    final = pass_num >= MAX_REPAIR_PASSES
    urgency = (
        f"FINAL repair pass ({pass_num}/{MAX_REPAIR_PASSES}). Prioritize fixing "
        "EVERY listed error exactly — expand thin sections, add missing fields, "
        "cite every refId as [N], and hit the density floors.\n\n"
        if final
        else f"Repair pass {pass_num}/{MAX_REPAIR_PASSES}.\n\n"
    )
    error_lines = report.get("error_list") or []
    error_note = (
        urgency
        + "The previous draft FAILED validate_topic.py with these HARD errors — "
        "fix ALL of them in this regeneration (do not invent new refIds):\n- "
        + "\n- ".join(error_lines)
        + "\n\n"
    )
    merged: dict = {}
    for group in GROUPS:
        prompt = _build_prompt(group, title, specialty, chapter, references, repair_note=error_note)
        raw = _llm_generate(prompt)
        merged.update(_extract_json(raw))
    return merged


# =============================================================================
# Orchestrator
# =============================================================================
def run_pipeline(job: dict, on_done=None) -> None:
    """Execute the full pipeline for one job dict:
    {job_id, topic_id, title, specialty, chapter, callback_url, llm?}."""
    global _CURRENT_USAGE, _CURRENT_LLM, _CURRENT_FALLBACK, _CURRENT_V5_RULES
    job_id = job["job_id"]
    topic_id = job["topic_id"]
    title = job["title"]
    specialty = job.get("specialty", "")
    chapter = job.get("chapter", specialty)
    callback_url = job["callback_url"]
    llm_block = job.get("llm") if isinstance(job.get("llm"), dict) else {}
    custom_rules = str((llm_block or {}).get("v5_prompt") or "").strip()
    _CURRENT_V5_RULES = custom_rules or None
    try:
        _CURRENT_LLM = llm.LlmConfig.from_job(job)
        _CURRENT_FALLBACK = llm.LlmConfig.fallback_from_job(job)
    except Exception as exc:
        _CURRENT_LLM = None
        _CURRENT_FALLBACK = None
        _CURRENT_V5_RULES = None
        _CURRENT_USAGE = UsageTracker()
        _post_signed(
            callback_url,
            {
                "job_id": job_id,
                "topic_id": topic_id,
                "status": "failed",
                "error": f"invalid LLM config: {exc}",
                "validator_report": None,
                "usage": _CURRENT_USAGE.as_dict(),
            },
        )
        if on_done:
            on_done(job_id)
        return

    _CURRENT_USAGE = UsageTracker(_CURRENT_LLM)

    def usage_payload() -> dict:
        return _CURRENT_USAGE.as_dict() if _CURRENT_USAGE else {}

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
                    "usage": usage_payload(),
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
            groups = _repair(
                title, specialty, chapter, references, report,
                callback_url, job_id, topic_id, pass_num=passes,
            )
            topic = assemble_topic(topic_id, title, specialty, chapter, references, groups)
            report = run_validator(topic)

        # After 2–3 repair passes, if still failing: accept draft for human review
        # rather than burning another cycle. Admin sees bypass flag + remaining errors.
        bypassed = False
        if not report["passed"]:
            if VALIDATOR_BYPASS_AFTER_REPAIRS and passes >= MAX_REPAIR_PASSES:
                bypassed = True
                err_preview = "; ".join((report.get("error_list") or [])[:5])
                report = {
                    **report,
                    "passed": False,
                    "bypassed": True,
                    "bypass_after_repairs": passes,
                    "bypass_reason": (
                        f"Validator still failing after {passes} repair pass(es) "
                        f"({report.get('errors', 0)} error(s)); accepted for board review."
                    ),
                    "error_preview": err_preview,
                }
                topic["validatorBypassed"] = True
                topic["validatorBypassNote"] = report["bypass_reason"]
                print(
                    f"[pipeline] job {job_id}: validator bypass after {passes} repairs "
                    f"— {report.get('errors')} remaining error(s)",
                    file=sys.stderr,
                )
            else:
                err_lines = report.get("error_list") or []
                summary = (
                    f"{report.get('errors', 0)} validator error(s) after {passes} "
                    f"repair pass(es)"
                )
                if err_lines:
                    summary += ": " + "; ".join(err_lines[:8])
                _post_signed(
                    callback_url,
                    {
                        "job_id": job_id,
                        "topic_id": topic_id,
                        "status": "failed",
                        "error": summary[:4000],
                        "validator_report": report,
                        "usage": usage_payload(),
                    },
                )
                return

        topic = media.attach_media(topic, job, callback_url, job_id, topic_id)

        _post_signed(
            callback_url,
            {
                "job_id": job_id,
                "topic_id": topic_id,
                "status": "completed",
                "progress": 100,
                "stage": "done_validator_bypassed" if bypassed else "done",
                "topic": topic,
                "validator_report": report,
                "usage": usage_payload(),
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
                "usage": usage_payload(),
            },
        )
    finally:
        _CURRENT_USAGE = None
        _CURRENT_LLM = None
        _CURRENT_FALLBACK = None
        if on_done:
            on_done(job_id)
