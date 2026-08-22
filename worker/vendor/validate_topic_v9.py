#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
validate_topic_v9.py
====================

Validator for the DoctorsHero RX Clinical Knowledge Hub, content standard v9.

Editorial owner:
    Dr. A.F.M. Helal Uddin, MRCP(UK), FRCP(London)
    Associate Professor of Medicine
    Sir Salimullah Medical College, Dhaka, Bangladesh

WHAT THIS DOES
--------------
Takes the JSON an LLM produced against the V9 Ultimate Master Prompt and answers
one question: *is this safe and complete enough to publish?*

It runs four layers of checks:

    Layer 1  STRUCTURAL   Does it parse? Are forbidden keys gone? Are enums legal?
    Layer 2  EVIDENCE     Do citations resolve 1:1? Are DOIs real? Is [EC] within quota?
    Layer 3  DENSITY      Is the text actually decision-dense, or is it padded filler?
    Layer 4  NETWORK      (opt-in) Do the DOIs resolve on CrossRef? Do media URLs return 200?

USAGE
-----
    # Basic validation
    python validate_topic_v9.py topic.json

    # Full run with network verification and a machine-readable report
    python validate_topic_v9.py topic.json \
        --check-dois --check-media --mailto you@example.com \
        --json-report report.json --emit-repair-prompts

    # Merge a multi-pass generation (PART 7 of the prompt) then validate
    python validate_topic_v9.py --merge p1.json p2.json p3.json p4.json -o merged.json
    python validate_topic_v9.py merged.json

    # Normalise the non-ASCII punctuation that breaks downstream parsers
    python validate_topic_v9.py topic.json --autofix-ascii -o clean.json

EXIT CODES
----------
    0  passed (no ERROR findings)
    1  failed (at least one ERROR, or WARN with --strict)
    2  usage / IO error

DEPENDENCIES
------------
    Standard library only. Network checks use urllib. No pip install required.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple
from urllib import request as urlrequest
from urllib import error as urlerror


# ══════════════════════════════════════════════════════════════════════════
# SECTION 1 — CONSTANTS: every rule from the V9 prompt, in one editable place
# ══════════════════════════════════════════════════════════════════════════

CONTENT_STANDARD = "v9"

# --- Hard floors (V1-V18 in the prompt). Overridable from the CLI. ---------
DEFAULT_FLOORS: Dict[str, int] = {
    "min_json_chars": 100_000,          # V1
    "min_mgmt_plus_regimens_chars": 30_000,   # V10
    "min_management_blocks": 7,         # V9
    "min_points_per_block": 4,          # V9
    "min_point_chars": 200,             # V9
    "min_differentials": 3,             # V5
    "min_prognosis": 5,                 # V6
    "min_precise_dosing": 2,            # V7
    "min_drug_regimens": 6,             # V8
    "min_interaction_flags": 4,         # V13
    "min_do_not_do": 5,                 # V17
    "min_red_flags": 5,                 # V17
    "min_diagnostic_tests": 3,          # V17
}

# --- Thresholds for the "is this padded?" heuristics -----------------------
EC_MAX_RATIO = 0.15                 # V18: [EC] markers <= 15% of blocks per section
NGRAM_SIZE = 8                      # window for repetition detection
NGRAM_REPEAT_MAX = 0.25             # >25% repeated 8-grams == padding
NUMERIC_DENSITY_CHARS = 120         # target: >=1 quantity per 120 chars

# --- Forbidden top-level keys (V14) ---------------------------------------
FORBIDDEN_TOPIC_KEYS = {"treatmentLines", "treatment"}

# --- Non-ASCII punctuation that silently breaks parsers and search indexes -
# U+2010 hyphen, U+2011 non-breaking hyphen, U+2012..2015 dashes,
# U+2018/19 smart single quotes, U+201C/D smart double quotes,
# U+00A0 non-breaking space, U+2212 minus sign, U+2026 ellipsis.
BAD_PUNCT_MAP = {
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-",
    "\u2014": "-", "\u2015": "-", "\u2212": "-",
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"',
    "\u00a0": " ", "\u2007": " ", "\u202f": " ", "\u2009": " ",
    "\u2026": "...",
}
BAD_PUNCT_RE = re.compile("[" + "".join(BAD_PUNCT_MAP.keys()) + "]")

# --- Citation markers ------------------------------------------------------
# Matches [3], [3,7], [3-5], [3, 7-9]  -- captures the inner body for parsing.
CITE_RE = re.compile(r"\[(\d+(?:\s*[,\-]\s*\d+)*)\]")
EC_RE = re.compile(r"\[EC(?::\s*\d+)?\]")      # matches [EC] and [EC:3]
EC_ID_RE = re.compile(r"\[EC:\s*(\d+)\]")      # captures the ecId
EC_BARE_RE = re.compile(r"\[EC\]")               # invalid in v9 - must be numbered

# --- Enums from the prompt schema -----------------------------------------
DOSE_UNITS = {"mg", "mcg", "g", "mL", "units", "IU", "mEq", "mmol"}
DOSE_ROUTES = {"PO", "IV", "IM", "SC", "PR", "SL", "TD", "INH", "TOP"}
DOSE_FREQS = {"OD", "BD", "TDS", "QID", "q4h", "q6h", "q8h",
              "q12h", "q24h", "PRN"}
MEDIA_KINDS = {"image", "gif", "animation", "video"}
MEDIA_SECTION_KEYS = {
    "overview", "background", "pathophysiology", "diagnosis", "management",
    "complications", "monitoring", "patientEducation",
}
ALLOWED_LICENSES = {
    "CC BY-SA 4.0", "CC BY 4.0", "CC0", "public domain",
    "YouTube ToS", "Vimeo ToS",
}
NC_LICENSE_RE = re.compile(r"\bNC\b|non[- ]?commercial", re.IGNORECASE)

# --- Required key sets -----------------------------------------------------
DRUG_REGIMEN_KEYS = [
    "drug", "indication", "initialDose", "titration", "maintenanceDose",
    "termination", "alternatives", "adverseEffectManagement", "monitoring",
    "genericKeys",
]
PRECISE_DOSING_KEYS = [
    "drug", "indication", "standardDose", "doseReductionCriteria",
    "renalAdjustment", "hepaticAdjustment", "administration", "onsetOffset",
]
TOPIC_METADATA_KEYS = [
    "topicId", "topicName", "specialty", "contentStandard", "version",
    "status", "knowledgeAsOf", "locale", "codes", "safetyNotice",
]

# --- Society guideline families (V4) --------------------------------------
SOCIETY_PATTERNS = [
    r"\bAAP\b", r"\bAHA\b", r"\bACC\b", r"\bESC\b", r"\bNICE\b", r"\bWHO\b",
    r"\bIDSA\b", r"\bKDIGO\b", r"\bADA\b", r"\bGOLD\b", r"\bGINA\b",
    r"\bECCO\b", r"\bEASL\b", r"\bACR\b", r"\bASH\b", r"\bAAN\b",
    r"\bRCOG\b", r"\bSurviving Sepsis\b", r"\bATS\b", r"\bERS\b",
]
SOCIETY_RE = re.compile("|".join(SOCIETY_PATTERNS), re.IGNORECASE)

# --- Generic / lazy headings that must never appear (V11) -----------------
GENERIC_HEADINGS = {
    "management", "treatment", "complications", "comorbidities", "other",
    "general", "overview", "notes", "miscellaneous", "considerations",
    "general management", "supportive care", "further management",
}

# --- Template / placeholder padding (F8, F7) ------------------------------
PLACEHOLDER_PATTERNS = [
    r"lorem ipsum", r"\bas an ai\b", r"\bplaceholder\b", r"\bTODO\b",
    r"\bTBD\b", r"\bXXX\b", r"\bN/A\b\s*$", r"\bitem\s+\d+\s*[-:]",
    r"\bWeek\s+N\b", r"\bDay\s+N\b", r"\bmicro-?message\s+\d+",
    r"\bpoint\s+\d+\s*:", r"\bconsideration\s+[A-C]\s*:",
    r"\binsert\s+(?:dose|value|reference)\b", r"\[citation needed\]",
    r"\bpending verification\b",   # only legal in R-DRAFT; handled contextually
]
PLACEHOLDER_RE = re.compile("|".join(PLACEHOLDER_PATTERNS), re.IGNORECASE)

# --- Foreign-language signals in media metadata (B2 / 5.3) ----------------
FOREIGN_TOKENS = [
    "sintomi", "sindrome", "sindrom", "algorytm", "abbildung", "krankheit",
    "enfermedad", "sintomas", "tratamiento", "diagnostico", "maladie",
    "traitement", "doenca", "doença", "síndrome", "malattia", "behandlung",
    "diagnostik", "ziekte", "sygdom", "sjukdom", "choroba", "bolezn",
    "esquema", "esquema", "fisiopatologia", "fisiopatología", "patologia",
]
FOREIGN_TOKEN_RE = re.compile(
    r"(?:^|[\W_])(" + "|".join(FOREIGN_TOKENS) + r")(?:[\W_]|$)",
    re.IGNORECASE,
)
# Scripts that are never acceptable in an English-only asset.
NON_LATIN_RE = re.compile(
    r"[\u0400-\u04FF"      # Cyrillic
    r"\u0600-\u06FF"       # Arabic
    r"\u0900-\u097F"       # Devanagari
    r"\u0980-\u09FF"       # Bengali
    r"\u4E00-\u9FFF"       # CJK
    r"\u3040-\u30FF"       # Kana
    r"\uAC00-\uD7AF]"      # Hangul
)

# --- Canonical 7 management themes (4.1) ----------------------------------
CANONICAL_MGMT_THEMES = [
    ("first-line / emergent", [r"first[- ]line", r"emergent", r"initial", r"primary pathway"]),
    ("second-line / advanced", [r"second[- ]line", r"advanced", r"invasive"]),
    ("third-line / refractory", [r"third[- ]line", r"refractory", r"adjunct"]),
    ("special populations", [r"special population", r"pregnan", r"paediatric", r"pediatric"]),
    ("patient education", [r"patient education", r"self[- ]management"]),
    ("pearls / recommendations", [r"pearl", r"key clinical", r"recommendation"]),
    ("monitoring / follow-up", [r"monitor", r"follow[- ]?up", r"complication prevention"]),
]

