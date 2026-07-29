// Seeds catalog stub topics (content_version=0) from a JSON file of shape
// { stubs: [{topicId, title, specialty, chapter, tier?}] } (a top-level
// `count` field is tolerated and ignored). Existing topic_ids are skipped.
//
// Usage: npm run ingest:catalog -- path/to/catalog.json

import { readFileSync } from 'node:fs';
import { pool } from '../src/db/pool.js';
import { insertCatalogStubs, type CatalogStub } from '../src/services/topicRepository.js';
import { logger } from '../src/utils/logger.js';

async function ingestCatalog() {
  const filePath = process.argv[2];
  if (!filePath) {
    throw new Error('Usage: npm run ingest:catalog -- <path-to-catalog-json>');
  }

  const raw = readFileSync(filePath, 'utf-8');
  const json = JSON.parse(raw);
  const stubs: CatalogStub[] = Array.isArray(json?.stubs) ? json.stubs : [];
  if (!stubs.length) {
    throw new Error(`No stubs found in ${filePath} (expected {stubs: [...]})`);
  }

  const invalid = stubs.filter(
    (s) => typeof s?.topicId !== 'string' || !s.topicId.trim()
      || typeof s?.title !== 'string' || !s.title.trim(),
  );
  if (invalid.length) {
    throw new Error(`${invalid.length} stub(s) missing topicId or title`);
  }

  logger.info(`Found ${stubs.length} catalog stubs in ${filePath}`);
  const { inserted, skipped } = await insertCatalogStubs(stubs);
  logger.info({ inserted, skipped, total: stubs.length }, 'Catalog ingestion complete');

  await pool.end();
}

ingestCatalog().catch((err) => {
  logger.error({ err }, 'Catalog ingestion failed');
  process.exit(1);
});
