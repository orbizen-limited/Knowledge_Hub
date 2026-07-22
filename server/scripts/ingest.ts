import { readFileSync, readdirSync } from 'node:fs';
import path from 'node:path';
import { pool } from '../src/db/pool.js';
import { env } from '../src/config/env.js';
import { normalizeTopic } from '../src/services/normalizeTopic.js';
import { logger } from '../src/utils/logger.js';

const UPSERT_SQL = `
  INSERT INTO topics (
    topic_id, title, specialty, chapter, tier, content_version, last_updated,
    review_status, reviewed_by, reviewed_at, bottom_line, agent_generated,
    keywords, care_settings, summary_paragraphs, content, source_file, updated_at
  ) VALUES (
    $1, $2, $3, $4, $5, $6, $7,
    $8, $9, $10, $11, $12,
    $13, $14, $15, $16, $17, now()
  )
  ON CONFLICT (topic_id) DO UPDATE SET
    title = EXCLUDED.title,
    specialty = EXCLUDED.specialty,
    chapter = EXCLUDED.chapter,
    tier = EXCLUDED.tier,
    content_version = EXCLUDED.content_version,
    last_updated = EXCLUDED.last_updated,
    review_status = EXCLUDED.review_status,
    reviewed_by = EXCLUDED.reviewed_by,
    reviewed_at = EXCLUDED.reviewed_at,
    bottom_line = EXCLUDED.bottom_line,
    agent_generated = EXCLUDED.agent_generated,
    keywords = EXCLUDED.keywords,
    care_settings = EXCLUDED.care_settings,
    summary_paragraphs = EXCLUDED.summary_paragraphs,
    content = EXCLUDED.content,
    source_file = EXCLUDED.source_file,
    updated_at = now();
`;

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
      const topic = normalizeTopic(json);

      if (!topic.topicId) {
        skipped += 1;
        malformed.push(`${file}: missing topicId`);
        continue;
      }

      await pool.query(UPSERT_SQL, [
        topic.topicId,
        topic.title,
        topic.specialty,
        topic.chapter,
        topic.tier,
        topic.contentVersion,
        topic.lastUpdated,
        topic.reviewStatus,
        topic.reviewedBy,
        topic.reviewedAt,
        topic.bottomLine,
        topic.agentGenerated,
        topic.keywords,
        topic.careSettings,
        topic.summaryParagraphs,
        JSON.stringify(topic.content),
        file,
      ]);
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