DOI_URL_RE = re.compile(r"^https://doi\.org/10\.\d{4,9}/\S+$", re.IGNORECASE)
DOI_EXTRACT_RE = re.compile(r"10\.\d{4,9}/[^\s\"'<>]+")


# ══════════════════════════════════════════════════════════════════════════
# SECTION 2 — FINDING MODEL
# A "Finding" is one problem. Think of it as a line in a lab report: what is
# wrong, how bad it is, exactly where it is, and what to do about it.
# ══════════════════════════════════════════════════════════════════════════

class Severity(str, Enum):
    ERROR = "ERROR"     # blocks publication
    WARN = "WARN"       # publishable but degraded
    INFO = "INFO"       # advisory / metric


@dataclass
class Finding:
    severity: Severity
    code: str                   # stable machine code, e.g. "V9_POINT_TOO_SHORT"
    path: str                   # JSON path, e.g. "topic.managementSections[2].content[0]"
    message: str
    repair_key: Optional[str] = None   # top-level key to regenerate in the repair loop
    detail: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["severity"] = self.severity.value
        return d


@dataclass
class Report:
    findings: List[Finding] = field(default_factory=list)
    metrics: Dict[str, Any] = field(default_factory=dict)

    # --- convenience adders -------------------------------------------------
    def error(self, code: str, path: str, msg: str,
              repair_key: Optional[str] = None, detail: Optional[str] = None) -> None:
        self.findings.append(Finding(Severity.ERROR, code, path, msg, repair_key, detail))

    def warn(self, code: str, path: str, msg: str,
             repair_key: Optional[str] = None, detail: Optional[str] = None) -> None:
        self.findings.append(Finding(Severity.WARN, code, path, msg, repair_key, detail))

    def info(self, code: str, path: str, msg: str,
             repair_key: Optional[str] = None, detail: Optional[str] = None) -> None:
        self.findings.append(Finding(Severity.INFO, code, path, msg, repair_key, detail))

    # --- summaries ----------------------------------------------------------
    def count(self, sev: Severity) -> int:
        return sum(1 for f in self.findings if f.severity == sev)

    @property
    def passed(self) -> bool:
        return self.count(Severity.ERROR) == 0


# ══════════════════════════════════════════════════════════════════════════
# SECTION 3 — SMALL HELPERS
# Each of these does one boring job so the checks above stay readable.
# ══════════════════════════════════════════════════════════════════════════

def walk_strings(node: Any, path: str = "") -> Iterable[Tuple[str, str]]:
    """Yield (json_path, string_value) for every string anywhere in the object.

    Like turning the whole JSON inside out so we can scan all prose at once.
    """
    if isinstance(node, str):
        yield path, node
    elif isinstance(node, dict):
        for k, v in node.items():
            yield from walk_strings(v, f"{path}.{k}" if path else k)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from walk_strings(v, f"{path}[{i}]")


def parse_citation_markers(text: str) -> Set[int]:
    """Turn '[3,7]' and '[3-5]' into {3,7} and {3,4,5}.

    The prompt allows Vancouver ranges, so '[3-5]' means three separate refs.
    """
    found: Set[int] = set()
    for body in CITE_RE.findall(text):
        # Split on commas first, then expand any hyphen ranges.
        for part in body.split(","):
            part = part.strip()
            if "-" in part:
                try:
                    lo, hi = (int(x.strip()) for x in part.split("-", 1))
                    if lo <= hi and hi - lo < 200:      # sanity guard
                        found.update(range(lo, hi + 1))
                except ValueError:
                    continue
            else:
                try:
                    found.add(int(part))
                except ValueError:
                    continue
    return found


def parse_ec_markers(text: str) -> Set[int]:
    """Turn '[EC:3]' into {3}. The EC sequence is separate from refIds."""
    return {int(m) for m in EC_ID_RE.findall(text)}


def strip_markers(text: str) -> str:
    """Remove [3] and [EC:n] so marker digits don't count as clinical numbers."""
    return EC_RE.sub(" ", CITE_RE.sub(" ", text))


def count_quantities(text: str) -> int:
    """Count clinical quantities: numbers, percentages, ranges, doses.

    We strip citations first, otherwise '[3]' would masquerade as a dose.
    """
    clean = strip_markers(text)
    return len(re.findall(r"\d+(?:\.\d+)?", clean))


def has_quantity(text: str) -> bool:
    return count_quantities(text) > 0


def normalise_tokens(text: str) -> List[str]:
    """Lowercase word tokens, used for repetition detection."""
    return re.findall(r"[a-z0-9]+", strip_markers(text).lower())


def ngram_repeat_ratio(text: str, n: int = NGRAM_SIZE) -> float:
    """Fraction of n-grams that are duplicates. High == the model is padding.

    Analogy: if a student writes the same eight-word phrase over and over to
    fill a page, this number climbs. Original writing sits near zero.
    """
    toks = normalise_tokens(text)
    if len(toks) < n * 2:
        return 0.0
    grams = [tuple(toks[i:i + n]) for i in range(len(toks) - n + 1)]
    if not grams:
        return 0.0
    counts = Counter(grams)
    duplicated = sum(c - 1 for c in counts.values() if c > 1)
    return duplicated / len(grams)


def serialized_len(obj: Any) -> int:
    """Character length of the object as compact JSON, matching floor V1."""
    return len(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))


def is_nonempty_str(v: Any) -> bool:
    return isinstance(v, str) and v.strip() != ""


def get(obj: Any, key: str, default: Any = None) -> Any:
    return obj.get(key, default) if isinstance(obj, dict) else default


def wikimedia_filename(url: str) -> Optional[str]:
    """Pull 'Heart_diagram.png' out of any Wikimedia-style URL for dedup."""
    if not isinstance(url, str):
        return None
    m = re.search(r"/(?:File:)?([^/]+\.(?:png|jpe?g|gif|webp))(?:$|[?#])", url, re.I)
    return m.group(1).lower() if m else None


def title_root(title: str) -> str:
    """Fuzzy key for title dedup: lowercase, alphanumerics only, digits dropped."""
    t = unicodedata.normalize("NFKD", title or "").lower()
    t = re.sub(r"[^a-z]+", "", t)
    return t[:40]


def truncate(s: str, n: int = 90) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 3] + "..."


# ══════════════════════════════════════════════════════════════════════════
# SECTION 4 — THE VALIDATOR
# ══════════════════════════════════════════════════════════════════════════

