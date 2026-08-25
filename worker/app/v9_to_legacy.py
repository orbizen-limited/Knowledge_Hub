"""Map v9 authoring fields onto DoctorsHero legacy topic keys the app already reads.

v9 emits: summary, etiology, presentation, workup, monitoring, content[] on blocks.
Legacy/RX expects: summaryParagraphs, etiologyEpidemiology, clinicalPresentation,
diagnosticWorkup, monitoringFollowUp, points[] on ContentBlocks.

Called after v9 validate/repair, before the worker callback / KH ingest.
Keeps original v9 keys too so validators and audits stay intact.
"""

from __future__ import annotations

from typing import Any


def _as_paragraphs(value: Any) -> list[str]:
    """Coerce a v9 prose string / list / nested content into paragraph strings."""
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        parts = [p.strip() for p in text.replace("\r\n", "\n").split("\n\n") if p.strip()]
        return parts if parts else [text]
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            if isinstance(item, str) and item.strip():
                out.append(item.strip())
            elif isinstance(item, dict):
                t = item.get("text") or item.get("point") or item.get("detail") or ""
                if isinstance(t, str) and t.strip():
                    out.append(t.strip())
                else:
                    out.extend(_as_paragraphs(item.get("content") or item.get("points")))
        return out
    if isinstance(value, dict):
        # Prefer explicit prose keys, else flatten content/points
        for key in ("text", "summary", "detail", "prose"):
            if isinstance(value.get(key), str) and value[key].strip():
                return _as_paragraphs(value[key])
        return _as_paragraphs(value.get("content") or value.get("points") or value.get("paragraphs"))
    return [str(value).strip()] if str(value).strip() else []


def _set_if_empty(target: dict, key: str, paragraphs: list[str]) -> None:
    existing = target.get(key)
    if isinstance(existing, list) and any(str(x).strip() for x in existing):
        return
    if isinstance(existing, str) and existing.strip():
        return
    if paragraphs:
        target[key] = paragraphs


def _content_to_points(block: dict) -> dict:
    """Ensure ContentBlock has legacy `points` (app/server parse points, not content)."""
    out = dict(block)
    heading = out.get("heading") or out.get("title") or ""
    out["heading"] = str(heading)

    raw = out.get("points")
    if raw is None:
        raw = out.get("content") or out.get("bullets") or out.get("items") or []
    if not isinstance(raw, list):
        raw = [raw] if raw else []

    points: list[dict] = []
    for item in raw:
        if isinstance(item, str):
            t = item.strip()
            if t:
                points.append({"text": t, "level": 0})
        elif isinstance(item, dict):
            text = item.get("text") or item.get("point") or item.get("detail") or ""
            if not isinstance(text, str):
                text = str(text) if text is not None else ""
            text = text.strip()
            if not text:
                continue
            try:
                level = int(item.get("level", 0))
            except (TypeError, ValueError):
                level = 0
            points.append({"text": text, "level": level})
    out["points"] = points
    # Keep content in sync for any v9-aware consumer
    out["content"] = list(points)
    return out


def _map_section_blocks(blocks: Any) -> list[dict]:
    if not isinstance(blocks, list):
        return []
    return [_content_to_points(b) for b in blocks if isinstance(b, dict)]


def _patient_education_from_bundle(bundle: Any) -> list[str]:
    if not isinstance(bundle, dict):
        return _as_paragraphs(bundle)
    out: list[str] = []
    for key in ("plainLanguageSummary", "banglaSummary", "localeAdaptationNotes"):
        out.extend(_as_paragraphs(bundle.get(key)))
    for key in ("sickDayRules", "whenToSeekHelp", "adherenceStrategies"):
        out.extend(_as_paragraphs(bundle.get(key)))
    faq = bundle.get("faq")
    if isinstance(faq, list):
        for item in faq:
            if not isinstance(item, dict):
                continue
            q = str(item.get("question") or "").strip()
            a = str(item.get("answer") or "").strip()
            if q and a:
                out.append(f"Q: {q} A: {a}")
            elif a:
                out.append(a)
            elif q:
                out.append(q)
    return [x for x in out if x]


def _bottom_line_from_summary(summary_paras: list[str]) -> str:
    if not summary_paras:
        return ""
    first = summary_paras[0].strip()
    # Cap to a short clinical bottom line for list cards
    if len(first) <= 400:
        return first
    cut = first[:400]
    # Prefer sentence boundary
    for sep in (". ", "; ", " — ", " - "):
        idx = cut.rfind(sep)
        if idx >= 120:
            return cut[: idx + 1].strip()
    return cut.rstrip() + "…"


def _complications_prognosis(topic: dict) -> list[str]:
    """Synthesize legacy complicationsPrognosis prose from v9 structures if empty."""
    existing = _as_paragraphs(topic.get("complicationsPrognosis"))
    if existing:
        return existing
    out: list[str] = []
    for block in topic.get("complicationSections") or []:
        if isinstance(block, dict):
            heading = str(block.get("heading") or "").strip()
            pts = block.get("points") or block.get("content") or []
            texts = _as_paragraphs(pts)
            if heading and texts:
                out.append(f"{heading}: {texts[0]}")
            else:
                out.extend(texts)
    for row in topic.get("complicationManagement") or []:
        if not isinstance(row, dict):
            continue
        heading = str(row.get("heading") or row.get("complication") or "").strip()
        detail = str(row.get("detail") or row.get("management") or "").strip()
        if heading and detail:
            out.append(f"{heading} — {detail}")
        elif detail:
            out.append(detail)
    for row in topic.get("prognosisQuantitative") or []:
        if not isinstance(row, dict):
            continue
        outcome = str(row.get("outcome") or "").strip()
        estimate = str(row.get("estimate") or "").strip()
        if outcome and estimate:
            out.append(f"{outcome}: {estimate}")
        elif estimate:
            out.append(estimate)
    return out


