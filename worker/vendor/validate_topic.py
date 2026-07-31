#!/usr/bin/env python3
"""Validate an enriched topic JSON file - tier 1 quality bar.

Usage:
    validate_topic.py <file.json>   validate one enriched topic
    validate_topic.py --corpus      corpus-level gate: no duplicate topicIds
"""
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

MIN_CHARS = 100_000

# Data sources for the v5 gates. VENDORED COPY (Knowledge_Hub worker): this
# file runs outside the doctorshero-rx repo tree, so instead of resolving the
# RX repo root we default every path to snapshots shipped next to this script
# and allow env overrides for deployments that mount the real data elsewhere.
VENDOR_DIR = Path(__file__).resolve().parent


def _env_path(name: str, default: Path) -> Path:
    v = os.environ.get(name, "").strip()
    return Path(v) if v else default


GENERIC_DB = _env_path("KH_VALIDATOR_GENERIC_DB", VENDOR_DIR / "medex_comprehensive_data.json")
GENERIC_INDEX_CACHE = _env_path("KH_VALIDATOR_GENERIC_INDEX", VENDOR_DIR / "knowledge_hub_generic_index.json")
ENRICHED_DIR = _env_path("KH_VALIDATOR_ENRICHED_DIR", VENDOR_DIR / "enriched")
TOPIC_INDEX_CACHE = _env_path("KH_VALIDATOR_TOPIC_INDEX", VENDOR_DIR / "knowledge_hub_topic_index.json")

# ---------------------------------------------------------------------------
# Authoritative non-DOI URL allowlist for references[].
#
# The reference gate prefers DOIs ("https://doi.org/..."), but a slice of
# genuine sources has no DOI to point at: regulator/society guideline pages
# (FDA, WHO, NICE, NCCN, CDC, EMA, SIGN, GINA, GOLD, IDSA, ATS, ERS, KDIGO),
# PubMed records of pre-DOI-era papers, NCBI Bookshelf / GeneReviews chapters,
# and arXiv preprints. A reference url is therefore VALID when it either starts
# with https://doi.org/ OR its host matches one of the entries below (exact
# host or any subdomain — e.g. eur-lex.europa.eu matches europa.eu,
# pubmed.ncbi.nlm.nih.gov matches nih.gov). DOI stays the preferred form: an
# allowlisted url whose citation text still looks like a journal article
# (year + volume:page pattern) earns a WARNING nudging authors back to the DOI.
# Anything else remains a hard error.
# ---------------------------------------------------------------------------
AUTHORITATIVE_URL_HOSTS = (
    # NCBI / NIH (PubMed, Bookshelf, GeneReviews, PMC) and US/EU public bodies
    "pubmed.ncbi.nlm.nih.gov", "www.ncbi.nlm.nih.gov", "nih.gov",
    "cdc.gov", "fda.gov", "who.int", "ema.europa.eu", "europa.eu",
    # guideline bodies
    "nice.org.uk", "sign.ac.uk", "nccn.org",
    # respiratory / renal / infectious-disease societies
    "ginasthma.org", "goldcopd.org", "idsociety.org", "thoracic.org",
    "ersnet.org", "kidney-international.org", "kdigo.org",
    # preprints
    "arxiv.org",
    # book / legal / report catalog & publisher sources for non-journal works
    # that have no DOI: library/publisher records (MIT Press, Routledge),
    # digital-library copies of print monographs (archive.org), official
    # statute text (govinfo.gov), archived government reports (Georgetown's
    # bioethics archive), an institutional legal repository (Minnesota Law),
    # and society guideline platforms (ACR, ACR DSI, AASM, COG survivorship,
    # NHS Scotland)
    "archive.org", "mitpress.mit.edu", "routledge.com", "govinfo.gov",
    "georgetown.edu", "scholarship.law.umn.edu",
    "acr.org", "acrdsi.org", "aasm.org", "survivorshipguidelines.org",
    "scot.nhs.uk",
)

_JOURNAL_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
_JOURNAL_VOL_PAGE_RE = re.compile(r";\s*\d+(?:\(\d+\))?\s*:")


