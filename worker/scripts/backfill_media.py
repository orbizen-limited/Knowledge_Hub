#!/usr/bin/env python3
"""Attach Commons media to existing v6 topics without re-running text enrich."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv()

from app import media  # noqa: E402

TOPICS = [
    (
        "dermatology.acne_vulgaris_topical_and_systemic",
        "Acne Vulgaris — Topical & Systemic",
    ),
    (
        "drug_reference.ace_inhibitors_and_arbs",
        "ACE Inhibitors & ARBs",
    ),
]

CALLBACK = os.environ.get(
    "KH_CALLBACK_URL",
    "https://api.doctorshero.com/api/internal/knowledge-hub/callback",
)


def main() -> int:
    job = {
        "llm": {
            "content_standard": "v6",
            "media_max_per_topic": 8,
            "media_max_per_section": 3,
            "media_max_bytes": 2097152,
            "media_host_allowlist": "upload.wikimedia.org,commons.wikimedia.org",
        }
    }
    results: dict[str, list] = {}
    job_ids = {
        "dermatology.acne_vulgaris_topical_and_systemic": "93",
        "drug_reference.ace_inhibitors_and_arbs": "92",
    }
    for topic_id, title in TOPICS:
        topic = {"topicId": topic_id, "title": title, "contentStandard": "v6"}
        jid = job_ids.get(topic_id, "93")
        print(f"[backfill] start {topic_id} job={jid}", flush=True)
        out = media.attach_media(topic, job, CALLBACK, jid, topic_id)
        items = out.get("media") or []
        results[topic_id] = items
        print(f"[backfill] {topic_id}: {len(items)} item(s)", flush=True)
        for m in items:
            print(
                f"  - {m.get('kind')} {m.get('sectionKey')} {(m.get('url') or '')[:100]}",
                flush=True,
            )
    Path("/tmp/kh_media_backfill.json").write_text(
        json.dumps(results, ensure_ascii=False), encoding="utf-8"
    )
    sql_lines = []
    for topic_id, items in results.items():
        payload = json.dumps(items, ensure_ascii=False).replace("'", "''")
        sql_lines.append(
            "UPDATE topics SET content = jsonb_set(COALESCE(content, '{}'::jsonb), "
            f"'{{media}}', '{payload}'::jsonb), updated_at = now() "
            f"WHERE topic_id = '{topic_id}';"
        )
    Path("/tmp/kh_media_update.sql").write_text("\n".join(sql_lines) + "\n", encoding="utf-8")
    print("[backfill] wrote /tmp/kh_media_backfill.json and /tmp/kh_media_update.sql", flush=True)
    return 0 if any(results.values()) else 2


if __name__ == "__main__":
    raise SystemExit(main())
