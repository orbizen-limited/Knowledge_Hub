#!/usr/bin/env python3
"""Validate an enriched topic JSON file - tier 1 quality bar."""
import json
import re
import sys
from pathlib import Path

MIN_CHARS = 100_000

FORBIDDEN_PADDING = [
    "lorem ipsum", "as an ai model", "i cannot", "placeholder",
    "tk text", "clinical block", "evidence-based guidelines",
    "multidisciplinary tumor board consultation", "diagnostic confirmation, risk stratification",
    "primary evaluation and clinical protocol"
]

# v3: the 8 mandatory fields of every preciseDosing entry (see MASTER_IDE_PROMPT_v3
# Part B.1 / anti-pattern table). All must be present AND non-empty.
PRECISE_DOSING_FIELDS = [
    "drug", "indication", "standardDose", "doseReductionCriteria",
    "renalAdjustment", "hepaticAdjustment", "administration", "onsetOffset",
]

# v3: text-bearing sections whose entries are plain prose strings (as opposed to
# ContentBlock {heading, points[]} sections). The templated-padding detector
# walks these too — the original validator only checked ContentBlock points, so
# counter-padding in these list-of-string sections slipped through.
PROSE_LIST_SECTIONS = [
    "summaryParagraphs", "etiologyEpidemiology", "clinicalPresentation",
    "diagnosticWorkup", "treatmentLines", "specialPopulations",
    "monitoringFollowUp", "complicationsPrognosis", "pathophysiology",
    "relapseRemission", "patientEducation",
]

# v3: real multi-arm / registry study names that legitimately end in a year and
# would otherwise trip the synthetic-trial regex. Substring match, case-folded.
REAL_STUDY_ALLOWLIST_SUBSTRINGS = [
    "global burden of disease study",
    "framingham",
    "nurses' health study",
    "uk biobank",
    "copenhagen city heart study",
    "atherosclerosis risk in communities",
]

# v4: the 10 mandatory keys of every drugRegimens entry (Flutter HubDrugRegimenTable
# renders one column per drug; an empty cell is a bedside dosing gap). Only
# hard-enforced for topics authored under the v4 standard (see below).
DRUG_REGIMEN_FIELDS = [
    "drug", "indication", "initialDose", "titration", "maintenanceDose",
    "termination", "alternatives", "adverseEffectManagement", "monitoring",
    "genericKeys",
]

# v4: treatment-density floors (the "management must never be thin" standard).
# A topic whose management core is smaller than these floors is a catalogue
# summary, not a Tier-1 reference — hard error for v4-standard topics,
# warning for legacy topics so the deployed corpus is not retro-broken.
TREATMENT_DENSITY = {
    "managementSections_min_blocks": 5,
    "managementSections_min_points_per_block": 4,
    "managementSections_min_point_chars": 150,
    "treatmentLines_min_entries": 4,
    "treatmentLines_min_description_chars": 200,
    "drugRegimens_min_entries": 6,
    "treatment_min_total_chars": 30_000,
}

# v4: sections whose prose is expected to carry Vancouver [N] citation markers
# when a topic declares referenceStyle: "vancouver". managementSections and
# diagnosisSections are checked per-ContentBlock; the others are checked as
# aggregated section text.
VANCOUVER_BLOCK_SECTIONS = ["managementSections", "diagnosisSections"]
VANCOUVER_AGGREGATE_SECTIONS = [
    "summaryParagraphs", "etiologyEpidemiology", "pathophysiology",
    "clinicalPresentation", "diagnosticWorkup", "treatmentLines",
    "monitoringFollowUp", "complicationsPrognosis", "relapseRemission",
    "comorbidityManagement", "complicationManagement",
]

_CITATION_RE = re.compile(r"\[(\d+(?:\s*[-–,]\s*\d+)*)\]")


def _citation_numbers(text):
    """Extract every reference number from Vancouver markers: [3], [3,7],
    [3-5] / [3–5] (ranges expanded). Returns a set of ints."""
    found = set()
    for m in _CITATION_RE.finditer(text or ""):
        body = m.group(1)
        for part in re.split(r"\s*,\s*", body):
            rng = re.split(r"\s*[-–]\s*", part)
            if len(rng) == 2 and rng[0].isdigit() and rng[1].isdigit():
                lo, hi = int(rng[0]), int(rng[1])
                if 0 < lo <= hi and hi - lo <= 30:
                    found.update(range(lo, hi + 1))
            elif part.strip().isdigit():
                found.add(int(part.strip()))
    return found