def _url_host_allowlisted(url):
    """True when the url's host matches the authoritative allowlist, exactly
    or as a subdomain ('eur-lex.europa.eu' -> 'europa.eu')."""
    try:
        host = (urlparse(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    if not host:
        return False
    return any(host == h or host.endswith("." + h) for h in AUTHORITATIVE_URL_HOSTS)


def _looks_like_journal_article(citation):
    """Heuristic: citation carries a year (19xx/20xx) AND a volume(:issue):
    page pattern like ';23(1):' or ';92:' — i.e. a journal article that should
    have a DOI."""
    return bool(
        _JOURNAL_YEAR_RE.search(citation) and _JOURNAL_VOL_PAGE_RE.search(citation)
    )


# Facet records (topic JSONs with a non-empty canonicalTopicId) are thin
# deep-link stubs: body content lives in the canonical topic. Carrying any of
# these sections non-empty is a WARNING — the content should be moved to the
# canonical, not duplicated in the facet.
FACET_FULL_CONTENT_SECTIONS = [
    "backgroundInformation", "etiologyEpidemiology", "pathophysiology",
    "clinicalPresentation", "differentialDiagnosis", "diagnosticWorkup",
    "diagnosisSections", "treatmentLines", "managementSections",
    "recommendations", "preciseDosing", "drugRegimens", "specialPopulations",
    "monitoringFollowUp", "complicationsPrognosis", "complicationSections",
    "complicationManagement", "comorbidityManagement", "relapseRemission",
    "patientEducation", "prognosisQuantitative", "references",
]

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


def _atomic_write_json(path, payload):
    """Write a cache file atomically (tmp + rename) so a concurrent validator
    run never reads a half-written index."""
    try:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass  # the cache is an optimisation; validation must still work


def _load_generic_names():
    """Salt-tolerant generic-name index for doseSpec.genericKey resolution.

    Built from the medex comprehensive DB (generic names from the generics
    sections + generic_name of every brand entry) and cached to
    tools/knowledge_hub_generic_index.json; the cache is rebuilt when the DB
    file's (mtime, size) changes or the cache is missing/corrupt. When the
    cache is valid the big DB is never parsed."""
    try:
        st = GENERIC_DB.stat()
        sig = [st.st_mtime, st.st_size]
    except OSError:
        sig = None
    if GENERIC_INDEX_CACHE.exists():
        try:
            cached = json.loads(GENERIC_INDEX_CACHE.read_text(encoding="utf-8"))
            # Vendored deployments ship only the index snapshot (no medex DB
            # on the worker box): when the DB file is absent, trust the
            # snapshot unconditionally instead of rebuilding from nothing.
            if cached.get("db_signature") == sig or sig is None:
                return set(cached["generic_names"])
        except (json.JSONDecodeError, KeyError, TypeError, OSError):
            pass  # fall through to rebuild
    names = set()
    try:
        db = json.loads(GENERIC_DB.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        db = {}
    for section in ("generics_allopathic", "generics_herbal"):
        for e in db.get(section, []) or []:
            n = str(e.get("medicine_name") or "").strip().lower()
            if n:
                names.add(n)
    for section in ("brands_allopathic", "brands_herbal"):
        for e in db.get(section, []) or []:
            g = str(e.get("generic_name") or "").strip().lower()
            if g:
                names.add(g)
    if names:
        _atomic_write_json(
            GENERIC_INDEX_CACHE,
            {"db_signature": sig, "generic_names": sorted(names)},
        )
    return names


def _generic_resolves(generic_key, names):
    """Mirror of MedicineDatabaseService.getBrandsByGenericPrefix
    (lib/services/database/medicine_database_service.dart): the key matches
    when a DB generic equals it exactly, or begins with `key + separator`
    (space or comma) — so salt forms resolve ('warfarin' -> 'Warfarin
    Sodium', 'amlodipine' -> 'Amlodipine Besylate'). Never a fuzzy match."""
    g = generic_key.strip().lower()
    if not g:
        return False
    if g in names:
        return True
    return any(n.startswith(g + " ") or n.startswith(g + ",") for n in names)


def _load_topic_index():
    """Snapshot {topicId: title} of every topic in assets/knowledge_hub/
    enriched/, for the v5 link-integrity gate. Cached to
    tools/knowledge_hub_topic_index.json; rebuilt when the newest file mtime
    in the enriched dir changes (or the file count differs, or the cache is
    missing/corrupt). Returns (topic_ids_dict, lowercased_title_set)."""
    files = sorted(ENRICHED_DIR.glob("*.json"))
    newest = 0.0
    for f in files:
        try:
            m = f.stat().st_mtime
            if m > newest:
                newest = m
        except OSError:
            pass
    if TOPIC_INDEX_CACHE.exists():
        try:
            cached = json.loads(TOPIC_INDEX_CACHE.read_text(encoding="utf-8"))
            # Vendored deployments have no enriched/ dir on disk — trust the
            # shipped snapshot when there are no local files to index.
            if not files or (cached.get("newest_mtime") == newest
                    and len(cached.get("topics", {})) >= 0
                    and cached.get("file_count") == len(files)):
                topics = cached["topics"]
                return topics, {t.strip().lower() for t in topics.values()}
        except (json.JSONDecodeError, KeyError, TypeError, OSError):
            pass  # fall through to rebuild
    topics = {}
    for f in files:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        tid = str(d.get("topicId") or "").strip()
        title = str(d.get("title") or "").strip()
        if tid:
            topics[tid] = title
    _atomic_write_json(
        TOPIC_INDEX_CACHE,
        {"newest_mtime": newest, "file_count": len(files), "topics": topics},
    )
    return topics, {t.strip().lower() for t in topics.values()}


def _corpus_mode():
    """Corpus-level gate: a single topicId must never be declared by more
    than one enriched file. Per-file validation cannot see siblings, hence
    this separate mode. Fast path: regex out the topicId instead of parsing
    each whole file."""
    seen = {}
    duplicates = []
    tid_re = re.compile(r'"topicId"\s*:\s*"([^"]+)"')
    files = sorted(ENRICHED_DIR.glob("*.json"))
    for f in files:
        tid = None
        try:
            m = tid_re.search(f.read_text(encoding="utf-8"))
            if m:
                tid = m.group(1)
        except OSError:
            pass
        if tid is None:
            try:
                tid = str(json.loads(f.read_text(encoding="utf-8")).get("topicId") or "").strip()
            except (json.JSONDecodeError, OSError):
                continue
        if not tid:
            continue
        if tid in seen:
            duplicates.append((tid, seen[tid], f.name))
        else:
            seen[tid] = f.name
    print(f"corpus: {len(files)} files scanned, {len(seen)} unique topicIds")
    if duplicates:
        print(f"DUPLICATE topicIds: {len(duplicates)}")
        for tid, first, second in duplicates:
            print(f"  - '{tid}' declared by both {first} and {second}")
        sys.exit(1)
    print("OK: no duplicate topicIds")
    sys.exit(0)


def main():
    if len(sys.argv) < 2:
        print("usage: validate_topic.py <file.json> | --corpus")
        sys.exit(1)
    if sys.argv[1] == "--corpus":
        _corpus_mode()
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

    # Standard opt-in flags. v4: "referenceStyle": "vancouver". v5:
    # "contentStandard": "v5" — implies every v4 gate also applies as a hard
    # error (v5 is a superset standard), plus the v5-only gates below.
    ref_style = str(topic.get("referenceStyle") or "").strip().lower()
    v5 = str(topic.get("contentStandard") or "").strip().lower() == "v5"
    v4 = ref_style == "vancouver" or v5

    # ------------------------------------------------------------------
    # Facet-record mode. A topic JSON whose canonicalTopicId is a non-empty
    # string is a thin deep-link stub (Dart: KnowledgeTopic.canonicalTopicId /
    # facetAnchors), not a full topic. Only the facet contract is enforced;
    # every full-content gate below (100K chars, sections, preciseDosing,
    # references, v4/v5 gates) is SKIPPED.
    # ------------------------------------------------------------------
    canonical_id = topic.get("canonicalTopicId")
    if isinstance(canonical_id, str) and canonical_id.strip():
        cid = canonical_id.strip()
        tid = str(topic.get("topicId") or "").strip()
        t = str(topic.get("title") or "").strip()
        if not tid:
            errors.append("facet record: topicId missing/empty")
        if not t:
            errors.append("facet record: title missing/empty")
        if tid and cid == tid:
            errors.append(
                "facet record: canonicalTopicId equals topicId (self-loop) — "
                "a facet must deep-link into a different canonical topic"
            )
        known_ids, _ = _load_topic_index()
        if cid not in known_ids:
            errors.append(
                f"facet record: canonicalTopicId '{cid}' resolves to no topicId "
                f"in the enriched corpus index"
            )
        sp = topic.get("summaryParagraphs")
        if not isinstance(sp, list) or not any(
            isinstance(x, str) and x.strip() for x in sp
        ):
            errors.append(
                "facet record: summaryParagraphs missing/empty — the Flutter "
                "loader requires body content"
            )
        # reviewStatus enum-index check applies to facets too — Dart's
        # ReviewStatusX.fromIndex silently coerces a non-int to 'approved'.
        rs = topic.get("reviewStatus")
        if not isinstance(rs, bool) and isinstance(rs, int):
            if rs not in (0, 1, 2, 3):
                errors.append(
                    f"facet record: reviewStatus={rs} out of range 0-3 "
                    f"(0=pendingClinicianCheck 1=pendingBoardReview 2=approved 3=rejected)"
                )
        else:
            errors.append(
                f"facet record: reviewStatus must be an integer enum index (0-3), "
                f"got {rs!r} ({type(rs).__name__})"
            )
        for sec in FACET_FULL_CONTENT_SECTIONS:
            if topic.get(sec):
                warnings.append(
                    f"facet record carries full-content section '{sec}' — "
                    f"content should live in the canonical topic '{cid}'"
                )
        print(f"topic:    {title}")
        print(f"id:       {topic_id}")
        print(f"chars:    {char_count:,}")
        print(f"standard: facet record (canonical: {cid})")
        print(f"errors:   {len(errors)}")
        for e in errors:
            print(f"  - {e}")
        print(f"warnings: {len(warnings)}")
        for w in warnings:
            print(f"  - {w}")
        sys.exit(0 if not errors else 1)

    if char_count < MIN_CHARS:
        errors.append(f"content chars={char_count:,} below minimum {MIN_CHARS:,}")

    # reviewStatus must be the integer enum index (0=pendingClinicianCheck,
    # 1=pendingBoardReview, 2=approved, 3=rejected). Dart's ReviewStatusX.fromIndex
    # silently coerces anything else (a string name, a numeric string, missing
    # key) to index -1 -> falls through to the *default* `approved` -- so a
    # non-int reviewStatus doesn't just fail to parse, it silently mislabels the
    # topic as clinician-approved. Hard error, not a warning.
    review_status = topic.get("reviewStatus")
    if not isinstance(review_status, bool) and isinstance(review_status, int):
        if review_status not in (0, 1, 2, 3):
            errors.append(
                f"reviewStatus={review_status} out of range 0-3 "
                f"(0=pendingClinicianCheck 1=pendingBoardReview 2=approved 3=rejected)"
            )
    else:
        errors.append(
            f"reviewStatus must be an integer enum index (0-3), got "
            f"{review_status!r} ({type(review_status).__name__}) -- a non-int "
            f"value is silently coerced to 'approved' by the app, mislabelling "
            f"the review state"
        )

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
            if url.startswith("https://doi.org/"):
                continue
            if _url_host_allowlisted(url):
                # DOI stays the preferred form: nudge when the citation still
                # looks like a journal article (year + volume:page pattern).
                if _looks_like_journal_article(cit):
                    warnings.append(
                        f"allowlisted non-DOI url for what looks like a journal "
                        f"article — prefer https://doi.org/…: {cit[:80]}"
                    )
                continue
            errors.append(
                f"reference missing/invalid url (must be https://doi.org/... or a "
                f"host on the authoritative allowlist — see AUTHORITATIVE_URL_HOSTS "
                f"at the top of validate_topic.py): {cit[:80]}"
            )

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
            # hard error) for legacy and v4 topics because a large slice of the
            # already-deployed corpus (e.g. cardiology) stores bare DOIs;
            # hard-failing them would break shipped topics. For v5-standard
            # topics it is a HARD ERROR — new topics MUST use the full URL.
            doi = str(p.get("doi", ""))
            if not doi:
                warnings.append(f"prognosisQuantitative missing doi: {p.get('outcome','')[:60]}")
            elif not doi.startswith("https://doi.org/"):
                (errors if v5 else warnings).append(
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
                    if not isinstance(p, dict):
                        # A point authored as a raw string or nested list instead
                        # of {text, level} used to crash the whole validator run
                        # with an unhandled AttributeError, making the file
                        # unauditable rather than reporting it as a failure.
                        errors.append(
                            f"{sec_name}[{idx}] '{h}' has a point that is not a "
                            f"{{text, level}} object (got {type(p).__name__}): "
                            f"{str(p)[:80]}"
                        )
                        continue
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
    # (v5-standard topics — "contentStandard": "v5" — are treated as v4
    # too; the flags are computed up front, right after the JSON loads.)
    # For v4 topics every check below is a HARD ERROR. For legacy topics
    # (no flag — the entire pre-v4 deployed corpus) the same checks
    # degrade to warnings, so we tighten the authoring standard going
    # forward without retro-breaking shipped content (the v3 lesson:
    # hard gates that retro-fail the corpus create a backlog, not
    # compliance).
    # ------------------------------------------------------------------
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
        errors.append(
            "v4/v5-standard topic but no [N] citation markers found in any "
            "main-text section (referenceStyle 'vancouver' / contentStandard 'v5')"
        )

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

    # ------------------------------------------------------------------
    # v5 gates: structured doseSpec on every drugRegimens entry, and
    # cross-topic link integrity.
    #
    # A topic opts into the v5 standard by setting top-level metadata
    #   "contentStandard": "v5"
    # v5 implies every v4 gate above as a hard error too. The doseSpec
    # schema gate is v5-only and silent for other topics (the deployed
    # corpus has no doseSpec yet — warning there would be pure noise);
    # the link-integrity gate degrades to a warning for non-v5 topics,
    # same as the v4 pattern.
    # ------------------------------------------------------------------
    def _v5_fail(msg):
        (errors if v5 else warnings).append(msg)

    if v5 and isinstance(dreg, list):
        generic_names = None  # lazy: built only when a genericKey needs resolving
        for di, dr in enumerate(dreg):
            if not isinstance(dr, dict):
                continue
            label = dr.get("drug", "") or f"entry {di}"
            spec = dr.get("doseSpec")
            if not isinstance(spec, dict):
                errors.append(
                    f"drugRegimens[{di}] '{label}' missing 'doseSpec' object "
                    f"(v5 structured dosing schema)"
                )
                continue
            amount = spec.get("amount")
            if isinstance(amount, bool) or not isinstance(amount, (int, float)) or amount <= 0:
                errors.append(
                    f"drugRegimens[{di}] '{label}' doseSpec.amount must be a positive "
                    f"number, got {amount!r}"
                )
            for k in ("unit", "route", "frequency", "maxDosePerDay", "genericKey"):
                if not str(spec.get(k, "")).strip():
                    errors.append(
                        f"drugRegimens[{di}] '{label}' doseSpec.{k} must be a non-empty string"
                    )
            if "durationDays" not in spec:
                errors.append(
                    f"drugRegimens[{di}] '{label}' doseSpec.durationDays key must be present "
                    f"(number of days, or null only if genuinely open-ended e.g. lifelong "
                    f"therapy) — an absent key is an authoring gap, not 'open-ended'"
                )
            else:
                dd = spec["durationDays"]
                if dd is not None and (isinstance(dd, bool) or not isinstance(dd, (int, float))):
                    errors.append(
                        f"drugRegimens[{di}] '{label}' doseSpec.durationDays must be a number "
                        f"or null, got {dd!r}"
                    )
            taper = spec.get("taper")
            if taper is not None and not (
                isinstance(taper, dict) and str(taper.get("schedule", "")).strip()
            ):
                errors.append(
                    f"drugRegimens[{di}] '{label}' doseSpec.taper must be null or "
                    f"{{schedule: <non-empty string>}}"
                )
            for adj_key, band_key in (("renalAdjustment", "egfrBand"),
                                      ("hepaticAdjustment", "severityBand")):
                adj = spec.get(adj_key)
                if not isinstance(adj, list):
                    errors.append(
                        f"drugRegimens[{di}] '{label}' doseSpec.{adj_key} must be an array "
                        f"(empty array allowed when the drug needs no adjustment)"
                    )
                else:
                    for ai, a in enumerate(adj):
                        if not isinstance(a, dict) or not str(a.get(band_key, "")).strip() \
                                or not str(a.get("action", "")).strip():
                            errors.append(
                                f"drugRegimens[{di}] '{label}' doseSpec.{adj_key}[{ai}] must "
                                f"be an object with non-empty '{band_key}' and 'action'"
                            )
            gkey = str(spec.get("genericKey", "")).strip()
            if gkey:
                if generic_names is None:
                    generic_names = _load_generic_names()
                if not generic_names:
                    errors.append(
                        f"drugRegimens[{di}] '{label}' doseSpec.genericKey cannot be verified — "
                        f"medex generic DB index unavailable"
                    )
                elif not _generic_resolves(gkey, generic_names):
                    errors.append(
                        f"drugRegimens[{di}] '{label}' doseSpec.genericKey '{gkey}' does not "
                        f"resolve to any generic in the medex DB"
                    )
            rids = spec.get("refIds")
            if not isinstance(rids, list) or not rids:
                errors.append(
                    f"drugRegimens[{di}] '{label}' doseSpec.refIds must be a non-empty "
                    f"array of references[] refId integers"
                )
            else:
                if any(not isinstance(r, int) or isinstance(r, bool) for r in rids):
                    errors.append(
                        f"drugRegimens[{di}] '{label}' doseSpec.refIds must all be "
                        f"integers: {rids}"
                    )
                orphan = [r for r in rids if isinstance(r, int) and r not in ref_ids]
                if orphan:
                    errors.append(
                        f"drugRegimens[{di}] '{label}' doseSpec.refIds {orphan} have no "
                        f"matching references[] refId"
                    )

    # --- Link integrity: relatedTopicIds / crossReferences must resolve ---
    related = topic.get("relatedTopicIds") or []
    crossrefs = topic.get("crossReferences") or []
    if isinstance(related, list) and isinstance(crossrefs, list) and (related or crossrefs):
        known_ids, known_titles = _load_topic_index()
        for rid in related:
            r = str(rid).strip()
            if r and r not in known_ids:
                _v5_fail(
                    f"relatedTopicIds entry '{r}' matches no topicId in the enriched corpus"
                )
        for c in crossrefs:
            cs = str(c).strip()
            if cs and cs not in known_ids and cs.lower() not in known_titles:
                _v5_fail(
                    f"crossReferences entry '{cs[:60]}' resolves to no known topic "
                    f"(by exact topicId or exact title)"
                )

    # --- facetAnchors: map facet topicId -> a heading inside THIS topic ---
    # Full topics may declare deep-link anchors for their facet records. Both
    # directions are checked as warnings: the facet topicId must exist in the
    # corpus index, and the anchor value must match a real ContentBlock heading
    # in this topic's own management/diagnosis/complication/background sections.
    anchors = topic.get("facetAnchors")
    if isinstance(anchors, dict) and anchors:
        known_ids, _ = _load_topic_index()
        own_headings = set()
        for sec_name in ("managementSections", "diagnosisSections",
                         "complicationSections", "backgroundInformation"):
            blocks = topic.get(sec_name, [])
            if not isinstance(blocks, list):
                continue
            for b in blocks:
                if isinstance(b, dict):
                    h = str(b.get("heading") or "").strip().lower()
                    if h:
                        own_headings.add(h)
        for facet_id, heading in anchors.items():
            fid = str(facet_id).strip()
            if fid and fid not in known_ids:
                warnings.append(
                    f"facetAnchors key '{fid}' resolves to no topicId in the "
                    f"enriched corpus index"
                )
            hs = str(heading).strip()
            if hs and hs.lower() not in own_headings:
                warnings.append(
                    f"facetAnchors['{fid}'] heading '{hs[:60]}' matches no heading "
                    f"in this topic's managementSections/diagnosisSections/"
                    f"complicationSections/backgroundInformation"
                )

    std_line = (
        "v5 — all v4 + v5 gates enforced as errors" if v5
        else "v4 (vancouver) — gates enforced as errors" if v4
        else "legacy — v4/v5 gates reported as warnings"
    )
    print(f"topic:    {title}")
    print(f"id:       {topic_id}")
    print(f"chars:    {char_count:,}")
    print(f"standard: {std_line}")
    print(f"errors:   {len(errors)}")
    for e in errors:
        print(f"  - {e}")
    print(f"warnings: {len(warnings)}")
    for w in warnings:
        print(f"  - {w}")

    sys.exit(0 if not errors else 1)


if __name__ == "__main__":
    main()
