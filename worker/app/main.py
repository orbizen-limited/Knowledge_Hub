"""
main.py — FastAPI app for the Knowledge Hub enrichment worker.

Endpoints:
  GET  /health       → {status:'ok', jobs_active:N}   (unsigned liveness probe)
  POST /v1/enrich    → verifies JWT+HMAC (else 401); accepts the job, returns
                       202 {accepted:true, job_id} immediately, and runs the
                       pipeline on a background thread pool.

Auth mirrors app/Http/Middleware/VerifyInternalRequest.php: the inbound request
from the Laravel backend is signed iss=doctorshero-backend, aud=kh-worker with a
JWT bearer token + HMAC over the raw body (see app/security.py).

Binds 127.0.0.1:4410 (loopback only) — never exposed via Apache.
"""

from __future__ import annotations

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

load_dotenv()

from . import batch_pipeline, pipeline, security  # noqa: E402  (env must load first)

MAX_CONCURRENT = int(os.environ.get("KH_WORKER_MAX_CONCURRENT", "1") or "1")

app = FastAPI(title="DoctorsHero Knowledge Hub Enrichment Worker", version="1.1")

_executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT)
_active_lock = threading.Lock()
_active_jobs: set[str] = set()


def _job_started(job_id: str) -> None:
    with _active_lock:
        _active_jobs.add(job_id)


def _job_finished(job_id: str) -> None:
    with _active_lock:
        _active_jobs.discard(job_id)


@app.get("/health")
def health() -> dict:
    with _active_lock:
        n = len(_active_jobs)
    return {"status": "ok", "jobs_active": n}


@app.get("/v1/cost-estimate")
def cost_estimate() -> dict:
    """Unsigned pre-flight estimate for admin UI (loopback-only service)."""
    return {"success": True, "data": pipeline.estimate_enrichment_cost()}


@app.post("/v1/enrich")
async def enrich(request: Request):
    raw = await request.body()
    body = raw.decode("utf-8")
    # path with a leading slash — matches VerifyInternalRequest's '/'.ltrim(path)
    path = request.url.path

    try:
        security.verify_inbound(
            method=request.method,
            path=path,
            body=body,
            headers=dict(request.headers),
        )
    except ValueError as exc:
        return JSONResponse(
            {"success": False, "message": "Unauthorized", "reason": str(exc)},
            status_code=401,
        )

    try:
        payload = json.loads(body)
    except (ValueError, TypeError):
        return JSONResponse({"accepted": False, "message": "invalid JSON body"}, status_code=422)

    required = ["job_id", "topic_id", "title", "specialty", "chapter", "callback_url"]
    missing = [k for k in required if not payload.get(k)]
    if missing:
        return JSONResponse(
            {"accepted": False, "message": f"missing fields: {', '.join(missing)}"},
            status_code=422,
        )

    job_id = payload["job_id"]
    _job_started(job_id)
    _executor.submit(pipeline.run_pipeline, payload, _job_finished)

    return JSONResponse({"accepted": True, "job_id": job_id}, status_code=202)


@app.post("/v1/enrich-batch")
async def enrich_batch(request: Request):
    """Bulk Batch-API enrichment. Topic count is dynamic (topics[] length)."""
    raw = await request.body()
    body = raw.decode("utf-8")
    path = request.url.path

    try:
        security.verify_inbound(
            method=request.method,
            path=path,
            body=body,
            headers=dict(request.headers),
        )
    except ValueError as exc:
        return JSONResponse(
            {"success": False, "message": "Unauthorized", "reason": str(exc)},
            status_code=401,
        )

    try:
        payload = json.loads(body)
    except (ValueError, TypeError):
        return JSONResponse({"accepted": False, "message": "invalid JSON body"}, status_code=422)

    if not payload.get("batch_id") or not payload.get("callback_url"):
        return JSONResponse(
            {"accepted": False, "message": "missing batch_id or callback_url"},
            status_code=422,
        )
    topics = payload.get("topics") or []
    if not isinstance(topics, list) or not topics:
        return JSONResponse(
            {"accepted": False, "message": "topics[] required (non-empty)"},
            status_code=422,
        )
    for i, t in enumerate(topics):
        if not isinstance(t, dict) or not t.get("job_id") or not t.get("topic_id"):
            return JSONResponse(
                {"accepted": False, "message": f"topics[{i}] needs job_id and topic_id"},
                status_code=422,
            )

    batch_id = str(payload["batch_id"])
    _job_started(f"batch:{batch_id}")
    _executor.submit(
        batch_pipeline.run_batch_pipeline,
        payload,
        lambda _bid: _job_finished(f"batch:{batch_id}"),
    )

    return JSONResponse(
        {
            "accepted": True,
            "batch_id": batch_id,
            "topic_count": len(topics),
            "external_batch_id": None,  # filled after provider submit via callback
        },
        status_code=202,
    )