def _topic_citation_numbers(topic):
    """Collect all Vancouver citation numbers from every text-bearing field
    (references[] itself is excluded — markers must appear in main text)."""
    nums = set()

    def _walk(node):
        if isinstance(node, str):
            nums.update(_citation_numbers(node))
        elif isinstance(node, list):
            for v in node:
                _walk(v)
        elif isinstance(node, dict):
            for v in node.values():
                _walk(v)

    for key, value in topic.items():
        if key == "references":
            continue
        _walk(value)
    return nums


def _normalize_for_dup(text):
    """Collapse a string to a template signature for near-duplicate detection:
    strip digits, punctuation runs, and whitespace, then case-fold. Templated
    padding that differs only by an incrementing counter ("item 1", "item 2",
    "Day 3", "Week 4 Expectations", "micro-message 5") all map to one signature."""
    t = re.sub(r"\d+", "#", text.lower())
    t = re.sub(r"[^a-z#]+", " ", t)
    return t.strip()

SOCIETY_FAMILIES = [
    "KDIGO", "KDOQI", "ESH", "ESC", "ESHRE", "EASD",
    "AHA", "ACC", "AAN", "AANEM", "ACR", "AAP", "ERS", "ATS",
    "IDSA", "ATS", "FDA", "EMA", "MHRA",
    "AUA", "EAU", "SUO", "ASTRO", "ASCO", "ESMO",
    "ACOG", "SMFM", "ASRM", "SOGC", "RANZCOG", "RCOG", "ISSHP",
    "EULAR", "ACR", "APLAR", "GRAPPA",
    "ADA", "EASL", "AASLD", "ESPGHAN", "NICE", "USPSTF",
    "UpToDate", "DynaMed", "BMJ Best Practice",
    "ISTH", "ASH", "ASHN", "ASN", "ISN", "ERA", "ERA-EDTA",
]

SECTION_NAMES = [
    "summaryParagraphs", "recommendations", "etiologyEpidemiology",
    "clinicalPresentation", "differentialDiagnosis", "diagnosticWorkup",
    "treatmentLines", "specialPopulations", "monitoringFollowUp",
    "complicationsPrognosis", "pathophysiology", "comorbidityManagement",
    "complicationManagement", "drugRegimens", "relapseRemission",
    "patientEducation", "crossReferences", "backgroundInformation",
    "diagnosisSections", "managementSections", "complicationSections",
    "prognosisQuantitative", "preciseDosing",
]


