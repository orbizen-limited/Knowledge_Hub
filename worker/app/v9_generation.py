"""v9 Knowledge Hub generation — 4-pass MULTI-PASS protocol + validate_topic_v9 gate."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

WORKER_ROOT = Path(__file__).resolve().parent.parent
VALIDATOR_V9 = WORKER_ROOT / "vendor" / "validate_topic_v9.py"
V9_PROMPT_FILE = WORKER_ROOT / "vendor" / "knowledge_hub_prompt_v9.md"

V9_MIN_CHARS = int(os.environ.get("KH_WORKER_V9_MIN_CHARS", "100000") or "100000")
V9_MEDIA_MODE = os.environ.get("KH_WORKER_V9_MEDIA_MODE", "QUERY-ONLY").strip() or "QUERY-ONLY"
V9_REFERENCE_MODE = os.environ.get("KH_WORKER_V9_REFERENCE_MODE", "R-STRICT").strip() or "R-STRICT"

_PROMPT_CACHE: str | None = None

# Condensed worker rules — the full knowledge_hub_prompt_v9.md (~50k chars) is kept
# in vendor/ for admin override (llm.v9_prompt) but must NOT be sent on every pass;
# that blew Gemini context limits and caused HTTP 400 failures.
V9_WORKER_CONDENSED = """\
You are the Senior Clinical Editorial Physician for DoctorsHero RX Clinical Knowledge Hub
(Bangladesh EMR). Author to contentStandard "v9".

OUTPUT DISCIPLINE (absolute):
- Emit one JSON object only. First char { last char }. No markdown fences. No commentary.
- ASCII punctuation only in JSON strings (plain hyphen-minus, straight quotes).
- Never emit treatmentLines or treatment — all therapy lives in managementSections.
- Cite ONLY supplied refIds as inline [N]. Never invent DOIs or refIds.
- If a density floor cannot be met honestly, record it in _selfAudit.failedFloors[] —
  never pad with filler prose or fake citations.

V9 SHAPE:
- MULTI-PASS: emit only this pass's keys plus _merge metadata.
- managementSections: >=7 ContentBlocks in canonical order across passes 2-3.
- drugRegimens: >=6 entries each with full doseSpec object.
- media (pass 4): QUERY-ONLY — proposedUrl null, use searchDirectives.
- crossLinks: [] (worker cannot resolve corpus).
- references: injected by pipeline in pass 4 — do NOT invent references.