class TopicValidator:
    """Runs every v9 check against one parsed topic document."""

    def __init__(
        self,
        doc: Dict[str, Any],
        raw_text: str,
        floors: Dict[str, int],
        extensions_on: bool = True,
        reference_mode: str = "R-STRICT",
        media_mode: str = "QUERY-ONLY",
    ) -> None:
        self.doc = doc
        self.raw_text = raw_text
        self.floors = floors
        self.extensions_on = extensions_on
        self.reference_mode = reference_mode
        self.media_mode = media_mode

        self.topic: Dict[str, Any] = get(doc, "topic", {}) or {}
        self.media: List[Any] = get(doc, "media", []) or []
        self.audit: Dict[str, Any] = get(doc, "_selfAudit", {}) or {}

        self.report = Report()
        # Cached during the run, reused by several checks.
        self._cited_refids: Set[int] = set()
        self._declared_refids: Set[int] = set()

    # ---------------------------------------------------------------- run --
    def run(self) -> Report:
        checks = [
            self.check_structure,
            self.check_forbidden_keys,
            self.check_ascii_hygiene,
            self.check_placeholders,
            self.check_char_floors,
            self.check_management_sections,
            self.check_diagnosis_sections,
            self.check_references,
            self.check_expert_consensus,
            self.check_drug_regimens,
            self.check_precise_dosing,
            self.check_prognosis,
            self.check_differentials,
            self.check_interaction_flags,
            self.check_comorbidity_complication,
            self.check_monitoring,
            self.check_v9_extensions,
            self.check_media,
            self.check_padding_heuristics,
            self.check_self_audit,
        ]
        for fn in checks:
            try:
                fn()
            except Exception as exc:   # a broken check must never hide the others
                self.report.error(
                    "VALIDATOR_INTERNAL", fn.__name__,
                    f"Check crashed: {type(exc).__name__}: {exc}",
                )
        return self.report

    # ------------------------------------------------------------ layer 1 --
    def check_structure(self) -> None:
        """V-structural: the two mandatory top-level keys must exist."""
        if "topic" not in self.doc:
            self.report.error("STRUCT_NO_TOPIC", "$", "Missing top-level 'topic' key.")
        if "media" not in self.doc:
            self.report.error("STRUCT_NO_MEDIA", "$",
                              "Missing top-level 'media' key (may be an empty array).")
        elif not isinstance(self.media, list):
            self.report.error("STRUCT_MEDIA_TYPE", "media", "'media' must be an array.")

        # Every core v8 field must be present.
        core = [
            "summary", "etiology", "pathophysiology", "presentation", "workup",
            "diagnosisSections", "managementSections", "drugRegimens",
            "preciseDosing", "prognosisQuantitative", "differentialDiagnosis",
            "comorbidityManagement", "complicationManagement", "monitoring",
            "drugInteractionFlags", "references",
        ]
        for k in core:
            if k not in self.topic:
                self.report.error("STRUCT_MISSING_FIELD", f"topic.{k}",
                                  f"Mandatory v8 core field '{k}' is absent.",
                                  repair_key=k)

    def check_forbidden_keys(self) -> None:
        """V14: treatmentLines and treatment are deleted in v7+."""
        for k in FORBIDDEN_TOPIC_KEYS:
            if k in self.topic:
                self.report.error(
                    "V14_FORBIDDEN_KEY", f"topic.{k}",
                    f"Forbidden key '{k}' present. All treatment logic must live "
                    f"inside managementSections.",
                    repair_key="managementSections",
                )

    def check_ascii_hygiene(self) -> None:
        """The non-ASCII punctuation catcher.

        Smart quotes and non-breaking hyphens look identical to a human but
        break parsers, search indexes and generic-name matching.
        """
        offenders: List[Tuple[str, str, str]] = []
        for path, s in walk_strings(self.doc):
            m = BAD_PUNCT_RE.search(s)
            if m:
                ch = m.group(0)
                offenders.append((path, ch, f"U+{ord(ch):04X}"))
        self.report.metrics["nonAsciiPunctuationHits"] = len(offenders)
        if offenders:
            sample = "; ".join(f"{p} ({cp})" for p, _, cp in offenders[:5])
            self.report.error(
                "ASCII_PUNCT", "$",
                f"{len(offenders)} string(s) contain non-ASCII punctuation "
                f"(smart quotes / non-breaking or en dashes). Run --autofix-ascii.",
                detail=sample,
            )
        # Keys must be pure ASCII.
        for path, _ in walk_strings(self.doc):
            for seg in re.split(r"[.\[\]]", path):
                if seg and not seg.isascii():
                    self.report.error("ASCII_KEY", path, "Non-ASCII character in a JSON key.")
                    break

    def check_placeholders(self) -> None:
        """F7 / F8: template scaffolding and placeholder text."""
        hits = 0
        for path, s in walk_strings(self.topic):
            if len(s) < 12:
                continue
            m = PLACEHOLDER_RE.search(s)
            if not m:
                continue
            token = m.group(0).lower()
            # 'pending verification' is legal ONLY in R-DRAFT reference stubs.
            if "pending verification" in token:
                if self.reference_mode == "R-DRAFT" and path.startswith("references"):
                    continue
            hits += 1
            self.report.error(
                "F8_PLACEHOLDER", f"topic.{path}",
                f"Placeholder/template text detected: '{truncate(m.group(0), 40)}'.",
                detail=truncate(s, 140),
            )
        self.report.metrics["placeholderHits"] = hits

    def check_char_floors(self) -> None:
        """V1 and V10: the two length floors."""
        total = serialized_len({"topic": self.topic, "media": self.media})
        self.report.metrics["charCountTopicPlusMedia"] = total
        floor = self.floors["min_json_chars"]
        if total < floor:
            self.report.error(
                "V1_TOTAL_CHARS", "$",
                f"Combined topic+media length {total:,} < floor {floor:,}. "
                f"Add real clinical substance or declare the shortfall in "
                f"_selfAudit.failedFloors - never pad.",
            )

        mgmt_blob = json.dumps(
            {"m": self.topic.get("managementSections", []),
             "d": self.topic.get("drugRegimens", [])},
            ensure_ascii=False,
        )
        self.report.metrics["charCountManagementPlusRegimens"] = len(mgmt_blob)
        if len(mgmt_blob) < self.floors["min_mgmt_plus_regimens_chars"]:
            self.report.error(
                "V10_MGMT_CHARS", "topic.managementSections+drugRegimens",
                f"Management + regimens length {len(mgmt_blob):,} < floor "
                f"{self.floors['min_mgmt_plus_regimens_chars']:,}.",
                repair_key="managementSections",
            )

    # ------------------------------------------------------------ layer 2 --
    def _iter_blocks(self, key: str):
        """Yield (index, block, heading, content_list) for a *Sections array."""
        for i, block in enumerate(self.topic.get(key, []) or []):
            if not isinstance(block, dict):
                self.report.error(f"BLOCK_TYPE", f"topic.{key}[{i}]",
                                  "Section block must be an object.", repair_key=key)
                continue
            yield i, block, block.get("heading", ""), block.get("content", []) or []

    def check_management_sections(self) -> None:
        """V9 + V12: the heaviest floor in the whole standard."""
        key = "managementSections"
        blocks = self.topic.get(key, []) or []
        n_blocks = len(blocks)
        self.report.metrics["managementBlocks"] = n_blocks

        if n_blocks < self.floors["min_management_blocks"]:
            self.report.error(
                "V9_BLOCK_COUNT", f"topic.{key}",
                f"{n_blocks} management blocks < required "
                f"{self.floors['min_management_blocks']}.",
                repair_key=key,
            )

        total_points = 0
        short_points = 0
        no_number_points = 0
        no_citation_blocks = 0
        min_len = None
        densities: List[float] = []

        for i, block, heading, content in self._iter_blocks(key):
            path = f"topic.{key}[{i}]"
            if not is_nonempty_str(heading):
                self.report.error("V9_NO_HEADING", path, "Block heading is empty.",
                                  repair_key=key)
            if not content:
                self.report.error("V9_EMPTY_BLOCK", path,
                                  f"Block '{truncate(heading,50)}' has no content points.",
                                  repair_key=key)
                continue
            if len(content) < self.floors["min_points_per_block"]:
                self.report.error(
                    "V9_POINTS_PER_BLOCK", path,
                    f"Block '{truncate(heading,50)}' has {len(content)} points "
                    f"< required {self.floors['min_points_per_block']}.",
                    repair_key=key,
                )

            block_has_citation = False
            for j, point in enumerate(content):
                ppath = f"{path}.content[{j}]"
                if not isinstance(point, dict) or not is_nonempty_str(point.get("text")):
                    self.report.error("V9_POINT_SHAPE", ppath,
                                      "ContentPoint must be {'text': str, 'level': int}.",
                                      repair_key=key)
                    continue
                text = point["text"]
                total_points += 1
                L = len(text)
                min_len = L if min_len is None else min(min_len, L)

                if L < self.floors["min_point_chars"]:
                    short_points += 1
                    self.report.error(
                        "V9_POINT_TOO_SHORT", ppath,
                        f"ContentPoint is {L} chars < {self.floors['min_point_chars']}.",
                        repair_key=key, detail=truncate(text, 120),
                    )
                if not has_quantity(text):
                    no_number_points += 1
                    self.report.error(
                        "V9_POINT_NO_NUMERIC", ppath,
                        "ContentPoint contains no dose, threshold, interval or score "
                        "(citation digits do not count).",
                        repair_key=key, detail=truncate(text, 120),
                    )
                if parse_citation_markers(text) or EC_RE.search(text):
                    block_has_citation = True
                densities.append(count_quantities(text) / max(L, 1) * NUMERIC_DENSITY_CHARS)

            if not block_has_citation:
                no_citation_blocks += 1
                self.report.error(
                    "V12_BLOCK_NO_CITATION", path,
                    f"Block '{truncate(heading,50)}' contains no [N] or [EC] marker.",
                    repair_key=key,
                )

        self.report.metrics.update({
            "managementPointsTotal": total_points,
            "pointsBelowMinChars": short_points,
            "pointsWithoutNumeric": no_number_points,
            "minPointCharLength": min_len or 0,
            "meanNumericDensityPer120Chars": round(
                sum(densities) / len(densities), 2) if densities else 0.0,
        })

        if densities:
            mean_density = sum(densities) / len(densities)
            if mean_density < 1.0:
                self.report.warn(
                    "DENSITY_LOW", f"topic.{key}",
                    f"Mean decision density {mean_density:.2f} quantities per "
                    f"{NUMERIC_DENSITY_CHARS} chars (target >= 1.0). Text is "
                    f"narrative rather than actionable.",
                    repair_key=key,
                )

        # Theme coverage (advisory - headings may be reworded).
        headings_blob = " || ".join(str(b.get("heading", "")) for b in blocks).lower()
        missing = [name for name, pats in CANONICAL_MGMT_THEMES
                   if not any(re.search(p, headings_blob) for p in pats)]
        if missing:
            self.report.warn(
                "V9_THEME_COVERAGE", f"topic.{key}",
                f"Canonical theme(s) not detected in headings: {', '.join(missing)}.",
                repair_key=key,
            )

    def check_diagnosis_sections(self) -> None:
        """V12 for diagnosisSections: every block needs a citation marker."""
        key = "diagnosisSections"
        for i, block, heading, content in self._iter_blocks(key):
            path = f"topic.{key}[{i}]"
            if not content:
                self.report.error("DX_EMPTY_BLOCK", path,
                                  f"Block '{truncate(heading,50)}' has no content.",
                                  repair_key=key)
                continue
            blob = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
            if not (parse_citation_markers(blob) or EC_RE.search(blob)):
                self.report.error("V12_BLOCK_NO_CITATION", path,
                                  f"Block '{truncate(heading,50)}' has no [N]/[EC] marker.",
                                  repair_key=key)

    def check_references(self) -> None:
        """V2, V3, V4: the reference integrity core.

        Two sets are compared: refIds declared in references[] and refIds
        actually cited as [N] anywhere in the text. They must match exactly.
        """
        refs = self.topic.get("references", []) or []
        declared: Set[int] = set()
        seen_ids: Counter = Counter()

        for i, r in enumerate(refs):
            path = f"topic.references[{i}]"
            if not isinstance(r, dict):
                self.report.error("V2_REF_SHAPE", path, "Reference must be an object.",
                                  repair_key="references")
                continue
            rid = r.get("refId")
            if not isinstance(rid, int) or rid <= 0:
                self.report.error("V2_REF_ID", path,
                                  f"refId must be a positive integer, got {rid!r}.",
                                  repair_key="references")
            else:
                declared.add(rid)
                seen_ids[rid] += 1
            if not is_nonempty_str(r.get("citation")):
                self.report.error("V2_REF_CITATION", path, "Empty 'citation' string.",
                                  repair_key="references")
            url = r.get("url")
            if self.reference_mode == "R-DRAFT":
                if url:
                    self.report.error(
                        "R_DRAFT_DOI", path,
                        "R-DRAFT mode forbids emitting URLs/DOIs - they cannot be "
                        "verified and become fabrications.",
                        repair_key="references")
            else:
                if not (isinstance(url, str) and DOI_URL_RE.match(url.strip())):
                    self.report.error(
                        "V2_REF_URL", path,
                        f"url must be a full https://doi.org/... link, got {url!r}.",
                        repair_key="references")

        for rid, c in seen_ids.items():
            if c > 1:
                self.report.error("V2_REF_DUPLICATE", "topic.references",
                                  f"refId {rid} declared {c} times.",
                                  repair_key="references")

        # Collect every [N] used anywhere in the topic except inside references[].
        cited: Set[int] = set()
        for path, s in walk_strings(self.topic):
            if path.startswith("references"):
                continue
            cited |= parse_citation_markers(s)

        self._declared_refids, self._cited_refids = declared, cited
        dangling = sorted(cited - declared)
        unused = sorted(declared - cited)

        self.report.metrics.update({
            "referencesDeclared": len(declared),
            "refIdsCitedInText": len(cited),
            "danglingCitations": dangling,
            "unusedReferences": unused,
        })

        if dangling:
            self.report.error(
                "V3_DANGLING_CITATION", "topic.references",
                f"{len(dangling)} inline marker(s) point to non-existent refIds: "
                f"{dangling[:20]}.",
                repair_key="references")
        if unused:
            self.report.error(
                "V3_UNUSED_REFERENCE", "topic.references",
                f"{len(unused)} reference(s) are never cited: {unused[:20]}. "
                f"Cite them or remove them - the mapping must be 1:1.",
                repair_key="references")

        # V4: at least one society guideline family.
        ref_blob = json.dumps(refs, ensure_ascii=False)
        if not SOCIETY_RE.search(ref_blob):
            self.report.error(
                "V4_NO_SOCIETY_GUIDELINE", "topic.references",
                "No recognised society guideline family (AHA/ACC/ESC/NICE/WHO/"
                "IDSA/KDIGO/ADA/GOLD/GINA...) found in the reference list.",
                repair_key="references")

    def check_expert_consensus(self) -> None:
        """V18: [EC:n] is a pressure valve, not a loophole.

        Two independent tests:
          (a) INTEGRITY - the ecId set cited in the text must equal the ecId set
              declared in expertConsensusClaims[], exactly as for references.
          (b) QUOTA     - [EC:n] may carry no more than 15% of blocks per section.
        """
        claims = self.topic.get("expertConsensusClaims", []) or []

        # ---- (a) declared side -------------------------------------------
        declared: Set[int] = set()
        seen: Counter = Counter()
        for i, cobj in enumerate(claims):
            path = f"topic.expertConsensusClaims[{i}]"
            if not isinstance(cobj, dict):
                self.report.error("V18_EC_SHAPE", path, "Entry must be an object.",
                                  repair_key="expertConsensusClaims")
                continue
            eid = cobj.get("ecId")
            if not isinstance(eid, int) or isinstance(eid, bool) or eid <= 0:
                self.report.error("V18_EC_ID", f"{path}.ecId",
                                  f"ecId must be a positive integer, got {eid!r}.",
                                  repair_key="expertConsensusClaims")
            else:
                declared.add(eid)
                seen[eid] += 1
            for k in ("claim", "rationale", "section"):
                if not is_nonempty_str(cobj.get(k)):
                    self.report.error("V18_EC_FIELD", f"{path}.{k}",
                                      f"Required field '{k}' is empty.",
                                      repair_key="expertConsensusClaims")
        for eid, n in seen.items():
            if n > 1:
                self.report.error("V18_EC_DUPLICATE", "topic.expertConsensusClaims",
                                  f"ecId {eid} declared {n} times.",
                                  repair_key="expertConsensusClaims")

        # ---- (a) cited side ----------------------------------------------
        cited: Set[int] = set()
        for path, text in walk_strings(self.topic):
            if path.startswith("expertConsensusClaims"):
                continue
            cited |= parse_ec_markers(text)
            if EC_BARE_RE.search(text):
                self.report.error(
                    "V18_EC_BARE", f"topic.{path}",
                    "Bare [EC] marker found. v9 requires a numbered [EC:n] that "
                    "maps to an ecId in expertConsensusClaims[].",
                    repair_key="expertConsensusClaims",
                    detail=truncate(text, 120))

        dangling = sorted(cited - declared)
        unused = sorted(declared - cited)
        if dangling:
            self.report.error("V18_EC_DANGLING", "topic.expertConsensusClaims",
                              f"[EC:n] marker(s) with no declared ecId: {dangling[:20]}.",
                              repair_key="expertConsensusClaims")
        if unused:
            self.report.error("V18_EC_UNUSED", "topic.expertConsensusClaims",
                              f"Declared ecId(s) never cited in the text: "
                              f"{unused[:20]}. The mapping must be 1:1.",
                              repair_key="expertConsensusClaims")

        self.report.metrics["ecIntegrity"] = {
            "declared": sorted(declared), "cited": sorted(cited),
            "dangling": dangling, "unused": unused,
            "isOneToOne": not dangling and not unused,
        }

        # ---- (b) quota ----------------------------------------------------
        ratios: Dict[str, float] = {}
        for key in ("managementSections", "diagnosisSections"):
            total = ec = 0
            for i, _block, _h, content in self._iter_blocks(key):
                for p in content:
                    if not isinstance(p, dict):
                        continue
                    total += 1
                    if EC_RE.search(p.get("text", "")):
                        ec += 1
            ratio = (ec / total) if total else 0.0
            ratios[key] = round(ratio, 3)
            if ratio > EC_MAX_RATIO:
                self.report.error(
                    "V18_EC_RATIO", f"topic.{key}",
                    f"{ec}/{total} blocks ({ratio:.0%}) rely on [EC:n] > cap "
                    f"{EC_MAX_RATIO:.0%}. This section is under-sourced - find "
                    f"real citations rather than widening the consensus valve.",
                    repair_key=key)
        self.report.metrics["expertConsensusRatioBySection"] = ratios

    # ------------------------------------------------------------ layer 3 --
    def _validate_dose_spec(self, spec: Any, path: str, drug: str) -> None:
        """v5 doseSpec structural validation - the EMR-executable payload."""
        if not isinstance(spec, dict):
            self.report.error("V8_NO_DOSESPEC", path,
                              f"drugRegimens entry '{drug}' has no doseSpec object.",
                              repair_key="drugRegimens")
            return

        amount = spec.get("amount")
        if not isinstance(amount, (int, float)) or isinstance(amount, bool) or amount <= 0:
            self.report.error("DOSESPEC_AMOUNT", f"{path}.amount",
                              f"amount must be a positive unquoted number, got {amount!r}.",
                              repair_key="drugRegimens")

        for fld, allowed in (("unit", DOSE_UNITS), ("route", DOSE_ROUTES),
                             ("frequency", DOSE_FREQS)):
            val = spec.get(fld)
            if val not in allowed:
                self.report.error(
                    f"DOSESPEC_{fld.upper()}", f"{path}.{fld}",
                    f"{fld}={val!r} is not in the allowed enum "
                    f"({', '.join(sorted(allowed))}).",
                    repair_key="drugRegimens")

        dur = spec.get("durationDays", "MISSING")
        if dur != "MISSING" and dur is not None:
            if not isinstance(dur, (int, float)) or isinstance(dur, bool) or dur <= 0:
                self.report.error("DOSESPEC_DURATION", f"{path}.durationDays",
                                  f"durationDays must be a positive number or null, "
                                  f"got {dur!r}.", repair_key="drugRegimens")

        if not is_nonempty_str(spec.get("maxDosePerDay")):
            self.report.error("DOSESPEC_MAXDOSE", f"{path}.maxDosePerDay",
                              "maxDosePerDay must be a non-empty string.",
                              repair_key="drugRegimens")

        for adj_key, band_key in (("renalAdjustment", "egfrBand"),
                                  ("hepaticAdjustment", "severityBand")):
            adj = spec.get(adj_key)
            if not isinstance(adj, list) or not adj:
                self.report.error(f"DOSESPEC_{adj_key.upper()}", f"{path}.{adj_key}",
                                  f"{adj_key} must be a non-empty array of "
                                  f"{{{band_key}, action}} objects.",
                                  repair_key="drugRegimens")
                continue
            for k, band in enumerate(adj):
                if not isinstance(band, dict) or not is_nonempty_str(band.get(band_key)) \
                        or not is_nonempty_str(band.get("action")):
                    self.report.error(
                        f"DOSESPEC_{adj_key.upper()}_ITEM", f"{path}.{adj_key}[{k}]",
                        f"Each entry needs non-empty '{band_key}' and 'action'.",
                        repair_key="drugRegimens")

        gk = spec.get("genericKey")
        if not is_nonempty_str(gk):
            self.report.error("DOSESPEC_GENERICKEY", f"{path}.genericKey",
                              "genericKey must be a bare lowercase generic name.",
                              repair_key="drugRegimens")
        else:
            if gk != gk.lower():
                self.report.error("DOSESPEC_GENERICKEY_CASE", f"{path}.genericKey",
                                  f"genericKey '{gk}' must be lowercase.",
                                  repair_key="drugRegimens")
            if re.search(r"\b(sodium|potassium|hydrochloride|hcl|sulfate|sulphate|"
                         r"tartrate|maleate|besylate|mesylate|succinate|fumarate|"
                         r"citrate|acetate)\b", gk, re.I):
                self.report.error("DOSESPEC_GENERICKEY_SALT", f"{path}.genericKey",
                                  f"genericKey '{gk}' includes a salt; use the bare "
                                  f"INN so DrugAliasResolver can bind it.",
                                  repair_key="drugRegimens")

        ref_ids = spec.get("refIds")
        if not isinstance(ref_ids, list) or not ref_ids:
            self.report.error("DOSESPEC_REFIDS", f"{path}.refIds",
                              "refIds must be a non-empty array of integers.",
                              repair_key="drugRegimens")
        else:
            for rid in ref_ids:
                if not isinstance(rid, int) or rid not in self._declared_refids:
                    self.report.error("DOSESPEC_REFID_DANGLING", f"{path}.refIds",
                                      f"refId {rid!r} is not declared in references[].",
                                      repair_key="drugRegimens")

        # Cross-check: doseSpec.amount should appear in the initialDose prose.
        # Advisory only - phrasing legitimately varies.
        if isinstance(amount, (int, float)) and not isinstance(amount, bool):
            init = str(spec.get("_initialDose", "") or "")
            if init:
                nums = {float(x) for x in re.findall(r"\d+(?:\.\d+)?", init)}
                if float(amount) not in nums:
                    self.report.warn(
                        "DOSESPEC_AMOUNT_MISMATCH", f"{path}.amount",
                        f"doseSpec.amount={amount} does not appear in initialDose "
                        f"text '{truncate(init, 60)}'.",
                        repair_key="drugRegimens")

    def check_drug_regimens(self) -> None:
        """V8: >=6 regimens, all 10 fields non-empty, valid doseSpec, stop rule."""
        regs = self.topic.get("drugRegimens", []) or []
        self.report.metrics["drugRegimens"] = len(regs)
        if len(regs) < self.floors["min_drug_regimens"]:
            self.report.error("V8_REGIMEN_COUNT", "topic.drugRegimens",
                              f"{len(regs)} regimens < required "
                              f"{self.floors['min_drug_regimens']}.",
                              repair_key="drugRegimens")

        for i, r in enumerate(regs):
            path = f"topic.drugRegimens[{i}]"
            if not isinstance(r, dict):
                self.report.error("V8_REGIMEN_SHAPE", path, "Regimen must be an object.",
                                  repair_key="drugRegimens")
                continue
            drug = str(r.get("drug", f"#{i}"))
            for k in DRUG_REGIMEN_KEYS:
                v = r.get(k)
                if k == "genericKeys":
                    if not isinstance(v, list) or not v or not all(
                            is_nonempty_str(x) for x in v):
                        self.report.error("V8_GENERICKEYS", f"{path}.genericKeys",
                                          f"'{drug}': genericKeys must be a non-empty "
                                          f"array of non-empty strings.",
                                          repair_key="drugRegimens")
                elif not is_nonempty_str(v):
                    self.report.error("V8_FIELD_EMPTY", f"{path}.{k}",
                                      f"'{drug}': required field '{k}' is empty.",
                                      repair_key="drugRegimens")

            # Every regimen must state a stop / taper / rebound rule (4.2).
            term = str(r.get("termination", ""))
            if term and not re.search(r"\bstop|taper|discontinu|withdraw|cease|"
                                      r"rebound|wean\b", term, re.I):
                self.report.warn("V8_NO_STOP_RULE", f"{path}.termination",
                                 f"'{drug}': termination text does not describe a stop, "
                                 f"taper or rebound rule.",
                                 repair_key="drugRegimens")

            spec = r.get("doseSpec")
            if isinstance(spec, dict):
                spec = dict(spec)
                spec["_initialDose"] = r.get("initialDose", "")
            self._validate_dose_spec(spec, f"{path}.doseSpec", drug)

            # High-alert drugs must be flagged (2.5).
            if re.search(r"insulin|heparin|warfarin|enoxaparin|methotrexate|"
                         r"morphine|fentanyl|potassium chloride|chemotherap|"
                         r"vincristine|digoxin", drug, re.I):
                if r.get("highAlert") is not True:
                    self.report.warn("SAFETY_HIGH_ALERT", f"{path}.highAlert",
                                     f"'{drug}' looks like a high-alert medication but "
                                     f"highAlert is not true.",
                                     repair_key="drugRegimens")

    def check_precise_dosing(self) -> None:
        """V7: >=2 entries, all 8 fields non-empty."""
        items = self.topic.get("preciseDosing", []) or []
        self.report.metrics["preciseDosing"] = len(items)
        if len(items) < self.floors["min_precise_dosing"]:
            self.report.error("V7_PRECISE_COUNT", "topic.preciseDosing",
                              f"{len(items)} entries < required "
                              f"{self.floors['min_precise_dosing']}.",
                              repair_key="preciseDosing")
        for i, d in enumerate(items):
            path = f"topic.preciseDosing[{i}]"
            if not isinstance(d, dict):
                self.report.error("V7_SHAPE", path, "Entry must be an object.",
                                  repair_key="preciseDosing")
                continue
            for k in PRECISE_DOSING_KEYS:
                if not is_nonempty_str(d.get(k)):
                    self.report.error("V7_FIELD_EMPTY", f"{path}.{k}",
                                      f"Required field '{k}' is empty.",
                                      repair_key="preciseDosing")

    def check_prognosis(self) -> None:
        """V6: >=5 entries, numeric estimates, real DOI URLs."""
        items = self.topic.get("prognosisQuantitative", []) or []
        self.report.metrics["prognosisQuantitative"] = len(items)
        if len(items) < self.floors["min_prognosis"]:
            self.report.error("V6_PROGNOSIS_COUNT", "topic.prognosisQuantitative",
                              f"{len(items)} entries < required "
                              f"{self.floors['min_prognosis']}.",
                              repair_key="prognosisQuantitative")
        for i, p in enumerate(items):
            path = f"topic.prognosisQuantitative[{i}]"
            if not isinstance(p, dict):
                self.report.error("V6_SHAPE", path, "Entry must be an object.",
                                  repair_key="prognosisQuantitative")
                continue
            for k in ("outcome", "estimate", "source"):
                if not is_nonempty_str(p.get(k)):
                    self.report.error("V6_FIELD_EMPTY", f"{path}.{k}",
                                      f"Required field '{k}' is empty.",
                                      repair_key="prognosisQuantitative")
            est = str(p.get("estimate", ""))
            if est and not has_quantity(est):
                self.report.error("V6_ESTIMATE_VAGUE", f"{path}.estimate",
                                  f"Estimate must be a specific numeric statement, "
                                  f"got '{truncate(est, 80)}'.",
                                  repair_key="prognosisQuantitative")
            doi = p.get("doi")
            if self.reference_mode != "R-DRAFT":
                if not (isinstance(doi, str) and DOI_URL_RE.match(doi.strip())):
                    self.report.error("V6_DOI", f"{path}.doi",
                                      f"doi must be a full https://doi.org/... URL, "
                                      f"got {doi!r}.",
                                      repair_key="prognosisQuantitative")

    def check_differentials(self) -> None:
        """V5: >=3 (target 5-10) with real distinguishing features."""
        items = self.topic.get("differentialDiagnosis", []) or []
        self.report.metrics["differentialDiagnosis"] = len(items)
        if len(items) < self.floors["min_differentials"]:
            self.report.error("V5_DDX_COUNT", "topic.differentialDiagnosis",
                              f"{len(items)} entries < required "
                              f"{self.floors['min_differentials']}.",
                              repair_key="differentialDiagnosis")
        elif len(items) < 5:
            self.report.warn("V5_DDX_THIN", "topic.differentialDiagnosis",
                             f"Only {len(items)} differentials; the prompt targets 5-10.",
                             repair_key="differentialDiagnosis")
        urgent = 0
        for i, d in enumerate(items):
            path = f"topic.differentialDiagnosis[{i}]"
            if not isinstance(d, dict):
                self.report.error("V5_SHAPE", path, "Entry must be an object.",
                                  repair_key="differentialDiagnosis")
                continue
            for k in ("condition", "distinguishingFeature"):
                if not is_nonempty_str(d.get(k)):
                    self.report.error("V5_FIELD_EMPTY", f"{path}.{k}",
                                      f"Required field '{k}' is empty.",
                                      repair_key="differentialDiagnosis")
            feat = str(d.get("distinguishingFeature", ""))
            if len(feat) < 60:
                self.report.warn("V5_FEATURE_THIN", f"{path}.distinguishingFeature",
                                 f"Distinguishing feature is only {len(feat)} chars; "
                                 f"add a decision rule, cut-off or red-flag threshold.",
                                 repair_key="differentialDiagnosis")
            if "[URGENT]" in feat or "[URGENT]" in str(d.get("condition", "")):
                urgent += 1
        if items and urgent == 0:
            self.report.warn("V5_NO_URGENT_FLAG", "topic.differentialDiagnosis",
                             "No differential is flagged [URGENT]; can't-miss diagnoses "
                             "must be marked.",
                             repair_key="differentialDiagnosis")

    def check_interaction_flags(self) -> None:
        """V13: >=4 interaction flags with mechanism, monitoring and action."""
        items = self.topic.get("drugInteractionFlags", []) or []
        self.report.metrics["drugInteractionFlags"] = len(items)
        if len(items) < self.floors["min_interaction_flags"]:
            self.report.error("V13_DDI_COUNT", "topic.drugInteractionFlags",
                              f"{len(items)} entries < required "
                              f"{self.floors['min_interaction_flags']}.",
                              repair_key="drugInteractionFlags")
        for i, f in enumerate(items):
            path = f"topic.drugInteractionFlags[{i}]"
            blob = json.dumps(f, ensure_ascii=False) if not isinstance(f, str) else f
            if not re.search(r"CYP|P-?gp|OATP?|QTc?|hERG|pharmacodynamic|"
                             r"additive|renal clearance|protein binding",
                             blob, re.I):
                self.report.warn("V13_NO_MECHANISM", path,
                                 "No pharmacokinetic/dynamic mechanism named "
                                 "(CYP450, P-gp, OATP, QTc/hERG, additive effect).",
                                 repair_key="drugInteractionFlags")
            if not has_quantity(blob):
                self.report.error("V13_NO_ACTION_NUMBER", path,
                                  "Interaction flag contains no numeric action or "
                                  "monitoring interval.",
                                  repair_key="drugInteractionFlags")

    def check_comorbidity_complication(self) -> None:
        """V11: non-empty, unique, specific headings."""
        for key in ("comorbidityManagement", "complicationManagement"):
            items = self.topic.get(key, []) or []
            self.report.metrics[key] = len(items)
            if not items:
                self.report.error("V11_EMPTY", f"topic.{key}",
                                  f"'{key}' must be a non-empty list.", repair_key=key)
                continue
            seen: Set[str] = set()
            for i, it in enumerate(items):
                path = f"topic.{key}[{i}]"
                if not isinstance(it, dict):
                    self.report.error("V11_SHAPE", path,
                                      "Entry must be {'heading','detail'}.", repair_key=key)
                    continue
                h = str(it.get("heading", "")).strip()
                d = str(it.get("detail", "")).strip()
                if not h:
                    self.report.error("V11_NO_HEADING", path, "Empty heading.",
                                      repair_key=key)
                    continue
                if not d:
                    self.report.error("V11_NO_DETAIL", path,
                                      f"'{truncate(h,40)}' has empty detail.",
                                      repair_key=key)
                norm = re.sub(r"\s+", " ", h.lower()).strip()
                if norm in GENERIC_HEADINGS:
                    self.report.error("V11_GENERIC_HEADING", path,
                                      f"Heading '{h}' is generic. Name the specific "
                                      f"clinical entity.", repair_key=key)
                if norm in seen:
                    self.report.error("V11_DUPLICATE_HEADING", path,
                                      f"Duplicate heading '{h}'.", repair_key=key)
                seen.add(norm)
                if d and not has_quantity(d):
                    self.report.warn("V11_NO_THRESHOLD", f"{path}.detail",
                                     f"'{truncate(h,40)}': detail has no recognition "
                                     f"threshold or dose.", repair_key=key)

    def check_monitoring(self) -> None:
        m = self.topic.get("monitoring")
        if not is_nonempty_str(m):
            self.report.error("MONITORING_EMPTY", "topic.monitoring",
                              "'monitoring' must be a non-empty string.",
                              repair_key="monitoring")
        elif not has_quantity(m):
            self.report.error("MONITORING_NO_NUMERIC", "topic.monitoring",
                              "'monitoring' contains no threshold or interval.",
                              repair_key="monitoring")

    def check_v9_extensions(self) -> None:
        """V17: the fields that make this beat UpToDate."""
        if not self.extensions_on:
            self.report.info("V9_EXT_OFF", "$", "V9 extensions disabled; skipping V17.")
            return

        meta = self.topic.get("topicMetadata")
        if not isinstance(meta, dict):
            self.report.error("V17_NO_METADATA", "topic.topicMetadata",
                              "topicMetadata object is missing.",
                              repair_key="topicMetadata")
        else:
            for k in TOPIC_METADATA_KEYS:
                if k not in meta or meta.get(k) in (None, "", {}):
                    self.report.error("V17_METADATA_FIELD", f"topic.topicMetadata.{k}",
                                      f"topicMetadata.{k} is missing or empty.",
                                      repair_key="topicMetadata")
            if meta.get("contentStandard") != CONTENT_STANDARD:
                self.report.warn("V17_STANDARD", "topic.topicMetadata.contentStandard",
                                 f"Expected '{CONTENT_STANDARD}', got "
                                 f"{meta.get('contentStandard')!r}.",
                                 repair_key="topicMetadata")
            if not is_nonempty_str(meta.get("safetyNotice")):
                self.report.error("SAFETY_NOTICE", "topic.topicMetadata.safetyNotice",
                                  "Mandatory safetyNotice string is absent.",
                                  repair_key="topicMetadata")

        for key, floor_key, label in (
            ("doNotDo", "min_do_not_do", "Choosing Wisely entries"),
            ("redFlags", "min_red_flags", "red flags"),
            ("diagnosticTestPerformance", "min_diagnostic_tests", "diagnostic test rows"),
        ):
            items = self.topic.get(key, []) or []
            self.report.metrics[key] = len(items)
            if len(items) < self.floors[floor_key]:
                self.report.error(f"V17_{key.upper()}_COUNT", f"topic.{key}",
                                  f"{len(items)} {label} < required "
                                  f"{self.floors[floor_key]}.", repair_key=key)

        # redFlags need a machine-checkable threshold.
        for i, rf in enumerate(self.topic.get("redFlags", []) or []):
            if isinstance(rf, dict) and not has_quantity(str(rf.get("threshold", ""))):
                self.report.error("V17_REDFLAG_THRESHOLD", f"topic.redFlags[{i}].threshold",
                                  "Red flag threshold must be numeric "
                                  "(e.g. 'SpO2 < 90% on room air').",
                                  repair_key="redFlags")

        # pointOfCareFlow must be a closed graph.
        flow = self.topic.get("pointOfCareFlow", []) or []
        if flow:
            term_re = re.compile(r"discharge|observ|admit|ICU|refer|operat|"
                                 r"theatre|palliat|step\s*\d+", re.I)
            for i, step in enumerate(flow):
                if not isinstance(step, dict):
                    continue
                for branch in ("ifYes", "ifNo"):
                    val = str(step.get(branch, ""))
                    if not val:
                        self.report.error("V17_FLOW_OPEN_BRANCH",
                                          f"topic.pointOfCareFlow[{i}].{branch}",
                                          "Branch is empty; every branch must terminate "
                                          "in a disposition or another step.",
                                          repair_key="pointOfCareFlow")
                    elif not term_re.search(val):
                        self.report.warn("V17_FLOW_NO_DISPOSITION",
                                         f"topic.pointOfCareFlow[{i}].{branch}",
                                         "Branch does not name a disposition or a "
                                         "next step.", repair_key="pointOfCareFlow")

        # localeContext must not invent precise prices (F5).
        lc = self.topic.get("localeContext")
        if isinstance(lc, dict):
            for i, da in enumerate(lc.get("drugAvailability", []) or []):
                if not isinstance(da, dict):
                    continue
                band = str(da.get("costBandPerMonth", ""))
                if band and re.search(r"\b\d{2,}\s*(?:BDT|Tk|taka|USD|\$)\b", band, re.I) \
                        and not re.match(r"^(low|moderate|high|very high|unknown)",
                                         band.strip(), re.I):
                    self.report.error("F5_INVENTED_PRICE",
                                      f"topic.localeContext.drugAvailability[{i}]",
                                      f"costBandPerMonth must be a band label, not a "
                                      f"precise price: '{truncate(band,60)}'.",
                                      repair_key="localeContext")

    def check_media(self) -> None:
        """B1-B6 / 5.x: dedup, language, licence, format, verification."""
        media = self.media if isinstance(self.media, list) else []
        self.report.metrics["mediaItems"] = len(media)

        seen_urls: Dict[str, int] = {}
        seen_files: Dict[str, int] = {}
        seen_titles: Dict[str, int] = {}
        section_counts: Counter = Counter()

        for i, m in enumerate(media):
            path = f"media[{i}]"
            if not isinstance(m, dict):
                self.report.error("MEDIA_SHAPE", path, "Media entry must be an object.",
                                  repair_key="media")
                continue

            kind = m.get("kind")
            if kind not in MEDIA_KINDS:
                self.report.error("MEDIA_KIND", f"{path}.kind",
                                  f"kind={kind!r} not in {sorted(MEDIA_KINDS)}.",
                                  repair_key="media")
            sk = m.get("sectionKey")
            if sk not in MEDIA_SECTION_KEYS:
                self.report.error("MEDIA_SECTIONKEY", f"{path}.sectionKey",
                                  f"sectionKey={sk!r} not in the allowed enum.",
                                  repair_key="media")
            else:
                section_counts[sk] += 1

            for k in ("title", "caption", "alt", "relevance"):
                if not is_nonempty_str(m.get(k)):
                    self.report.error("MEDIA_FIELD_EMPTY", f"{path}.{k}",
                                      f"'{k}' is empty.", repair_key="media")

            status = m.get("verificationStatus")
            url = m.get("proposedUrl")

            if self.media_mode == "QUERY-ONLY":
                if url:
                    self.report.error(
                        "F6_UNVERIFIED_URL", f"{path}.proposedUrl",
                        "MEDIA_MODE=QUERY-ONLY forbids proposedUrl; the model cannot "
                        "verify an image it never fetched. Emit searchDirectives.",
                        repair_key="media")
                sd = m.get("searchDirectives")
                if not isinstance(sd, dict) or not sd.get("queries"):
                    self.report.error("MEDIA_NO_DIRECTIVES", f"{path}.searchDirectives",
                                      "QUERY-ONLY mode requires searchDirectives.queries.",
                                      repair_key="media")
                if status != "query-only":
                    self.report.warn("MEDIA_STATUS", f"{path}.verificationStatus",
                                     f"Expected 'query-only', got {status!r}.",
                                     repair_key="media")
            else:  # VERIFIED
                if status != "fetched-ok":
                    self.report.error("F6_NOT_VERIFIED", f"{path}.verificationStatus",
                                      f"VERIFIED mode requires 'fetched-ok', got "
                                      f"{status!r}.", repair_key="media")
                if not is_nonempty_str(url) or not str(url).startswith("https://"):
                    self.report.error("B6_URL", f"{path}.proposedUrl",
                                      f"proposedUrl must be an HTTPS URL, got {url!r}.",
                                      repair_key="media")
                if is_nonempty_str(url):
                    u = str(url)
                    if u.lower().endswith(".svg"):
                        self.report.error("B6_SVG", f"{path}.proposedUrl",
                                          "SVG is not allowed.", repair_key="media")
                    if re.search(r"\.(mp4|webm|mov|avi)(\?|$)", u, re.I):
                        self.report.error("B6_VIDEO_FILE", f"{path}.proposedUrl",
                                          "Direct video file links are forbidden; use "
                                          "YouTube/Vimeo watch URLs.", repair_key="media")
                    if re.search(r"(?:gstatic|googleusercontent|encrypted-tbn|"
                                 r"webcache\.google)", u, re.I):
                        self.report.error("B4_PROXY_URL", f"{path}.proposedUrl",
                                          "Google-cached/proxy URL is forbidden; use the "
                                          "original host's permanent file URL.",
                                          repair_key="media")
                    if u in seen_urls:
                        self.report.error("B1_DUPLICATE_URL", f"{path}.proposedUrl",
                                          f"Duplicate of media[{seen_urls[u]}].",
                                          repair_key="media")
                    seen_urls[u] = i
                    fn = wikimedia_filename(u)
                    if fn:
                        if fn in seen_files:
                            self.report.error("B1_DUPLICATE_FILENAME",
                                              f"{path}.proposedUrl",
                                              f"Same filename '{fn}' as media"
                                              f"[{seen_files[fn]}].", repair_key="media")
                        seen_files[fn] = i

                lic = m.get("license")
                if not is_nonempty_str(lic):
                    self.report.error("B5_NO_LICENSE", f"{path}.license",
                                      "license is mandatory.", repair_key="media")
                else:
                    if NC_LICENSE_RE.search(str(lic)):
                        self.report.error("B5_NC_LICENSE", f"{path}.license",
                                          f"Non-commercial licence '{lic}' is rejected.",
                                          repair_key="media")
                    elif str(lic) not in ALLOWED_LICENSES:
                        self.report.warn("B5_UNKNOWN_LICENSE", f"{path}.license",
                                         f"Licence '{lic}' is not on the allow-list.",
                                         repair_key="media")
                if not is_nonempty_str(m.get("sourceName")):
                    self.report.error("B6_NO_SOURCE", f"{path}.sourceName",
                                      "sourceName is mandatory.", repair_key="media")
                if not is_nonempty_str(m.get("attribution")):
                    self.report.warn("B5_NO_ATTRIBUTION", f"{path}.attribution",
                                     "attribution is required by most CC licences.",
                                     repair_key="media")

            # Fuzzy title dedup (B1).
            root = title_root(str(m.get("title", "")))
            if root and len(root) > 6:
                if root in seen_titles:
                    self.report.warn("B1_FUZZY_TITLE_DUP", f"{path}.title",
                                     f"Title root matches media[{seen_titles[root]}].",
                                     repair_key="media")
                seen_titles[root] = i

            # Language filter (B2 / 5.3).
            blob = " ".join(str(m.get(k, "")) for k in
                            ("title", "caption", "alt", "proposedUrl", "attribution"))
            fm = FOREIGN_TOKEN_RE.search(blob)
            if fm:
                self.report.error("B2_FOREIGN_TOKEN", path,
                                  f"Foreign-language token '{fm.group(1)}' in metadata; "
                                  f"asset must be discarded.", repair_key="media")
            if NON_LATIN_RE.search(blob):
                self.report.error("B2_NON_LATIN_SCRIPT", path,
                                  "Non-Latin script detected in media metadata.",
                                  repair_key="media")

        # One best asset per section (B1).
        for sk, c in section_counts.items():
            if c > 1:
                self.report.warn("B1_MULTIPLE_PER_SECTION", f"media[sectionKey={sk}]",
                                 f"{c} assets for section '{sk}'; the prompt asks for "
                                 f"one best-in-class asset per section.",
                                 repair_key="media")

    def check_padding_heuristics(self) -> None:
        """The n-gram repetition scan: is the model padding to hit the floor?"""
        mgmt_text = " ".join(
            p.get("text", "")
            for b in (self.topic.get("managementSections") or [])
            if isinstance(b, dict)
            for p in (b.get("content") or [])
            if isinstance(p, dict)
        )
        ratio = ngram_repeat_ratio(mgmt_text)
        self.report.metrics["ngramRepeatRatioManagement"] = round(ratio, 4)
        if ratio > NGRAM_REPEAT_MAX:
            self.report.error(
                "F7_PADDING", "topic.managementSections",
                f"{ratio:.0%} of {NGRAM_SIZE}-grams in management text are repeated "
                f"(cap {NGRAM_REPEAT_MAX:.0%}). This is padding, not content.",
                repair_key="managementSections")
        elif ratio > NGRAM_REPEAT_MAX * 0.7:
            self.report.warn("F7_PADDING_NEAR", "topic.managementSections",
                             f"Repetition ratio {ratio:.0%} approaching the "
                             f"{NGRAM_REPEAT_MAX:.0%} padding cap.",
                             repair_key="managementSections")

        # Near-duplicate ContentPoints across the whole document.
        seen: Dict[str, str] = {}
        for b_i, block in enumerate(self.topic.get("managementSections") or []):
            if not isinstance(block, dict):
                continue
            for p_i, p in enumerate(block.get("content") or []):
                if not isinstance(p, dict):
                    continue
                toks = normalise_tokens(p.get("text", ""))
                if len(toks) < 12:
                    continue
                key = " ".join(toks[:12])
                path = f"topic.managementSections[{b_i}].content[{p_i}]"
                if key in seen:
                    self.report.error("F7_DUPLICATE_POINT", path,
                                      f"Opens identically to {seen[key]}.",
                                      repair_key="managementSections")
                seen[key] = path

    def check_self_audit(self) -> None:
        """V16: the honesty check. Recompute the model's own numbers."""
        if not self.audit:
            self.report.error("V16_NO_SELFAUDIT", "_selfAudit",
                              "Mandatory _selfAudit object is absent.",
                              repair_key="_selfAudit")
            return

        counts = self.audit.get("counts", {}) or {}
        recomputed = {
            "managementBlocks": self.report.metrics.get("managementBlocks", 0),
            "managementPointsTotal": self.report.metrics.get("managementPointsTotal", 0),
            "drugRegimens": self.report.metrics.get("drugRegimens", 0),
            "preciseDosing": self.report.metrics.get("preciseDosing", 0),
            "prognosisQuantitative": self.report.metrics.get("prognosisQuantitative", 0),
            "differentialDiagnosis": self.report.metrics.get("differentialDiagnosis", 0),
            "drugInteractionFlags": self.report.metrics.get("drugInteractionFlags", 0),
            "references": self.report.metrics.get("referencesDeclared", 0),
            "mediaItems": self.report.metrics.get("mediaItems", 0),
        }
        mismatches = []
        for k, actual in recomputed.items():
            claimed = counts.get(k)
            if isinstance(claimed, int) and claimed != actual:
                mismatches.append(f"{k}: claimed {claimed}, actual {actual}")
        if mismatches:
            self.report.error("V16_AUDIT_MISMATCH", "_selfAudit.counts",
                              f"Self-audit counts do not match the document: "
                              f"{'; '.join(mismatches[:6])}.",
                              repair_key="_selfAudit")

        # F10: the dishonesty check. Claiming success while errors exist is fatal.
        claimed_pass = self.audit.get("floorsMet")
        real_errors = self.report.count(Severity.ERROR)
        if claimed_pass is True and real_errors > 0:
            self.report.error(
                "F10_FALSE_AUDIT", "_selfAudit.floorsMet",
                f"_selfAudit claims floorsMet=true but {real_errors} ERROR finding(s) "
                f"exist. A dishonest audit voids the whole output.",
                repair_key="_selfAudit")
        if claimed_pass is False:
            declared = self.audit.get("failedFloors") or []
            self.report.info("V16_HONEST_FAILURE", "_selfAudit",
                             f"Model honestly declared failure on: "
                             f"{', '.join(map(str, declared[:8])) or '(unspecified)'}.")
        if not is_nonempty_str(self.audit.get("confidenceStatement")):
            self.report.warn("V16_NO_CONFIDENCE", "_selfAudit.confidenceStatement",
                             "Missing the one-sentence honest weakness statement.",
                             repair_key="_selfAudit")