def main():
    if len(sys.argv) < 2:
        print("usage: validate_topic.py <file.json>")
        sys.exit(1)
    p = Path(sys.argv[1])
    if not p.exists():
        print(f"File not found: {p}")
        sys.exit(2)

    try:
        text = p.read_text(encoding="utf-8")
        topic = json.loads(text)
    except json.JSONDecodeError as e:
        print(f"INVALID JSON at line {e.lineno} col {e.colno}: {e.msg}")
        sys.exit(3)

    errors = []
    warnings = []

    title = topic.get("title") or topic.get("topicId") or "<unnamed>"
    topic_id = topic.get("topicId", "")
    s = json.dumps(topic, ensure_ascii=False)
    char_count = len(s)

    if char_count < MIN_CHARS:
        errors.append(f"content chars={char_count:,} below minimum {MIN_CHARS:,}")

    lower = s.lower()
    for stem in FORBIDDEN_PADDING:
        if stem in lower:
            warnings.append(f"possible padding stem: '{stem}'")

    refs = topic.get("references", [])
    if not refs:
        errors.append("references section empty")
    else:
        for r in refs:
            url = (r.get("url") or "").strip()
            cit = (r.get("citation") or "").strip()
            if not cit:
                errors.append(f"reference missing citation: {r}")
                continue
            if not url.startswith("https://doi.org/"):
                errors.append(f"reference missing/invalid url (must be https://doi.org/...): {cit[:80]}")

    society_seen = False
    for r in refs + [topic]:
        text_blob = (str(r.get("citation") or "") + " " + str(r.get("societyFamily") or ""))
        for fam in SOCIETY_FAMILIES:
            if fam.lower() in text_blob.lower():
                society_seen = True
                break
        if society_seen:
            break
    if not society_seen:
        warnings.append("no recognized society-family guideline citation found")

    diff = topic.get("differentialDiagnosis", [])
    if not isinstance(diff, list) or len(diff) < 3:
        errors.append(f"differentialDiagnosis entries={len(diff) if isinstance(diff,list) else 0} < 3")
    else:
        for d in diff:
            if not d.get("condition"):
                errors.append("differentialDiagnosis entry missing 'condition'")
            if not d.get("distinguishingFeature"):
                errors.append(f"differentialDiagnosis entry missing 'distinguishingFeature': {d}")

    prog = topic.get("prognosisQuantitative", [])
    if not isinstance(prog, list) or len(prog) < 5:
        errors.append(f"prognosisQuantitative entries={len(prog) if isinstance(prog,list) else 0} < 5")
    else:
        for p in prog:
            if not p.get("outcome"):
                errors.append("prognosisQuantitative entry missing 'outcome'")
            if not p.get("estimate"):
                errors.append(f"prognosisQuantitative entry missing 'estimate': {p}")
            # v3 Part C.2 TARGET: prognosis DOI should be the full
            # https://doi.org/... URL, not a bare 10.x DOI. Kept a WARNING (not a
            # hard error) because a large slice of the already-deployed corpus
            # (e.g. cardiology) stores bare DOIs; hard-failing them would break
            # shipped topics. New topics MUST use the full URL; a corpus-wide
            # normalization (json.load -> prefix -> json.dump) is the follow-up.
            doi = str(p.get("doi", ""))
            if not doi:
                warnings.append(f"prognosisQuantitative missing doi: {p.get('outcome','')[:60]}")
            elif not doi.startswith("https://doi.org/"):
                warnings.append(
                    f"prognosisQuantitative doi should be full 'https://doi.org/...' URL: "
                    f"{p.get('outcome','')[:50]}"
                )

    dose = topic.get("preciseDosing", [])
    if not isinstance(dose, list) or len(dose) < 2:
        errors.append(f"preciseDosing entries={len(dose) if isinstance(dose,list) else 0} < 2")
    else:
        for di, d in enumerate(dose):
            if not isinstance(d, dict):
                errors.append(f"preciseDosing[{di}] is not an object")
                continue
            # v3 Part B.1: all 8 fields present AND non-empty, or the bedside
            # dosing panel renders half-blank (the "incomplete preciseDosing"
            # anti-pattern). Was previously only drug + standardDose.
            missing = [k for k in PRECISE_DOSING_FIELDS if not str(d.get(k, "")).strip()]
            if missing:
                label = d.get("drug") or f"entry {di}"
                errors.append(
                    f"preciseDosing '{label}' missing/empty required field(s): {', '.join(missing)}"
                )

    # Strict array schema & key validations
    dreg = topic.get("drugRegimens", [])
    if not isinstance(dreg, list) or len(dreg) == 0:
        errors.append("drugRegimens section must be a non-empty list of dicts")
    else:
        for dr in dreg:
            if not isinstance(dr, dict) or not dr.get("drug"):
                errors.append(f"drugRegimens entry missing required 'drug' key: {dr}")

    cm = topic.get("comorbidityManagement", [])
    if not isinstance(cm, list) or len(cm) == 0:
        errors.append("comorbidityManagement section must be a non-empty list of KeyedDetail dicts ({heading, detail})")
    else:
        headings = [c.get("heading", "").strip() for c in cm if isinstance(c, dict)]
        gen_headings = [h for h in headings if h.lower() in ["management", "clinical consideration", "general considerations", "complication management"]]
        if len(gen_headings) > 1 or len(headings) != len(set(headings)):
            errors.append(f"comorbidityManagement contains duplicate or generic placeholder headings: {gen_headings or headings}")

    comp_m = topic.get("complicationManagement", [])
    if isinstance(comp_m, str):
        errors.append("complicationManagement must be a list of KeyedDetail dicts ({heading, detail}), not a raw string")
    elif isinstance(comp_m, list):
        headings = [c.get("heading", "").strip() for c in comp_m if isinstance(c, dict)]
        gen_headings = [h for h in headings if h.lower() in ["management", "clinical consideration", "general considerations", "complication management"]]
        if len(gen_headings) > 1 or len(headings) != len(set(headings)):
            errors.append(f"complicationManagement contains duplicate or generic placeholder headings: {gen_headings or headings}")

    # Check ContentBlock sections for empty, duplicate, artificial-counter
    # headings, and exact/near-duplicate templated points.
    #
    # `global_points_seen` catches EXACT duplicate point text across blocks.
    # `template_sig_counts` catches NEAR-duplicate templated padding — text that
    # differs only by an incrementing counter ("... item 1", "... item 2",
    # "Day 3 recovery checkpoint ...", "Week 4 Expectations"). This is the
    # defect class that shipped undetected: each string was technically unique
    # (the number differed) so the exact-match check never fired. See
    # MASTER_IDE_PROMPT_v3 Part A.3 "Repetitive Generic Points".
    global_points_seen = set()
    template_sig_counts = {}   # normalized signature -> occurrence count
    template_sig_example = {}  # signature -> first raw example (for the message)

    def _register_prose(raw, where):
        raw = (raw or "").strip()
        if len(raw) < 25:
            return
        sig = _normalize_for_dup(raw)
        if not sig:
            return
        template_sig_counts[sig] = template_sig_counts.get(sig, 0) + 1
        template_sig_example.setdefault(sig, (raw, where))

    contentblock_sections = [
        "managementSections", "diagnosisSections",
        "complicationSections", "backgroundInformation",
    ]
    for sec_name in contentblock_sections:
        blocks = topic.get(sec_name, [])
        if not isinstance(blocks, list):
            continue
        heading_sigs = {}  # normalized heading -> count within this section
        for idx, b in enumerate(blocks):
            if not isinstance(b, dict):
                continue
            h = b.get("heading", "")
            if re.search(r"\bPart\s+\d+\b", h, re.I):
                errors.append(f"{sec_name}[{idx}] has artificial numbered heading: '{h}'")
            hsig = _normalize_for_dup(h)
            if hsig:
                heading_sigs[hsig] = heading_sigs.get(hsig, 0) + 1
            pts = b.get("points", [])
            if not pts or len(pts) == 0:
                errors.append(f"{sec_name}[{idx}] '{h}' has empty 'points' list")
            else:
                for p in pts:
                    pt_text = p.get("text", "").strip()
                    if not pt_text:
                        continue
                    low = pt_text.lower()
                    if low in global_points_seen:
                        errors.append(
                            f"{sec_name}[{idx}] contains repetitive generic point: '{low[:40]}...'"
                        )
                    else:
                        global_points_seen.add(low)
                    _register_prose(pt_text, f"{sec_name}[{idx}].points")
        # Sequential/counter headings within one section (e.g. "Detail 1..20",
        # "Recovery Week 1..8 Expectations", "Evidence Context (1)..(4)").
        for hsig, count in heading_sigs.items():
            if count >= 3:
                errors.append(
                    f"{sec_name} has {count} sequentially-numbered/near-identical headings "
                    f"('{hsig[:50]}...') — use distinct clinical titles, not a counter"
                )

    # Walk the prose-list sections through the SAME near-duplicate detector —
    # counter padding also appears here (e.g. patientEducation
    # "micro-message 1..10", monitoringFollowUp "item N").
    for sec_name in PROSE_LIST_SECTIONS:
        entries = topic.get(sec_name, [])
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, str):
                _register_prose(entry, sec_name)
            elif isinstance(entry, dict):
                # objects (e.g. specialPopulations, treatmentLines) — scan ONLY
                # the free-prose content keys. Metadata/label fields
                # (evidenceLevel, grade, source, population, year, …)
                # legitimately repeat the same value across entries and must
                # NOT be treated as duplicated prose.
                for vk, vv in entry.items():
                    if isinstance(vv, str) and vk.lower() in (
                        "detail", "text", "guidance", "description",
                        "note", "narrative", "body",
                    ):
                        _register_prose(vv, f"{sec_name}.{vk}")

    # A template signature repeated >=6 times is padding, not coincidence. The
    # real counter-padding always ran 8-30x; genuine content occasionally
    # repeats a structural line 2-5x (e.g. a shared drug-interaction-audit
    # sentence), so the floor sits at 6 to avoid false positives on real topics.
    for sig, count in template_sig_counts.items():
        if count >= 6:
            example, where = template_sig_example.get(sig, ("", "?"))
            errors.append(
                f"templated padding: {count} near-identical entries (first in {where}) "
                f"differing only by a counter/number — '{example[:60]}...'"
            )

    tl = topic.get("treatmentLines", [])
    if not isinstance(tl, list) or len(tl) == 0:
        errors.append("treatmentLines section must be a non-empty list of TreatmentLine dicts ({line, description})")
    else:
        for t in tl:
            if not isinstance(t, dict) or not t.get("description"):
                errors.append(f"treatmentLines entry missing required 'description' key: {t}")

    content = topic.get("content") or topic
    sections_found = sum(1 for n in SECTION_NAMES if n in content)
    if sections_found < 18:
        warnings.append(f"only {sections_found} of {len(SECTION_NAMES)} canonical sections present")

    risky_pattern = re.compile(r"\b(?:trial|cohort|study|XX)\s*-?\s*\d{2,}\b", re.I)
    real_trial_allowlist = {
        "STAR trial", "CART trial", "CARTITUDE trial", "FAST trial",
        "ALLHEART", "MASTER trial", "STOP-IgAN", "EF-14", "MAGPIE",
        "PRINCIPLE", "RECOVERY", "PRONTO", "SPARC trial", "JEWEL trial",
        "PROMM", "SOLID trial", "CSP 468", "STOP-IGAN", "STAR-A",
        "Magpie Trial 2002", "NOBLE", "HELP trial", "LRT-2",
    }
    s_low = s.lower()
    reported = 0
    for m in risky_pattern.finditer(s):
        if reported >= 3:
            break
        hit = m.group(0)
        # Skip if the hit sits inside a known real registry/cohort study name
        # (e.g. "Global Burden of Disease Study 2019").
        ctx = s_low[max(0, m.start() - 45):m.end()]
        if any(sub in ctx for sub in REAL_STUDY_ALLOWLIST_SUBSTRINGS):
            continue
        if hit in real_trial_allowlist:
            continue
        warnings.append(f"possible synthetic trial identifier: '{hit}'")
        reported += 1

    # ------------------------------------------------------------------
    # v4 gates: Vancouver numeric referencing + treatment density.
    #
    # A topic opts into the v4 standard by setting top-level metadata
    #   "referenceStyle": "vancouver"
    # For v4 topics every check below is a HARD ERROR. For legacy topics
    # (no flag — the entire pre-v4 deployed corpus) the same checks
    # degrade to warnings, so we tighten the authoring standard going
    # forward without retro-breaking shipped content (the v3 lesson:
    # hard gates that retro-fail the corpus create a backlog, not
    # compliance).
    # ------------------------------------------------------------------
    ref_style = str(topic.get("referenceStyle") or "").strip().lower()
    v4 = ref_style == "vancouver"

    def _v4_fail(msg):
        (errors if v4 else warnings).append(msg)

    # --- Vancouver referencing ---
    ref_ids = set()
    for ri, r in enumerate(refs):
        rid = r.get("refId")
        if not isinstance(rid, int) or rid <= 0:
            _v4_fail(f"references[{ri}] missing positive integer 'refId' (Vancouver numbering): {str(r)[:60]}")
        elif rid in ref_ids:
            _v4_fail(f"references[{ri}] duplicates refId {rid}")
        else:
            ref_ids.add(rid)

    cited = _topic_citation_numbers(topic)
    dangling = sorted(n for n in cited if n not in ref_ids)
    uncited = sorted(n for n in ref_ids if n not in cited)
    if dangling:
        _v4_fail(
            f"Vancouver citation(s) {dangling[:8]}{'...' if len(dangling) > 8 else ''} in main text "
            f"have no matching references[] refId"
        )
    if uncited:
        _v4_fail(
            f"references[] refId(s) {uncited[:8]}{'...' if len(uncited) > 8 else ''} are never cited "
            f"in the main text — cite them [N] where the claim is made or drop them"
        )
    if v4 and not cited:
        errors.append("referenceStyle is 'vancouver' but no [N] citation markers found in any main-text section")

    # Per-block citation coverage: every management/diagnosis ContentBlock
    # must anchor its claims to at least one numbered reference.
    for sec_name in VANCOUVER_BLOCK_SECTIONS:
        blocks = topic.get(sec_name, [])
        if not isinstance(blocks, list):
            continue
        for bi, b in enumerate(blocks):
            if not isinstance(b, dict):
                continue
            blob = " ".join(str(p.get("text", "")) for p in b.get("points", []) if isinstance(p, dict))
            if blob and not _citation_numbers(blob):
                _v4_fail(
                    f"{sec_name}[{bi}] '{b.get('heading', '')[:50]}' carries no Vancouver [N] "
                    f"citation — treatment/diagnosis claims must be evidence-anchored"
                )

    # --- Treatment density ("management must never be thin") ---
    td = TREATMENT_DENSITY
    mgmt = topic.get("managementSections", [])
    mgmt_chars = 0
    if isinstance(mgmt, list):
        if len(mgmt) < td["managementSections_min_blocks"]:
            _v4_fail(
                f"managementSections has {len(mgmt)} blocks (< {td['managementSections_min_blocks']}) "
                f"— management must be organised indication-wise, one block per clinical scenario"
            )
        for bi, b in enumerate(mgmt):
            if not isinstance(b, dict):
                continue
            pts = [p for p in b.get("points", []) if isinstance(p, dict) and str(p.get("text", "")).strip()]
            if len(pts) < td["managementSections_min_points_per_block"]:
                _v4_fail(
                    f"managementSections[{bi}] '{b.get('heading', '')[:50]}' has {len(pts)} points "
                    f"(< {td['managementSections_min_points_per_block']})"
                )
            thin = [p for p in pts if len(str(p.get("text", ""))) < td["managementSections_min_point_chars"]]
            if thin:
                _v4_fail(
                    f"managementSections[{bi}] '{b.get('heading', '')[:50]}' has {len(thin)} point(s) under "
                    f"{td['managementSections_min_point_chars']} chars — management bullets must carry "
                    f"doses, thresholds and stop rules, not one-line labels"
                )
            mgmt_chars += sum(len(str(p.get("text", ""))) for p in pts)

    if isinstance(tl, list):
        if len(tl) < td["treatmentLines_min_entries"]:
            _v4_fail(f"treatmentLines has {len(tl)} entries (< {td['treatmentLines_min_entries']})")
        for ti, t in enumerate(tl):
            if isinstance(t, dict) and len(str(t.get("description", ""))) < td["treatmentLines_min_description_chars"]:
                _v4_fail(
                    f"treatmentLines[{ti}] '{str(t.get('line', ''))[:40]}' description under "
                    f"{td['treatmentLines_min_description_chars']} chars"
                )
        mgmt_chars += sum(len(str(t.get("description", ""))) for t in tl if isinstance(t, dict))

    if isinstance(dreg, list):
        if len(dreg) < td["drugRegimens_min_entries"]:
            _v4_fail(f"drugRegimens has {len(dreg)} entries (< {td['drugRegimens_min_entries']})")
        for di, dr in enumerate(dreg):
            if not isinstance(dr, dict):
                continue
            missing = []
            for k in DRUG_REGIMEN_FIELDS:
                if k == "genericKeys":
                    if not dr.get(k):
                        missing.append(k)
                elif not str(dr.get(k, "")).strip():
                    missing.append(k)
            if missing:
                _v4_fail(
                    f"drugRegimens[{di}] '{dr.get('drug', '')}' missing/empty: {', '.join(missing)}"
                )
            mgmt_chars += sum(len(str(dr.get(k, ""))) for k in DRUG_REGIMEN_FIELDS if k != "genericKeys")

    if mgmt_chars < td["treatment_min_total_chars"]:
        _v4_fail(
            f"treatment core (managementSections + treatmentLines + drugRegimens) is "
            f"{mgmt_chars:,} chars (< {td['treatment_min_total_chars']:,}) — management must be the "
            f"densest part of the topic, not a summary"
        )

    print(f"topic:    {title}")
    print(f"id:       {topic_id}")
    print(f"chars:    {char_count:,}")
    print(f"standard: {'v4 (vancouver) — gates enforced as errors' if v4 else 'legacy — v4 gates reported as warnings'}")
    print(f"errors:   {len(errors)}")
    for e in errors:
        print(f"  - {e}")
    print(f"warnings: {len(warnings)}")
    for w in warnings:
        print(f"  - {w}")

    sys.exit(0 if not errors else 1)


if __name__ == "__main__":
    main()
