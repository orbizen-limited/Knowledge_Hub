// Shared normalize+upsert path for topic ingestion. Used by both the CLI
// (scripts/ingest.ts) and the internal HTTP write endpoints so a topic JSON
// file is persisted identically no matter how it arrives.

import { pool } from '../db/pool.js';
import { normalizeTopic } from './normalizeTopic.js';

export const REVIEW_STATUSES = [
  'pending_clinician_check',
  'pending_board_review',
  'approved',
  'rejected',
] as const;

export type ReviewStatus = (typeof REVIEW_STATUSES)[number];

export function isReviewStatus(value: unknown): value is ReviewStatus {
  return typeof value === 'string' && (REVIEW_STATUSES as readonly string[]).includes(value);
}

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
    updated_at = now()
  RETURNING (xmax = 0) AS was_inserted;
`;

export interface UpsertTopicOptions {
  sourceFile?: string;
  /** Overrides the review status carried in the topic JSON when provided. */
  reviewStatus?: ReviewStatus;
}

export interface UpsertTopicResult {
  topicId: string;
  action: 'inserted' | 'updated';
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export async function upsertTopic(
  json: Record<string, any>,
  opts: UpsertTopicOptions = {},
): Promise<UpsertTopicResult> {
  const topic = normalizeTopic(json);

  if (!topic.topicId) {
    throw new Error('missing topicId');
  }
  if (opts.reviewStatus) {
    topic.reviewStatus = opts.reviewStatus;
  }

  const { rows } = await pool.query<{ was_inserted: boolean }>(UPSERT_SQL, [
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
    opts.sourceFile ?? '',
  ]);

  return {
    topicId: topic.topicId,
    action: rows[0]?.was_inserted ? 'inserted' : 'updated',
  };
}
