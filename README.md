# DoctorsHero Knowledge Hub

Standalone local microservice serving the DoctorsHero Knowledge Hub clinical
content pack (898 evidence-based topics, sourced from doctorshero-rx's offline
`assets/knowledge_hub/enriched/`) over a GraphQL API, plus a browsing UI.

- `server/` — Node/Express/TypeScript + GraphQL API, backed by Postgres. See
  [`API.md`](./API.md) for the schema and how to call it (e.g. from the
  Laravel `doctorshero-backend`).
- `web/` — Next.js UI for browsing/searching topics locally, styled after the
  Flutter app's Knowledge Hub theme.

## Prerequisites

- Node.js >= 20
- A local PostgreSQL instance with a dedicated role + database already
  created (see below)

## First-time setup

```powershell
# 1. Create a Postgres role + database for this service (adjust password)
psql -U postgres -c "CREATE ROLE knowledge_hub_app LOGIN PASSWORD 'change-me';"
psql -U postgres -c "CREATE DATABASE knowledge_hub OWNER knowledge_hub_app;"

# 2. Configure the server
cd server
copy .env.example .env
# edit .env: set DATABASE_URL, API_KEY, INGEST_SOURCE_DIR (path to the
# doctorshero-rx checkout's assets/knowledge_hub/enriched folder)
npm install
npm run migrate     # creates the topics table + indexes
npm run ingest       # loads the JSON content pack into Postgres

# 3. Configure the web UI
cd ../web
copy .env.local.example .env.local
# edit .env.local: KH_API_KEY must match server/.env's API_KEY
npm install
```

## Running locally

```powershell
# Terminal 1
cd server
npm run dev      # http://127.0.0.1:4400/graphql

# Terminal 2
cd web
npm run dev -- -p 3400   # http://localhost:3400
```

Re-run `npm run ingest` in `server/` whenever the doctorshero-rx content pack
is updated — it's an upsert keyed by `topicId`, safe to run repeatedly.

## Production builds

```powershell
cd server && npm run build && npm start
cd web && npm run build && npm start
```

## Security notes

- `.env` / `.env.local` are gitignored — never commit real secrets. Only the
  `.env*.example` templates are tracked.
- The Postgres role used by this service should be scoped to only the
  `knowledge_hub` database, not a superuser.
- The web UI's browser-facing code never sees the API key — see
  [`API.md`](./API.md#authentication) and `web/app/api/proxy/route.ts`.