def _special_populations(topic: dict) -> list[dict]:
    existing = topic.get("specialPopulations")
    if isinstance(existing, list) and existing:
        return existing
    # Pull from management block headings that mention populations, or localeContext
    locale = topic.get("localeContext")
    out: list[dict] = []
    if isinstance(locale, dict):
        for key, label in (
            ("availabilityNotes", "Local availability (BD)"),
            ("costNotes", "Cost considerations (BD)"),
            ("emlStatus", "EML / formulary"),
            ("referralReality", "Referral pathways (BD)"),
        ):
            val = locale.get(key)
            paras = _as_paragraphs(val)
            if paras:
                out.append({"population": label, "considerations": paras[0]})
    return out


def map_v9_topic_to_legacy(topic: dict) -> dict:
    """Mutate a flat topic dict so DoctorsHero RX / GraphQL see legacy field names."""
    if not isinstance(topic, dict):
        return topic
    out = dict(topic)

    # --- prose fields (v9 string → legacy string[]) ---
    _set_if_empty(out, "summaryParagraphs", _as_paragraphs(out.get("summaryParagraphs") or out.get("summary")))
    _set_if_empty(
        out,
        "etiologyEpidemiology",
        _as_paragraphs(out.get("etiologyEpidemiology") or out.get("etiology")),
    )
    _set_if_empty(
        out,
        "clinicalPresentation",
        _as_paragraphs(out.get("clinicalPresentation") or out.get("presentation")),
    )
    _set_if_empty(
        out,
        "diagnosticWorkup",
        _as_paragraphs(out.get("diagnosticWorkup") or out.get("workup")),
    )
    _set_if_empty(
        out,
        "monitoringFollowUp",
        _as_paragraphs(out.get("monitoringFollowUp") or out.get("monitoring")),
    )
    # pathophysiology: same key, but v9 is often a single string
    patho = _as_paragraphs(out.get("pathophysiology"))
    if patho:
        out["pathophysiology"] = patho

    # --- ContentBlocks: content[] → points[] ---
    if "managementSections" in out:
        out["managementSections"] = _map_section_blocks(out.get("managementSections"))
    if "diagnosisSections" in out:
        out["diagnosisSections"] = _map_section_blocks(out.get("diagnosisSections"))
    if "complicationSections" in out:
        out["complicationSections"] = _map_section_blocks(out.get("complicationSections"))
    if "backgroundInformation" in out:
        out["backgroundInformation"] = _map_section_blocks(out.get("backgroundInformation"))

    # --- patient education ---
    pe = _as_paragraphs(out.get("patientEducation"))
    if not pe:
        pe = _patient_education_from_bundle(out.get("patientEducationBundle"))
    if pe:
        out["patientEducation"] = pe

    # --- bottom line for catalog cards ---
    if not str(out.get("bottomLine") or "").strip():
        bl = _bottom_line_from_summary(out.get("summaryParagraphs") or [])
        if bl:
            out["bottomLine"] = bl

    # --- complications / prognosis prose ---
    cp = _complications_prognosis(out)
    if cp:
        out["complicationsPrognosis"] = cp

    # --- special populations fallback from locale ---
    sp = _special_populations(out)
    if sp:
        out["specialPopulations"] = sp

    # --- redFlags → recommendations-ish optional: leave as-is under redFlags ---
    # App may not render redFlags yet; fold high-signal into bottomLine only if empty (already handled).

    # Ensure contentStandard stamped
    out.setdefault("contentStandard", "v9")

    # Never invent treatmentLines for v9
    out.pop("treatmentLines", None)

    return out


def map_v9_document_for_ingest(doc_or_flat: dict) -> dict:
    """Accept either {topic, media} envelope or flat topic; return flat legacy-shaped topic."""
    if not isinstance(doc_or_flat, dict):
        return doc_or_flat

    if isinstance(doc_or_flat.get("topic"), dict):
        flat = dict(doc_or_flat["topic"])
        if isinstance(doc_or_flat.get("media"), list):
            flat["media"] = doc_or_flat["media"]
        if doc_or_flat.get("_selfAudit") is not None:
            flat["_selfAudit"] = doc_or_flat["_selfAudit"]
    else:
        flat = dict(doc_or_flat)

    meta = flat.get("topicMetadata")
    if isinstance(meta, dict):
        flat.setdefault("topicId", meta.get("topicId"))
        flat.setdefault("title", meta.get("topicName") or meta.get("title"))
        flat.setdefault("specialty", meta.get("specialty"))
        flat.setdefault("contentStandard", meta.get("contentStandard") or "v9")

    return map_v9_topic_to_legacy(flat)
