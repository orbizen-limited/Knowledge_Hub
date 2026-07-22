# Knowledge Hub API

GraphQL API in front of the DoctorsHero Knowledge Hub content (898 evidence-based
clinical topics ingested from doctorshero-rx's offline content pack). Intended for
consumption by other DoctorsHero services — primarily `doctorshero-backend` — over
the local network / loopback.

- **Base URL (local dev):** `http://127.0.0.1:4400`
- **GraphQL endpoint:** `POST /graphql`
- **Health check:** `GET /health` (unauthenticated, for uptime probes)

## Authentication

Every request to `/graphql` must include the shared secret as a header:

```
X-Api-Key: <value of API_KEY from server/.env>
```

Requests without a valid key get `401 Unauthorized`. The key is compared with a
constant-time check (`node:crypto.timingSafeEqual`) to avoid timing side-channels.
There is no per-caller key — this is one shared secret between the Knowledge Hub
service and each trusted internal consumer (the local Next.js UI, the Laravel
backend). Rotate it by changing `API_KEY` in `server/.env` and restarting the
service; update every consumer at the same time.

## Rate limiting & query cost guards

- `express-rate-limit`: `RATE_LIMIT_MAX` requests (default 200) per
  `RATE_LIMIT_WINDOW_MS` (default 60s) per source IP, on `/graphql` only.
- `graphql-depth-limit`: rejects queries nested deeper than `GRAPHQL_MAX_DEPTH`
  (default 10).
- A custom field-count rule rejects queries selecting more than
  `GRAPHQL_MAX_COMPLEXITY` (default 2000) total fields.
- `express.json({ limit: '1mb' })` caps request body size.

All four are configurable via `server/.env`.

## Calling it from PHP / Laravel (Guzzle)

```php
$response = Http::withHeaders([
    'X-Api-Key' => config('services.knowledge_hub.api_key'),
])->post('http://127.0.0.1:4400/graphql', [
    'query' => 'query($id: ID!) { topic(topicId: $id) { title bottomLine } }',
    'variables' => ['id' => 'cardiology.advanced_cardiovascular_life_support_acls'],
]);

$topic = $response->json('data.topic');
```

Standard GraphQL-over-HTTP contract: `POST` a JSON body `{ "query": "...",
"variables": { ... } }`, get back `{ "data": {...} }` or `{ "errors": [...] }`
(HTTP 200 even on GraphQL-level errors — check the `errors` key). Malformed
requests / auth failures return non-200 status codes with a plain
`{ "error": "..." }` body.

## Schema

### `Topic`

The full shape of one clinical topic. Every list field defaults to `[]`, never
`null`, if the source content pack didn't populate that section.

| Field | Type | Notes |
|---|---|---|
| `topicId` | `ID!` | Stable slug, e.g. `cardiology.advanced_cardiovascular_life_support_acls` |
| `title` | `String!` | |
| `specialty` | `String!` | e.g. `Cardiology` |
| `chapter` | `String!` | Broader grouping, e.g. `Internal Medicine` (may be empty) |
| `tier` | `String!` | `tier1` \| `tier2` \| `tier3` — evidence tier |
| `contentVersion` | `Int!` | `0` = catalog stub (no real content yet) |
| `lastUpdated` | `String` | ISO 8601, nullable |
| `reviewStatus` | `String!` | `pending_clinician_check` \| `pending_board_review` \| `approved` \| `rejected` |
| `reviewedBy`, `reviewedAt`, `reviewLog` | | Clinician sign-off audit trail |
| `bottomLine` | `String!` | One-line clinical summary |
| `agentGenerated` | `Boolean!` | True if LLM-authored pending review, false for curated content |
| `keywords`, `careSettings` (`outpatient`\|`inpatient`\|`critical`) | `[String!]!` | |
| `summaryParagraphs` | `[String!]!` | |
| `recommendations` | `[Recommendation!]!` | `{ text, grade, source, sourceUrl, evidenceLevel }` |
| `references` | `[Reference!]!` | `{ citation, url, organization, year, doi }` |
| `etiologyEpidemiology`, `clinicalPresentation`, `diagnosticWorkup`, `monitoringFollowUp`, `complicationsPrognosis`, `pathophysiology`, `relapseRemission`, `patientEducation`, `crossReferences`, `relatedMedicineGenericKeys`, `curriculumRefs` | `[String!]!` | Plain bullet lists |
| `differentialDiagnosis` | `[DifferentialDiagnosisEntry!]!` | `{ condition, distinguishingFeature }` |
| `treatmentLines` | `[TreatmentLine!]!` | `{ line, description, medicineGenericKeys }` |
| `specialPopulations` | `[SpecialPopulationNote!]!` | `{ population, guidance }` |
| `comorbidityManagement`, `complicationManagement` | `[KeyedDetail!]!` | `{ heading, detail }` |
| `drugRegimens` | `[DrugRegimen!]!` | `{ drug, indication, initialDose, titration, maintenanceDose, termination, alternatives, adverseEffectManagement, monitoring, genericKeys }` |
| `backgroundInformation`, `diagnosisSections`, `managementSections`, `complicationSections` | `[ContentBlock!]!` | `{ heading, points: [{ text, level }] }` — nested outline sections, `level` is indent depth |
| `prognosisQuantitative` | `[PrognosisQuantitativeEntry!]!` | `{ outcome, estimate, source, doi }` |
| `preciseDosing` | `[PreciseDosingEntry!]!` | `{ drug, indication, standardDose, doseReductionCriteria, renalAdjustment, hepaticAdjustment, administration, onsetOffset }` |

### Queries

```graphql
type Query {
  topic(topicId: ID!): Topic
  topics(
    specialty: String
    chapter: String
    tier: String
    careSetting: String
    limit: Int = 20   # capped at 100
    offset: Int = 0
  ): TopicConnection!
  search(query: String!, limit: Int = 20): [SearchResult!]!   # limit capped at 50
  chapters: [ChapterSummary!]!
  health: HealthStatus!
}

type TopicConnection { totalCount: Int!, items: [Topic!]! }
type SearchResult { topic: Topic!, score: Float! }
type ChapterSummary { chapter: String!, totalCount: Int!, specialties: [SpecialtyCount!]! }
type SpecialtyCount { specialty: String!, count: Int! }
type HealthStatus { status: String!, topicCount: Int! }
```

- `topics` returns `null` items are never present; `topic` itself returns `null`
  if the `topicId` doesn't exist (not an error).
- `search` uses Postgres `websearch_to_tsquery` (handles quoted phrases, `AND`,
  `OR`, `-exclude` the way a search-engine query box would), ranked by
  `ts_rank` over title (highest weight) → bottom line/keywords → summary.
- `chapters` is the source for a browse tree (chapter → specialty → count);
  cheap to call often, it's cached in-process.

## Example requests

```bash
API_KEY="<value from server/.env>"

# Fetch one topic
curl -s -X POST http://127.0.0.1:4400/graphql \
  -H "Content-Type: application/json" -H "X-Api-Key: $API_KEY" \
  -d '{"query":"query($id: ID!) { topic(topicId: $id) { title bottomLine drugRegimens { drug initialDose } } }","variables":{"id":"cardiology.advanced_cardiovascular_life_support_acls"}}'

# Search
curl -s -X POST http://127.0.0.1:4400/graphql \
  -H "Content-Type: application/json" -H "X-Api-Key: $API_KEY" \
  -d '{"query":"{ search(query: \"hyperkalemia\", limit: 5) { score topic { topicId title specialty } } }"}'

# Browse tree
curl -s -X POST http://127.0.0.1:4400/graphql \
  -H "Content-Type: application/json" -H "X-Api-Key: $API_KEY" \
  -d '{"query":"{ chapters { chapter totalCount specialties { specialty count } } }"}'

# Uptime check (no auth needed)
curl -s http://127.0.0.1:4400/health
```

## Caching

Read queries are cached in-process (`lru-cache`, default 500 entries / 5 minute
TTL, both tunable via `CACHE_MAX_ENTRIES` / `CACHE_TTL_MS`). There is no
explicit cache-invalidation endpoint — after re-running `npm run ingest`,
either wait out the TTL or restart the service to guarantee fresh reads.

## Errors

GraphQL execution errors come back as `{ "errors": [{ "message": "..." }] }`
with HTTP 200, per the GraphQL-over-HTTP convention — always check for the
`errors` key rather than relying on status code alone. In production
(`NODE_ENV=production`), error messages are generic and stack traces are
stripped; full detail is only in the server's own logs.

| HTTP status | Meaning |
|---|---|
| 200 | Request handled (check `errors` key for GraphQL-level failures) |
| 401 | Missing/invalid `X-Api-Key` |
| 413 | Request body too large |
| 429 | Rate limit exceeded |
| 500 | Unhandled server error |
