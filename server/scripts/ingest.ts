import { readFileSync, readdirSync } from 'node:fs';
import path from 'node:path';
import { pool } from '../src/db/pool.js';
import { env } from '../src/config/env.js';
import { upsertTopic } from '../src/services/ingestTopic.js';
import { logger } from '../src/utils/logger.js';

async function ingest() {
  if (!env.ingestSourceDir) {
    throw new Error('INGEST_SOURCE_DIR is not set in .env');
  }

  const files = readdirSync(env.ingestSourceDir).filter((f) => f.endsWith('.json'));
  logger.info(`Found ${files.length} JSON files in ${env.ingestSourceDir}`);

  let inserted = 0;
  let skipped = 0;
  const malformed: string[] = [];

  for (const file of files) {
    const fullPath = path.join(env.ingestSourceDir, file);
    try {
      const raw = readFileSync(fullPath, 'utf-8');
      const json = JSON.parse(raw);
      await upsertTopic(json, { sourceFile: file });
      inserted += 1;
    } catch (err) {
      skipped += 1;
      malformed.push(`${file}: ${(err as Error).message}`);
    }
  }

  logger.info({ inserted, skipped, total: files.length }, 'Ingestion complete');
  if (malformed.length) {
    logger.warn({ malformed }, `${malformed.length} file(s) skipped or malformed`);
  }

  await pool.end();
}

ingest().catch((err) => {
  logger.error({ err }, 'Ingestion failed');
  process.exit(1);
});