DENSITY: points >=200 chars with >=1 numeric where clinical; decision-dense prose.
"""

V9_PASSES = [
    {
        "index": 1,
        "name": "clinical-core",
        "keys": [
            "topicMetadata", "summary", "etiology", "pathophysiology", "presentation",
            "workup", "diagnosisSections", "differentialDiagnosis",
            "diagnosticTestPerformance", "clinicalDecisionRules", "redFlags",
            "pointOfCareFlow",
        ],
        "instruction": (
            "MULTI-PASS Pass 1/4. Emit ONLY the keys listed below under a top-level "
            "'topic' object (nested topic fields, not flat). Include _merge metadata.\n"
            "Keys: topicMetadata, summary, etiology, pathophysiology, presentation, "
            "workup, diagnosisSections, differentialDiagnosis, diagnosticTestPerformance, "
            "clinicalDecisionRules, redFlags, pointOfCareFlow.\n"
            "topicMetadata must set contentStandard to v9, locale BD, status needs-review.\n"
            "All prose fields are dense strings with inline [N] citations. "
            "diagnosisSections use ContentBlock {heading, content:[{text,level}]}.\n"
            "Do NOT emit references, media, managementSections, or _selfAudit in this pass."
        ),
    },
    {
        "index": 2,
        "name": "management-a",
        "keys": ["managementSections", "drugRegimens"],
        "instruction": (
            "MULTI-PASS Pass 2/4. Emit ONLY managementSections blocks 1-4 (first four "
            "canonical blocks in order) and drugRegimens (>=6 complete entries with doseSpec). "
            "Wrap clinical keys under top-level 'topic'. Include _merge metadata.\n"
            "No treatmentLines. Fold all line-of-therapy detail into managementSections.\n"
            "Do NOT restate Pass 1 content. Do NOT emit blocks 5-7 yet."
        ),
    },
    {
        "index": 3,
        "name": "management-b",
        "keys": [
            "managementSections", "preciseDosing", "drugInteractionFlags",
            "comorbidityManagement", "complicationManagement", "prognosisQuantitative",
            "monitoring", "doNotDo", "qualityMeasures",
        ],
        "instruction": (
            "MULTI-PASS Pass 3/4. Emit ONLY managementSections blocks 5-7 (append to the "
            "7-block canonical order started in Pass 2) plus preciseDosing, "
            "drugInteractionFlags, comorbidityManagement, complicationManagement, "
            "prognosisQuantitative, monitoring, doNotDo, qualityMeasures.\n"
            "Wrap under top-level 'topic'. Include _merge metadata.\n"
            "Do NOT restate Pass 1-2 content."
        ),
    },
    {
        "index": 4,
        "name": "extensions",
        "keys": [
            "localeContext", "emrHooks", "patientEducationBundle", "areasOfUncertainty",
            "changesSinceLastUpdate", "expertConsensusClaims", "crossLinks", "media",
            "_selfAudit",
        ],
        "instruction": (
            "MULTI-PASS Pass 4/4. Emit localeContext, emrHooks, patientEducationBundle, "
            "areasOfUncertainty, changesSinceLastUpdate, expertConsensusClaims, crossLinks, "
            "media, and _selfAudit.\n"
            "Wrap clinical keys under top-level 'topic'. Put media at top level (sibling of "
            "topic) as an array. Put _selfAudit at top level.\n"
            "Do NOT emit references — the pipeline injects the verified reference pack.\n"
            f"MEDIA_MODE is {V9_MEDIA_MODE}: each media item must use proposedUrl null and "
            "searchDirectives (QUERY-ONLY) unless you have a fetched-ok verification.\n"
            "crossLinks must be an empty array (worker cannot resolve corpus).\n"
            "_selfAudit must honestly list failedFloors and coverageGaps if any floor unmet."
        ),
    },
]

PASS1_KEYS = frozenset(V9_PASSES[0]["keys"])
PASS2_KEYS = frozenset(V9_PASSES[1]["keys"])
PASS3_KEYS = frozenset(V9_PASSES[2]["keys"])
PASS4_KEYS = frozenset(V9_PASSES[3]["keys"])


def _load_prompt_template() -> str:
    global _PROMPT_CACHE
    if _PROMPT_CACHE is None:
        _PROMPT_CACHE = V9_PROMPT_FILE.read_text(encoding="utf-8")
    return _PROMPT_CACHE


def _references_block(references: list[dict]) -> str:
    lines = [f"[{r['refId']}] {r['citation']} {r['url']}" for r in references]
    return "\n".join(lines)


def _substitute_variables(text: str, ctx: dict[str, str]) -> str:
    out = text
    for key, val in ctx.items():
        out = out.replace(f"{{{{{key}}}}}", val)
    return out


def build_pass_prompt(
    pass_def: dict,
    *,
    title: str,
    topic_id: str,
    specialty: str,
    chapter: str,
    references: list[dict],
    custom_rules: str | None,
    repair_note: str = "",
) -> str:
    today = datetime.now(timezone.utc).date().isoformat()
    variables = {
        "TOPIC_NAME": title,
        "TOPIC_ID": topic_id,
        "SPECIALTY": specialty or chapter,
        "AUDIENCE": "practising physician",
        "LOCALE": "BD",
        "CODE_ICD11": "UNKNOWN",
        "CODE_ICD10": "UNKNOWN",
        "CODE_SNOMED": "UNKNOWN",
        "KNOWLEDGE_ASOF_DATE": today,
        "REFERENCE_MODE": V9_REFERENCE_MODE,
        "REFERENCE_PACK": _references_block(references),
        "GENERATION_MODE": "MULTI-PASS",
        "PASS_INDEX": str(pass_def["index"]),
        "V9_EXTENSIONS": "on",
        "MEDIA_MODE": V9_MEDIA_MODE,
        "MIN_JSON_CHARS": str(V9_MIN_CHARS),
    }
    base = custom_rules.strip() if custom_rules and custom_rules.strip() else V9_WORKER_CONDENSED
    base = _substitute_variables(base, variables)
    return (
        f"{base}\n\n"
        f"--- ACTIVE PASS ---\n"
        f"TOPIC: {title}\n"
        f"TOPIC_ID: {topic_id}\n"
        f"SPECIALTY: {specialty}\n"
        f"CHAPTER: {chapter}\n"
        f"PASS: {pass_def['index']}/4 ({pass_def['name']})\n\n"
        f"VERIFIED REFERENCES (cite ONLY these refIds as inline [N]):\n"
        f"{_references_block(references)}\n\n"
        f"{pass_def['instruction']}\n\n"
        f'Include: "_merge": {{"mode":"partial","passIndex":{pass_def["index"]},'
        f'"passTotal":4,"keysEmitted":{json.dumps(pass_def["keys"])},'
        f'"continuationToken":"{topic_id}#p{pass_def["index"]}"}}\n\n'
        f"{repair_note}"
        "Return ONLY the JSON object for this pass. No commentary, no code fences."
    )


def pass_for_repair_key(key: str) -> set[int]:
    if key == "managementSections":
        return {2, 3}
    if key in PASS1_KEYS or key == "topic":
        return {1}
    if key in PASS2_KEYS:
        return {2}
    if key in PASS3_KEYS:
        return {3}
    if key in PASS4_KEYS or key == "media":
        return {4}
    if key == "references":
        return set()  # worker-injected, not LLM-regenerated
    return {4}


def repair_keys_from_report(report: dict) -> list[str]:
    keys: set[str] = set()
    for f in report.get("findings") or []:
        if f.get("severity") != "ERROR":
            continue
        rk = f.get("repair_key")
        if rk and rk != "references":
            keys.add(rk)
    return sorted(keys)


def generate_passes(
    *,
    llm_generate: Callable[[str], str],
    extract_json: Callable[[str], dict],
    title: str,
    topic_id: str,
    specialty: str,
    chapter: str,
    references: list[dict],
    custom_rules: str | None,
    on_pass_done: Callable[[str, int], None] | None = None,
    repair_note: str = "",
    only_passes: set[int] | None = None,
) -> dict[int, dict]:
    """Run v9 passes 1-4 (or a subset). Returns {pass_index: parsed_json}."""
    out: dict[int, dict] = {}
    for pdef in V9_PASSES:
        idx = pdef["index"]
        if only_passes is not None and idx not in only_passes:
            continue
        prompt = build_pass_prompt(
            pdef,
            title=title,
            topic_id=topic_id,
            specialty=specialty,
            chapter=chapter,
            references=references,
            custom_rules=custom_rules,
            repair_note=repair_note,
        )
        raw = llm_generate(prompt)
        out[idx] = extract_json(raw)
        if on_pass_done:
            on_pass_done(pdef["name"], idx)
    return out


def merge_pass_files(pass_paths: list[str]) -> dict:
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as out_fh:
        merged_path = out_fh.name
    try:
        proc = subprocess.run(
            [
                sys.executable,
                str(VALIDATOR_V9),
                "--merge",
                *pass_paths,
                "-o",
                merged_path,
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"v9 merge failed (exit {proc.returncode}): {proc.stderr or proc.stdout}"
            )
        with open(merged_path, encoding="utf-8") as fh:
            return json.load(fh)
    finally:
        try:
            os.unlink(merged_path)
        except OSError:
            pass


def merge_pass_dicts(passes: dict[int, dict]) -> dict:
    """Write pass dicts to temp files and invoke validator --merge."""
    paths: list[str] = []
    try:
        for idx in sorted(passes):
            fh = tempfile.NamedTemporaryFile(
                "w", suffix=f"-p{idx}.json", delete=False, encoding="utf-8"
            )
            json.dump(passes[idx], fh, ensure_ascii=False)
            fh.close()
            paths.append(fh.name)
        return merge_pass_files(paths)
    finally:
        for p in paths:
            try:
                os.unlink(p)
            except OSError:
                pass


def finalize_document(
    doc: dict,
    *,
    topic_id: str,
    title: str,
    specialty: str,
    chapter: str,
    references: list[dict],
) -> dict:
    """Ensure v9 envelope, inject verified references, stamp ingest-friendly metadata."""
    if "topic" not in doc:
        media_list = doc.pop("media", []) if isinstance(doc.get("media"), list) else []
        audit = doc.pop("_selfAudit", None)
        wrapped: dict[str, Any] = {"topic": doc, "media": media_list}
        if audit is not None:
            wrapped["_selfAudit"] = audit
        doc = wrapped
    if "media" not in doc:
        doc["media"] = []
    if not isinstance(doc["media"], list):
        doc["media"] = []

    topic = doc["topic"]
    meta = topic.get("topicMetadata")
    if not isinstance(meta, dict):
        meta = {}
        topic["topicMetadata"] = meta

    today = datetime.now(timezone.utc).date().isoformat()
    meta.update({
        "topicId": topic_id,
        "topicName": title,
        "specialty": specialty or chapter,
        "contentStandard": "v9",
        "locale": "BD",
        "knowledgeAsOf": meta.get("knowledgeAsOf") or today,
        "status": meta.get("status") or "needs-review",
    })

    # Ingest + GraphQL expect top-level identity fields too.
    topic["topicId"] = topic_id
    topic["title"] = title
    topic["specialty"] = specialty or chapter
    topic["chapter"] = chapter
    topic["category"] = chapter
    topic["tier"] = "tier1"
    topic["contentVersion"] = 2
    topic["reviewStatus"] = 0
    topic["contentStandard"] = "v9"
    topic["referenceStyle"] = "vancouver"
    topic["lastUpdated"] = today
    topic["agentGenerated"] = True
    topic["references"] = references
    topic["crossLinks"] = []
    topic["crossReferences"] = []
    topic["relatedTopicIds"] = []
    topic.pop("treatmentLines", None)

    if V9_MEDIA_MODE == "QUERY-ONLY":
        cleaned_media: list[dict] = []
        for item in doc["media"]:
            if not isinstance(item, dict):
                continue
            item = dict(item)
            item["proposedUrl"] = None
            item.setdefault("verificationStatus", "query-only")
            cleaned_media.append(item)
        doc["media"] = cleaned_media

    return doc


def flatten_for_ingest(doc: dict) -> dict:
    """Flatten {topic, media, _selfAudit} for Laravel/GraphQL normalizeTopic."""
    inner = doc.get("topic") if isinstance(doc.get("topic"), dict) else {}
    flat = dict(inner)
    if isinstance(doc.get("media"), list):
        flat["media"] = doc["media"]
    if doc.get("_selfAudit") is not None:
        flat["_selfAudit"] = doc["_selfAudit"]
    meta = flat.get("topicMetadata")
    if isinstance(meta, dict):
        flat.setdefault("topicId", meta.get("topicId"))
        flat.setdefault("title", meta.get("topicName"))
        flat.setdefault("specialty", meta.get("specialty"))
        flat.setdefault("contentStandard", meta.get("contentStandard"))
    return flat


def run_validator(doc: dict) -> dict:
    """Validate v9 document; return structured report compatible with pipeline."""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as doc_fh:
        json.dump(doc, doc_fh, ensure_ascii=False)
        doc_path = doc_fh.name
    report_path = doc_path + ".report.json"
    try:
        cmd = [
            sys.executable,
            str(VALIDATOR_V9),
            doc_path,
            "--reference-mode",
            V9_REFERENCE_MODE,
            "--media-mode",
            V9_MEDIA_MODE,
            "--extensions",
            "on",
            "--min-chars",
            str(V9_MIN_CHARS),
            "--json-report",
            report_path,
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        payload: dict[str, Any] = {}
        if os.path.isfile(report_path):
            with open(report_path, encoding="utf-8") as fh:
                payload = json.load(fh)
        errors = payload.get("errorCount", 0)
        warnings = payload.get("warningCount", 0)
        findings = payload.get("findings") or []
        error_list = [
            f"[{f.get('code')}] {f.get('path')}: {f.get('message')}"
            for f in findings
            if f.get("severity") == "ERROR"
        ]
        warning_list = [
            f"[{f.get('code')}] {f.get('path')}: {f.get('message')}"
            for f in findings
            if f.get("severity") == "WARN"
        ]
        return {
            "passed": proc.returncode == 0 and errors == 0,
            "exit_code": proc.returncode,
            "errors": errors,
            "warnings": warnings,
            "error_list": error_list,
            "warning_list": warning_list,
            "findings": findings,
            "metrics": payload.get("metrics") or {},
            "contentStandard": "v9",
            "raw": proc.stdout,
        }
    finally:
        for p in (doc_path, report_path):
            try:
                os.unlink(p)
            except OSError:
                pass


def repair_passes(
    *,
    llm_generate: Callable[[str], str],
    extract_json: Callable[[str], dict],
    passes: dict[int, dict],
    report: dict,
    title: str,
    topic_id: str,
    specialty: str,
    chapter: str,
    references: list[dict],
    custom_rules: str | None,
    pass_num: int,
    max_passes: int,
    on_pass_done: Callable[[str, int], None] | None = None,
) -> dict[int, dict]:
    """Regenerate only the passes implicated by validator repair_keys."""
    keys = repair_keys_from_report(report)
    passes_to_rerun: set[int] = set()
    for key in keys:
        passes_to_rerun |= pass_for_repair_key(key)

    if not passes_to_rerun:
        # Fall back: rerun pass 4 (_selfAudit often wrong after content fixes)
        passes_to_rerun = {4}

    final = pass_num >= max_passes
    urgency = (
        f"FINAL repair pass ({pass_num}/{max_passes}). Fix EVERY listed error.\n\n"
        if final
        else f"Repair pass {pass_num}/{max_passes}.\n\n"
    )
    error_lines = report.get("error_list") or []
    repair_note = (
        urgency
        + "The assembled draft FAILED validate_topic_v9.py. Regenerate ONLY the keys "
        "assigned to this pass. Fix these errors (do not invent new refIds):\n- "
        + "\n- ".join(error_lines[:20])
        + "\n\n"
    )

    updated = dict(passes)
    for idx in sorted(passes_to_rerun):
        pdef = next(p for p in V9_PASSES if p["index"] == idx)
        prompt = build_pass_prompt(
            pdef,
            title=title,
            topic_id=topic_id,
            specialty=specialty,
            chapter=chapter,
            references=references,
            custom_rules=custom_rules,
            repair_note=repair_note,
        )
        raw = llm_generate(prompt)
        updated[idx] = extract_json(raw)
        if on_pass_done:
            on_pass_done(f"repair:{pdef['name']}", idx)
    return updated
