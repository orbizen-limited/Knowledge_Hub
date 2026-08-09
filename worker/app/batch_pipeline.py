"""
batch_pipeline.py — Bulk Batch-API enrichment for many KH topics.

Flow:
  1. Research references per topic (sync, free Crossref)
  2. Pack section-group prompts → submit Gemini or Qwen Batch (~50% off)
  3. Poll until complete (up to ~24h)
  4. Assemble + validate; repair passes use live LLM (small share of cost)
  5. Callback each child job as completed/failed (Laravel rolls up batch)

Topic count is whatever Laravel sent (dynamic).
"""

from __future__ import annotations

import sys
import traceback

from . import gemini_batch, llm, openai_compat_batch, pipeline

BATCH_PROVIDERS = frozenset({"gemini", "qwen"})


def run_batch_pipeline(job: dict, on_done=None) -> None:
    batch_id = str(job.get("batch_id") or "")
    callback_url = job["callback_url"]
    topics = job.get("topics") or []
    poll_interval = int(job.get("poll_interval_sec") or 60)
    poll_timeout = int(job.get("poll_timeout_sec") or 86400)

    # Use first topic as the carrier for batch-level status hints
    carrier = topics[0] if topics else None
    carrier_job_id = str((carrier or {}).get("job_id") or "0")
    carrier_topic_id = str((carrier or {}).get("topic_id") or "batch")

    def hint(status: str, external: str | None = None, error: str | None = None) -> None:
        if not carrier:
            return
        payload = {
            "job_id": carrier_job_id,
            "topic_id": carrier_topic_id,
            "status": "progress",
            "stage": f"batch:{status}",
            "progress": 5 if status == "submitted" else 40 if status == "polling" else 70,
            "batch_id": batch_id,
            "batch_status": status,
        }
        if external:
            payload["external_batch_id"] = external
        if error:
            payload["batch_error"] = error
        pipeline._post_signed(callback_url, payload)

    try:
        cfg = llm.LlmConfig.from_job(job)
    except Exception as exc:
        _fail_all(topics, callback_url, batch_id, f"invalid LLM config: {exc}")
        if on_done:
            on_done(batch_id)
        return

    if cfg.provider not in BATCH_PROVIDERS:
        _fail_all(
            topics,
            callback_url,
            batch_id,
            f"Batch mode implemented for gemini/qwen only (got {cfg.provider})",
        )
        if on_done:
            on_done(batch_id)
        return

    # Per-topic usage trackers (repairs + estimated batch share)
    trackers: dict[str, pipeline.UsageTracker] = {
        str(t["job_id"]): pipeline.UsageTracker(cfg) for t in topics
    }

    prepared: list[dict] = []
    request_pairs: list[tuple[str, str]] = []

    try:
        for t in topics:
            job_id = str(t["job_id"])
            topic_id = str(t["topic_id"])
            title = t.get("title") or topic_id
            specialty = t.get("specialty") or ""
            chapter = t.get("chapter") or specialty

            pipeline._progress(callback_url, job_id, topic_id, "research", 5)
            try:
                refs = pipeline.research_references(title, specialty, chapter)
            except Exception as exc:
                pipeline._post_signed(
                    callback_url,
                    {
                        "job_id": job_id,
                        "topic_id": topic_id,
                        "status": "failed",
                        "error": f"research failed: {exc}",
                        "batch_id": batch_id,
                        "usage": trackers[job_id].as_dict(),
                    },
                )
                continue

            if len(refs) < 10:
                pipeline._post_signed(
                    callback_url,
                    {
                        "job_id": job_id,
                        "topic_id": topic_id,
                        "status": "failed",
                        "error": f"insufficient verified references ({len(refs)} found, need >=10)",
                        "batch_id": batch_id,
                        "usage": trackers[job_id].as_dict(),
                    },
                )
                continue

            pipeline._progress(callback_url, job_id, topic_id, "research:done", 20)
            for group in pipeline.GROUPS:
                key = f"{job_id}::{group['name']}"
                prompt = pipeline._build_prompt(group, title, specialty, chapter, refs)
                request_pairs.append((key, prompt))

            prepared.append({
                "job_id": job_id,
                "topic_id": topic_id,
                "title": title,
                "specialty": specialty,
                "chapter": chapter,
                "references": refs,
            })

        if not prepared or not request_pairs:
            hint("failed", error="No topics passed research gate")
            if on_done:
                on_done(batch_id)
            return

        # Submit provider Batch
        display = f"kh-batch-{batch_id}"
        if cfg.provider == "gemini":
            names = gemini_batch.submit_batch(request_pairs, cfg, display_name=display)
            external = ",".join(names)
            hint("submitted", external=external)
            hint("polling", external=external)

            def on_tick(name, state, _batch):
                print(f"[batch] {name} state={state}", file=sys.stderr)

            texts = gemini_batch.poll_batches(
                names,
                cfg,
                poll_interval_sec=poll_interval,
                poll_timeout_sec=poll_timeout,
                on_tick=on_tick,
            )
        else:
            # Qwen (OpenAI-compatible Batch API)
            batch_ext_id = openai_compat_batch.submit_batch(
                request_pairs, cfg, display_name=display,
            )
            external = batch_ext_id
            hint("submitted", external=external)
            hint("polling", external=external)

            def on_tick(name, state, _batch):
                print(f"[batch] {name} state={state}", file=sys.stderr)

            texts = openai_compat_batch.poll_batches(
                [batch_ext_id],
                cfg,
                poll_interval_sec=poll_interval,
                poll_timeout_sec=poll_timeout,
                on_tick=on_tick,
            )
        hint("assembling", external=external)

        # Assemble each topic from group texts
        pipeline._CURRENT_LLM = cfg
        pipeline._CURRENT_FALLBACK = llm.LlmConfig.fallback_from_job(job)
        for item in prepared:
            job_id = item["job_id"]
            topic_id = item["topic_id"]
            tracker = trackers[job_id]
            pipeline._CURRENT_USAGE = tracker

            try:
                groups: dict = {}
                missing = []
                for group in pipeline.GROUPS:
                    key = f"{job_id}::{group['name']}"
                    raw = texts.get(key) or ""
                    # Batch refusal → live regenerate that group (fallback if configured)
                    if not raw.strip() or llm.looks_like_refusal(raw):
                        if llm.looks_like_refusal(raw) and raw.strip():
                            tracker.mark_primary_refused()
                        prompt = pipeline._build_prompt(
                            group,
                            item["title"],
                            item["specialty"],
                            item["chapter"],
                            item["references"],
                        )
                        raw = llm.generate_json_with_fallback(
                            prompt,
                            cfg,
                            pipeline._CURRENT_FALLBACK,
                            tracker,
                            retries=3,
                        )
                        if not raw.strip() or llm.looks_like_refusal(raw):
                            missing.append(group["name"])
                            continue
                    else:
                        tracker.add({
                            "promptTokenCount": 0,
                            "candidatesTokenCount": max(1, len(raw) // 4),
                        })
                    try:
                        groups.update(pipeline._extract_json(raw))
                    except Exception:
                        missing.append(group["name"])

                if missing:
                    raise RuntimeError(f"missing batch group responses: {', '.join(missing)}")

                pipeline._progress(callback_url, job_id, topic_id, "assemble", 72)
                topic = pipeline.assemble_topic(
                    topic_id, item["title"], item["specialty"], item["chapter"],
                    item["references"], groups,
                )

                pipeline._progress(callback_url, job_id, topic_id, "validate", 80)
                report = pipeline.run_validator(topic)

                passes = 0
                while not report["passed"] and passes < pipeline.MAX_REPAIR_PASSES:
                    passes += 1
                    pipeline._progress(
                        callback_url, job_id, topic_id, f"repair:{passes}", 82 + passes * 4,
                    )
                    groups = pipeline._repair(
                        item["title"], item["specialty"], item["chapter"],
                        item["references"], report,
                        callback_url, job_id, topic_id, pass_num=passes,
                    )
                    topic = pipeline.assemble_topic(
                        topic_id, item["title"], item["specialty"], item["chapter"],
                        item["references"], groups,
                    )
                    report = pipeline.run_validator(topic)

                bypassed = False
                if not report["passed"]:
                    if pipeline.VALIDATOR_BYPASS_AFTER_REPAIRS and passes >= pipeline.MAX_REPAIR_PASSES:
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
                    else:
                        err_lines = report.get("error_list") or []
                        summary = (
                            f"{report.get('errors', 0)} validator error(s) after {passes} "
                            f"repair pass(es)"
                        )
                        if err_lines:
                            summary += ": " + "; ".join(err_lines[:8])
                        pipeline._post_signed(
                            callback_url,
                            {
                                "job_id": job_id,
                                "topic_id": topic_id,
                                "status": "failed",
                                "error": summary[:4000],
                                "validator_report": report,
                                "batch_id": batch_id,
                                "usage": tracker.as_dict(),
                            },
                        )
                        continue

                # Apply batch discount factor to reported cost (generation was batch;
                # repairs are live — still approximate at 0.5× overall for admin UX).
                usage = tracker.as_dict()
                if usage.get("cost_usd") is not None:
                    usage = {
                        **usage,
                        "cost_usd": round(float(usage["cost_usd"]) * 0.5, 6),
                        "batch_discount_applied": 0.5,
                    }

                pipeline._post_signed(
                    callback_url,
                    {
                        "job_id": job_id,
                        "topic_id": topic_id,
                        "status": "completed",
                        "progress": 100,
                        "stage": "done_validator_bypassed" if bypassed else "done",
                        "topic": topic,
                        "validator_report": report,
                        "batch_id": batch_id,
                        "usage": usage,
                    },
                )
            except Exception as exc:
                print(f"[batch] topic {topic_id} failed: {exc}", file=sys.stderr)
                traceback.print_exc()
                pipeline._post_signed(
                    callback_url,
                    {
                        "job_id": job_id,
                        "topic_id": topic_id,
                        "status": "failed",
                        "error": f"batch assemble failed: {exc}",
                        "batch_id": batch_id,
                        "usage": tracker.as_dict(),
                    },
                )
    except Exception as exc:
        print(f"[batch] batch {batch_id} crashed: {exc}", file=sys.stderr)
        traceback.print_exc()
        hint("failed", error=str(exc))
        _fail_remaining(prepared, callback_url, batch_id, f"batch failed: {exc}", trackers)
    finally:
        pipeline._CURRENT_USAGE = None
        pipeline._CURRENT_LLM = None
        pipeline._CURRENT_FALLBACK = None
        if on_done:
            on_done(batch_id)


def _fail_all(topics, callback_url, batch_id, error: str) -> None:
    for t in topics:
        pipeline._post_signed(
            callback_url,
            {
                "job_id": str(t["job_id"]),
                "topic_id": str(t["topic_id"]),
                "status": "failed",
                "error": error,
                "batch_id": batch_id,
            },
        )


def _fail_remaining(prepared, callback_url, batch_id, error, trackers) -> None:
    # Only fail topics that never got a terminal callback — best effort: fail all prepared
    for item in prepared:
        job_id = item["job_id"]
        pipeline._post_signed(
            callback_url,
            {
                "job_id": job_id,
                "topic_id": item["topic_id"],
                "status": "failed",
                "error": error,
                "batch_id": batch_id,
                "usage": trackers.get(job_id, pipeline.UsageTracker()).as_dict(),
            },
        )
