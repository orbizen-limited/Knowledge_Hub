"""Unit tests for v9 → legacy DoctorsHero field mapping."""

from __future__ import annotations

from app.v9_to_legacy import map_v9_document_for_ingest, map_v9_topic_to_legacy


def test_maps_core_prose_fields():
    flat = map_v9_topic_to_legacy(
        {
            "summary": "ACS is acute coronary plaque rupture.\n\nImmediate ECG and aspirin matter.",
            "etiology": "Atherosclerotic plaque rupture causes most STEMIs.",
            "presentation": "Crushing central chest pain with radiation.",
            "workup": "ECG within 10 minutes of arrival.",
            "monitoring": "Troponin at 0 and 3 hours.",
            "pathophysiology": "Thrombosis on ruptured plaque.",
            "managementSections": [
                {
                    "heading": "First-line",
                    "content": [
                        {"text": "Give aspirin 300 mg chewed within 10 min [1].", "level": 0}
                    ],
                }
            ],
            "diagnosisSections": [
                {"heading": "ECG", "content": [{"text": "ST elevation >= 1 mm [2].", "level": 0}]}
            ],
            "patientEducationBundle": {
                "plainLanguageSummary": "Call emergency services for chest pain.",
                "whenToSeekHelp": ["Pain lasting more than 5 minutes"],
            },
        }
    )
    assert flat["summaryParagraphs"][0].startswith("ACS is acute")
    assert "etiologyEpidemiology" in flat and "plaque" in flat["etiologyEpidemiology"][0]
    assert "clinicalPresentation" in flat
    assert "diagnosticWorkup" in flat
    assert flat["monitoringFollowUp"][0].startswith("Troponin")
    assert isinstance(flat["pathophysiology"], list)
    assert flat["managementSections"][0]["points"][0]["text"].startswith("Give aspirin")
    assert flat["diagnosisSections"][0]["points"]
    assert any("emergency" in p.lower() for p in flat["patientEducation"])
    assert flat["bottomLine"]


def test_envelope_flatten_and_preserves_existing_legacy():
    doc = {
        "topic": {
            "topicMetadata": {"topicId": "t1", "topicName": "ACS", "specialty": "Cardiology"},
            "summary": "One paragraph only.",
            "etiologyEpidemiology": ["Already mapped etiology stays."],
            "etiology": "Should not overwrite existing list.",
        },
        "media": [{"id": "m1", "proposedUrl": None}],
    }
    flat = map_v9_document_for_ingest(doc)
    assert flat["topicId"] == "t1"
    assert flat["title"] == "ACS"
    assert flat["etiologyEpidemiology"] == ["Already mapped etiology stays."]
    assert flat["media"][0]["id"] == "m1"
    assert flat["summaryParagraphs"] == ["One paragraph only."]