# ══════════════════════════════════════════════════════════════════════════
# SECTION 5 — NETWORK CHECKS (opt-in)
# ══════════════════════════════════════════════════════════════════════════

class NetworkChecker:
    """Resolves DOIs on CrossRef and HEAD-checks media URLs.

    Both are opt-in because they are slow and need internet. Results are
    cached on disk so a repair loop does not re-query the same DOI 50 times.
    """

    def __init__(self, mailto: Optional[str] = None, cache_path: str = ".v9_net_cache.json",
                 timeout: int = 15, workers: int = 8) -> None:
        self.mailto = mailto
        self.cache_path = cache_path
        self.timeout = timeout
        self.workers = workers
        self.cache: Dict[str, Any] = self._load_cache()

    def _load_cache(self) -> Dict[str, Any]:
        try:
            with open(self.cache_path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            return {}

    def save_cache(self) -> None:
        try:
            with open(self.cache_path, "w", encoding="utf-8") as fh:
                json.dump(self.cache, fh)
        except Exception:
            pass

    def _ua(self) -> str:
        base = "DoctorsHeroRX-Validator/9.0 (https://doctorshero.com)"
        return f"{base} mailto:{self.mailto}" if self.mailto else base

    # -------------------------------------------------------------- DOIs ---
    def resolve_doi(self, doi: str) -> Tuple[bool, str]:
        """Ask CrossRef whether this DOI exists. Returns (ok, note)."""
        key = f"doi::{doi.lower()}"
        if key in self.cache:
            c = self.cache[key]
            return c["ok"], c["note"]
        url = f"https://api.crossref.org/works/{doi}"
        req = urlrequest.Request(url, headers={"User-Agent": self._ua()})
        try:
            with urlrequest.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
            msg = data.get("message", {})
            title = (msg.get("title") or ["(untitled)"])[0]
            year = None
            for f in ("published-print", "published-online", "issued"):
                parts = (msg.get(f) or {}).get("date-parts") or []
                if parts and parts[0]:
                    year = parts[0][0]
                    break
            note = f"{truncate(title, 70)} ({year})"
            ok = True
        except urlerror.HTTPError as e:
            ok, note = False, f"HTTP {e.code} - DOI not found on CrossRef"
        except Exception as e:
            ok, note = False, f"lookup failed: {type(e).__name__}"
        self.cache[key] = {"ok": ok, "note": note}
        time.sleep(0.05)   # be polite to the public API
        return ok, note

    def check_dois(self, doc: Dict[str, Any], report: Report) -> None:
        refs = (doc.get("topic") or {}).get("references", []) or []
        targets: List[Tuple[str, str]] = []
        for i, r in enumerate(refs):
            url = (r or {}).get("url") if isinstance(r, dict) else None
            if isinstance(url, str):
                m = DOI_EXTRACT_RE.search(url)
                if m:
                    targets.append((f"topic.references[{i}].url", m.group(0)))
        if not targets:
            report.info("NET_NO_DOIS", "topic.references", "No DOIs to resolve.")
            return
        bad = 0
        for path, doi in targets:
            ok, note = self.resolve_doi(doi)
            if not ok:
                bad += 1
                report.error("F1_DOI_UNRESOLVED", path,
                             f"DOI {doi} did not resolve on CrossRef ({note}). "
                             f"Likely fabricated.", repair_key="references")
        report.metrics["doisChecked"] = len(targets)
        report.metrics["doisUnresolved"] = bad
        self.save_cache()

    # ------------------------------------------------------------- media ---
    def head(self, url: str) -> Tuple[bool, str]:
        key = f"head::{url}"
        if key in self.cache:
            c = self.cache[key]
            return c["ok"], c["note"]
        try:
            req = urlrequest.Request(url, method="HEAD",
                                     headers={"User-Agent": self._ua()})
            with urlrequest.urlopen(req, timeout=self.timeout) as resp:
                status = resp.status
                ctype = resp.headers.get("Content-Type", "")
            ok = status == 200
            note = f"HTTP {status}, {ctype}"
            if ok and "image" not in ctype and "video" not in ctype \
                    and "html" not in ctype:
                ok, note = False, f"unexpected Content-Type: {ctype}"
        except urlerror.HTTPError as e:
            ok, note = False, f"HTTP {e.code}"
        except Exception as e:
            ok, note = False, f"{type(e).__name__}"
        self.cache[key] = {"ok": ok, "note": note}
        return ok, note

    def check_media(self, doc: Dict[str, Any], report: Report) -> None:
        media = doc.get("media", []) or []
        targets = [(f"media[{i}].proposedUrl", m["proposedUrl"])
                   for i, m in enumerate(media)
                   if isinstance(m, dict) and isinstance(m.get("proposedUrl"), str)
                   and m["proposedUrl"].startswith("http")]
        if not targets:
            report.info("NET_NO_MEDIA", "media", "No media URLs to check.")
            return
        bad = 0
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            results = list(pool.map(lambda t: (t[0], t[1], *self.head(t[1])), targets))
        for path, url, ok, note in results:
            if not ok:
                bad += 1
                report.error("F6_MEDIA_UNREACHABLE", path,
                             f"URL did not return a valid asset ({note}): "
                             f"{truncate(url, 80)}", repair_key="media")
        report.metrics["mediaUrlsChecked"] = len(targets)
        report.metrics["mediaUrlsUnreachable"] = bad
        self.save_cache()


# ══════════════════════════════════════════════════════════════════════════
# SECTION 6 — MULTI-PASS MERGE (PART 7 of the prompt)
# ══════════════════════════════════════════════════════════════════════════

def deep_merge(base: Dict[str, Any], incoming: Dict[str, Any],
               report: Optional[Report] = None, path: str = "") -> Dict[str, Any]:
    """Merge pass N into the accumulating document.

    Rule: dicts merge key-by-key, lists concatenate, scalars must not clash.
    A clash means the model restated content it was told not to restate.
    """
    for k, v in incoming.items():
        p = f"{path}.{k}" if path else k
        if k not in base:
            base[k] = v
        elif isinstance(base[k], dict) and isinstance(v, dict):
            deep_merge(base[k], v, report, p)
        elif isinstance(base[k], list) and isinstance(v, list):
            base[k].extend(v)
        elif base[k] != v:
            if report:
                report.warn("MERGE_CONFLICT", p,
                            "Two passes emitted different values for the same scalar "
                            "key; keeping the earlier one.")
    return base


def merge_passes(paths: Sequence[str], report: Report) -> Dict[str, Any]:
    merged: Dict[str, Any] = {}
    for p in paths:
        with open(p, "r", encoding="utf-8") as fh:
            obj = json.load(fh)
        meta = obj.pop("_merge", None)
        if meta:
            report.info("MERGE_PASS", p,
                        f"pass {meta.get('passIndex')}/{meta.get('passTotal')}, "
                        f"keys: {', '.join(meta.get('keysEmitted', [])[:8])}")
            if meta.get("truncatedAt"):
                report.warn("MERGE_TRUNCATED", p,
                            f"Pass reported truncation at {meta['truncatedAt']}; "
                            f"regenerate this pass.")
        # A pass may emit topic-level keys either bare or nested under "topic".
        if "topic" not in obj and "media" not in obj:
            obj = {"topic": obj}
        deep_merge(merged, obj, report)
    return merged


# ══════════════════════════════════════════════════════════════════════════
# SECTION 7 — AUTOFIX + REPORTING + CLI
# ══════════════════════════════════════════════════════════════════════════

def autofix_ascii(node: Any) -> Any:
    """Replace smart punctuation with ASCII equivalents, recursively."""
    if isinstance(node, str):
        return BAD_PUNCT_RE.sub(lambda m: BAD_PUNCT_MAP[m.group(0)], node)
    if isinstance(node, dict):
        return {k: autofix_ascii(v) for k, v in node.items()}
    if isinstance(node, list):
        return [autofix_ascii(v) for v in node]
    return node


COLORS = {"ERROR": "\033[91m", "WARN": "\033[93m", "INFO": "\033[96m",
          "OK": "\033[92m", "DIM": "\033[2m", "END": "\033[0m"}


def c(text: str, key: str, enabled: bool) -> str:
    return f"{COLORS[key]}{text}{COLORS['END']}" if enabled else text


def print_report(report: Report, color: bool, verbose: bool) -> None:
    order = {Severity.ERROR: 0, Severity.WARN: 1, Severity.INFO: 2}
    findings = sorted(report.findings, key=lambda f: (order[f.severity], f.code, f.path))

    print("\n" + "=" * 78)
    print(" DoctorsHero RX - Clinical Knowledge Hub - v9 Validation Report")
    print("=" * 78)

    print("\n-- METRICS " + "-" * 66)
    for k, v in sorted(report.metrics.items()):
        if isinstance(v, list) and len(v) > 12:
            v = f"{v[:12]} ...(+{len(v)-12})"
        print(f"  {k:<42} {v}")

    for sev in (Severity.ERROR, Severity.WARN, Severity.INFO):
        group = [f for f in findings if f.severity == sev]
        if not group or (sev == Severity.INFO and not verbose):
            continue
        print(f"\n-- {sev.value}S ({len(group)}) " + "-" * (66 - len(sev.value)))
        for f in group:
            print(f"  {c(f.severity.value, f.severity.value, color)} "
                  f"[{f.code}] {f.path}")
            print(f"        {f.message}")
            if f.detail and verbose:
                print(f"        {c('> ' + truncate(f.detail, 150), 'DIM', color)}")

    n_err, n_warn = report.count(Severity.ERROR), report.count(Severity.WARN)
    print("\n" + "=" * 78)
    verdict = c("PASS", "OK", color) if n_err == 0 else c("FAIL", "ERROR", color)
    print(f" VERDICT: {verdict}   errors={n_err}  warnings={n_warn}")
    print("=" * 78 + "\n")


def emit_repair_prompts(report: Report) -> None:
    """Group failures by top-level key and print a targeted re-prompt.

    This is the money-saving step: instead of regenerating a 100k-character
    topic, you regenerate only the two keys that failed.
    """
    by_key: Dict[str, List[Finding]] = defaultdict(list)
    for f in report.findings:
        if f.severity == Severity.ERROR and f.repair_key:
            by_key[f.repair_key].append(f)
    if not by_key:
        return
    print("-- TARGETED REPAIR PROMPTS " + "-" * 50 + "\n")
    for key, fs in sorted(by_key.items(), key=lambda kv: -len(kv[1])):
        reasons = sorted({f"{f.code}: {f.message}" for f in fs})[:6]
        print(f"### Regenerate `{key}` ({len(fs)} error(s))\n")
        print("Regenerate ONLY the key `%s`. Emit a JSON object containing that key "
              "and nothing else. Reuse the existing refId numbering exactly; do not "
              "introduce new references. It failed validation for these reasons:" % key)
        for r in reasons:
            print(f"  - {r}")
        print()


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Validate a DoctorsHero RX clinical topic against content standard v9.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("input", nargs="?", help="Path to the topic JSON file.")
    p.add_argument("--merge", nargs="+", metavar="PASS",
                   help="Merge multi-pass JSON files, then validate the result.")
    p.add_argument("-o", "--output", help="Write the merged/autofixed JSON here.")
    p.add_argument("--extensions", choices=["on", "off"], default="on",
                   help="Enforce v9 additive fields (default: on).")
    p.add_argument("--reference-mode", choices=["R-STRICT", "R-SEARCH", "R-DRAFT"],
                   default="R-STRICT")
    p.add_argument("--media-mode", choices=["VERIFIED", "QUERY-ONLY"],
                   default="QUERY-ONLY")
    p.add_argument("--min-chars", type=int, default=DEFAULT_FLOORS["min_json_chars"])
    p.add_argument("--check-dois", action="store_true",
                   help="Resolve every DOI against the CrossRef API (network).")
    p.add_argument("--check-media", action="store_true",
                   help="HEAD-check every media URL (network).")
    p.add_argument("--mailto", help="Contact email for the CrossRef polite pool.")
    p.add_argument("--autofix-ascii", action="store_true",
                   help="Rewrite smart punctuation to ASCII and save with -o.")
    p.add_argument("--json-report", help="Write the full report as JSON here.")
    p.add_argument("--emit-repair-prompts", action="store_true",
                   help="Print targeted re-prompts for each failing key.")
    p.add_argument("--strict", action="store_true",
                   help="Treat warnings as failures.")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--no-color", action="store_true")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    color = not args.no_color and sys.stdout.isatty()
    report = Report()

    # ---- load or merge ----------------------------------------------------
    try:
        if args.merge:
            doc = merge_passes(args.merge, report)
            raw = json.dumps(doc, ensure_ascii=False)
        elif args.input:
            with open(args.input, "r", encoding="utf-8") as fh:
                raw = fh.read()
            stripped = raw.strip()
            if stripped.startswith("```"):
                report.error("OUTPUT_FENCED", "$",
                             "File starts with a markdown code fence. The model must "
                             "emit raw JSON.")
                stripped = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", stripped)
            doc = json.loads(stripped)
        else:
            print("error: provide an input file or --merge", file=sys.stderr)
            return 2
    except json.JSONDecodeError as e:
        print(f"FATAL: JSON did not parse - {e}\n"
              f"       Truncated output is the classic single-pass failure; "
              f"switch to GENERATION_MODE=MULTI-PASS.", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"FATAL: {e}", file=sys.stderr)
        return 2

    if not isinstance(doc, dict):
        print("FATAL: top-level JSON must be an object.", file=sys.stderr)
        return 1

    # ---- optional autofix -------------------------------------------------
    if args.autofix_ascii:
        doc = autofix_ascii(doc)
        report.info("AUTOFIX", "$", "Non-ASCII punctuation normalised to ASCII.")

    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=2)
        report.info("WROTE", args.output, f"Wrote {args.output}")

    # ---- validate ---------------------------------------------------------
    floors = dict(DEFAULT_FLOORS)
    floors["min_json_chars"] = args.min_chars

    validator = TopicValidator(
        doc=doc, raw_text=raw, floors=floors,
        extensions_on=(args.extensions == "on"),
        reference_mode=args.reference_mode,
        media_mode=args.media_mode,
    )
    result = validator.run()
    result.findings = report.findings + result.findings   # keep load-time findings
    result.metrics.update(report.metrics)

    # ---- network ----------------------------------------------------------
    if args.check_dois or args.check_media:
        net = NetworkChecker(mailto=args.mailto)
        if args.check_dois:
            net.check_dois(doc, result)
        if args.check_media:
            net.check_media(doc, result)

    # ---- output -----------------------------------------------------------
    print_report(result, color, args.verbose)
    if args.emit_repair_prompts:
        emit_repair_prompts(result)

    if args.json_report:
        payload = {
            "contentStandard": CONTENT_STANDARD,
            "input": args.input or args.merge,
            "passed": result.passed,
            "errorCount": result.count(Severity.ERROR),
            "warningCount": result.count(Severity.WARN),
            "metrics": result.metrics,
            "findings": [f.to_dict() for f in result.findings],
        }
        with open(args.json_report, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        print(f"JSON report written to {args.json_report}\n")

    if result.count(Severity.ERROR) > 0:
        return 1
    if args.strict and result.count(Severity.WARN) > 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
