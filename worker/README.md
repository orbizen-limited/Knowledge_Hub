# DoctorsHero Knowledge Hub — Enrichment Worker

Server-side **v4 enrichment** worker for the DoctorsHero RX Clinical Knowledge
Hub. Laravel dispatches a job; this worker researches references (Crossref),
generates a Tier‑1 topic section‑by‑section with Gemini, assembles a v4 topic
JSON, gates it with the **bundled validator**, and POSTs the result back to
Laravel via a **signed callback**.

Loopback‑only FastAPI service. Binds `127.0.0.1:4410`. Never exposed via Apache.

## Layout

```
worker/
├── app/
│   ├── __init__.py
│   ├── main.py          # FastAPI app: GET /health, POST /v1/enrich (JWT+HMAC gated)
│   ├── security.py      # Python mirror of App\Support\ServiceSignature (JWT+HMAC)
│   └── pipeline.py      # research → generate → assemble → validate → callback
├── vendor/
│   └── validate_topic.py  # copied verbatim from doctorshero-rx (the v4 gate)
├── tests/
│   ├── test_validator_gate.py
│   └── fixtures/
│       └── dermatology-atopic-dermatitis.json  # known-good sample topic
├── supervisor/
│   └── kh-worker.conf   # example supervisor program
├── requirements.txt
├── .env.example
└── README.md
```

## Setup

```bash
cd worker
python -m venv venv
source venv/bin/activate            # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                # fill in secrets + GEMINI_API_KEY
```

## Environment

| Var | Purpose | Default |
|-----|---------|---------|
| `KH_WORKER_PORT` | uvicorn bind port | `4410` |
| `KH_WORKER_MAX_CONCURRENT` | max concurrent jobs (extras queue in‑process) | `1` |
| `KH_WORKER_JWT_SECRET` | HS256 JWT shared secret (**match backend**) | — |
| `KH_WORKER_HMAC_SECRET` | HMAC‑SHA256 shared secret (**match backend**) | — |
| `KH_WORKER_JWT_TTL` | JWT time‑to‑live, seconds | `60` |
| `KH_WORKER_CLOCK_SKEW` | allowed clock skew, seconds | `30` |
| `GEMINI_API_KEY` | Gemini API key | — |
| `KH_WORKER_GEMINI_MODEL` | Gemini model id | `gemini-2.5-flash` |
| `KH_WORKER_CROSSREF_MAILTO` | polite‑pool contact in User‑Agent | `dev@doctorshero.com` |
| `KH_WORKER_MAX_REPAIR_PASSES` | validator repair loops before failing | `3` |

## Run

```bash
uvicorn app.main:app --host 127.0.0.1 --port 4410
# health check
curl http://127.0.0.1:4410/health   # {"status":"ok","jobs_active":0}
```

Production: use `supervisor/kh-worker.conf` (loopback uvicorn, autorestart).

## Auth contract (mirrors `App\Support\ServiceSignature`)

Both directions are signed with a short‑lived **JWT (HS256)** bearer token plus
an **HMAC‑SHA256** signature over the raw body. Exact HTTP headers:

| Header | Meaning |
|--------|---------|
| `Authorization: Bearer <jwt>` | HS256 JWT (`iss`, `aud`, `iat`, `nbf`, `exp`, `jti`) |
| `X-Timestamp` | unix seconds, used in the HMAC canonical string |
| `X-Signature` | hex HMAC‑SHA256 of the canonical string |

HMAC canonical string (identical to `ServiceSignature::hmac`):

```
<timestamp>\n<UPPERCASE-METHOD>\n<path>\n<sha256-hex(body)>
```

`path` carries a leading slash (e.g. `/v1/enrich`).

| Direction | issuer (`iss`) | audience (`aud`) |
|-----------|----------------|------------------|
| Inbound `POST /v1/enrich` (backend → worker) | `doctorshero-backend` | `kh-worker` |
| Outbound callbacks (worker → backend) | `kh-worker` | `doctorshero-backend` |

Verification order (inbound, mirrors `VerifyInternalRequest`): JWT
signature/exp/nbf/iss/aud/jti + replay guard → HMAC freshness + signature.

## HTTP contract

### `POST /v1/enrich`  (signed)

Request body:

```json
{
  "job_id": "uuid",
  "topic_id": "dermatology.atopic_dermatitis",
  "title": "Atopic Dermatitis",
  "specialty": "General Dermatology",
  "chapter": "Dermatology",
  "callback_url": "https://api.doctorshero.com/api/internal/kh/enrich/callback"
}
```

Immediate response `202`:

```json
{ "accepted": true, "job_id": "uuid" }
```

The pipeline then runs on a background thread and reports via signed callbacks.

### Callbacks (signed POST to `callback_url`)

**Progress** — emitted at each stage:

```json
{ "job_id": "uuid", "topic_id": "...", "status": "progress",
  "stage": "research|generate:core|assemble|validate|repair:1", "progress": 0 }
```

**Completed**:

```json
{ "job_id": "uuid", "topic_id": "...", "status": "completed", "progress": 100,
  "topic": { /* full v4 topic JSON */ },
  "validator_report": { "passed": true, "errors": 0, "warnings": 0, "error_list": [], "warning_list": [], "raw": "..." } }
```

**Failed**:

```json
{ "job_id": "uuid", "topic_id": "...", "status": "failed",
  "error": "N validator error(s) after 3 repair pass(es)",
  "validator_report": { /* parsed report or null */ } }
```

## Validation gate

`pipeline.run_validator` writes the assembled topic to a temp file and runs
`python vendor/validate_topic.py <file>` as a subprocess (the bundled validator
has **no** `--json` flag — it prints a text report and exits `0` on pass, `1` on
errors). Its stdout is parsed into `validator_report`. On errors, the pipeline
feeds the specific errors back to Gemini and regenerates, up to
`KH_WORKER_MAX_REPAIR_PASSES` times, before reporting `failed`.

## Test

```bash
pytest tests/test_validator_gate.py     # or: python tests/test_validator_gate.py
```

Runs the bundled validator against the sample enriched dermatology topic and
asserts **0 errors / exit 0**, proving the gate wiring works. The validator uses
only the Python standard library, so this test runs without third‑party deps.
