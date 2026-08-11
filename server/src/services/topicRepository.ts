import { pool } from '../db/pool.js';

export interface TopicRow {
  topic_id: string;
  title: string;
  specialty: string;
  chapter: string;
  tier: string;
  content_version: number;
  last_updated: Date | null;
  review_status: string;
  reviewed_by: string;
  reviewed_at: Date | null;
  bottom_line: string;
  agent_generated: boolean;
  keywords: string[];
  care_settings: string[];
  summary_paragraphs: string[];
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  content: Record<string, any>;
  source_file: string;
  created_at: Date;
  updated_at: Date;
}

const CONTENT_LIST_FIELDS = [
  'recommendations',
  'references',
  'etiologyEpidemiology',
  'clinicalPresentation',
  'differentialDiagnosis',
  'diagnosticWorkup',
  'treatmentLines',
  'specialPopulations',
  'monitoringFollowUp',
  'complicationsPrognosis',
  'pathophysiology',
  'comorbidityManagement',
  'complicationManagement',
  'drugRegimens',
  'relapseRemission',
  'patientEducation',
  'crossReferences',
  'backgroundInformation',
  'diagnosisSections',
  'managementSections',
  'complicationSections',
  'relatedMedicineGenericKeys',
  'curriculumRefs',
  'prognosisQuantitative',
  'preciseDosing',
  'reviewLog',
  'drugInteractionFlags',
  'media',
] as const;

// v5 scalar metadata carried inside the content jsonb (absent on legacy rows).
const CONTENT_SCALAR_FIELDS = [
  'contentStandard',
  'referenceStyle',
  'canonicalTopicId',
] as const;

export function rowToTopic(row: TopicRow) {
  const content = row.content ?? {};
  const base: Record<string, unknown> = {
    topicId: row.topic_id,
    title: row.title,
    specialty: row.specialty,
    chapter: row.chapter,
    tier: row.tier,
    contentVersion: row.content_version,
    lastUpdated: row.last_updated ? row.last_updated.toISOString() : null,
    reviewStatus: row.review_status,
    reviewedBy: row.reviewed_by,
    reviewedAt: row.reviewed_at ? row.reviewed_at.toISOString() : null,
    bottomLine: row.bottom_line,
    agentGenerated: row.agent_generated,
    keywords: row.keywords ?? [],
    careSettings: row.care_settings ?? [],
    summaryParagraphs: row.summary_paragraphs ?? [],
  };
  for (const field of CONTENT_LIST_FIELDS) {
    base[field] = content[field] ?? [];
  }
  for (const field of CONTENT_SCALAR_FIELDS) {
    base[field] = content[field] ?? '';
  }
  // Stored as a {facetTopicId: sectionHeading} map; GraphQL has no map type,
  // so expose it as a list of pairs.
  const anchors = content.facetAnchors;
  base.facetAnchors =
    anchors && typeof anchors === 'object' && !Array.isArray(anchors)
      ? Object.entries(anchors).map(([facetTopicId, sectionHeading]) => ({
          facetTopicId,
          sectionHeading: String(sectionHeading ?? ''),
        }))
      : [];
  return base;
}

export async function findTopicById(topicId: string) {
  const { rows } = await pool.query<TopicRow>(
    'SELECT * FROM topics WHERE topic_id = $1',
    [topicId],
  );
  return rows[0] ? rowToTopic(rows[0]) : null;
}

export interface ListTopicsOptions {
  specialty?: string;
  chapter?: string;
  tier?: string;
  careSetting?: string;
  status?: string;
  search?: string;
  limit: number;
  offset: number;
}

export async function listTopics(opts: ListTopicsOptions) {
  const conditions: string[] = [];
  const params: unknown[] = [];

  if (opts.specialty) {
    params.push(opts.specialty);
    conditions.push(`specialty = $${params.length}`);
  }
  if (opts.chapter) {
    params.push(opts.chapter);
    conditions.push(`chapter = $${params.length}`);
  }
  if (opts.tier) {
    params.push(opts.tier);
    conditions.push(`tier = $${params.length}`);
  }
  if (opts.careSetting) {
    params.push(opts.careSetting);
    conditions.push(`$${params.length} = ANY(care_settings)`);
  }
  if (opts.status === 'stub') {
    conditions.push('content_version = 0');
  } else if (opts.status === 'enriched') {
    conditions.push(`(content_version > 0 AND review_status = 'approved')`);
  } else if (opts.status === 'pending_review') {
    conditions.push(`review_status IN ('pending_clinician_check', 'pending_board_review')`);
  } else if (opts.status === 'rejected') {
    conditions.push(`review_status = 'rejected'`);
  }
  if (opts.search) {
    params.push(opts.search);
    conditions.push(`title ILIKE '%' || $${params.length} || '%'`);
  }

  const where = conditions.length ? `WHERE ${conditions.join(' AND ')}` : '';

  const countResult = await pool.query<{ count: string }>(
    `SELECT COUNT(*) AS count FROM topics ${where}`,
    params,
  );

  const limitParams = [...params, opts.limit, opts.offset];
  const { rows } = await pool.query<TopicRow>(
    `SELECT * FROM topics ${where} ORDER BY title ASC LIMIT $${limitParams.length - 1} OFFSET $${limitParams.length}`,
    limitParams,
  );

  return {
    totalCount: Number(countResult.rows[0]?.count ?? 0),
    items: rows.map(rowToTopic),
  };
}

export async function searchTopics(query: string, limit: number) {
  const { rows } = await pool.query<TopicRow & { rank: number }>(
    `SELECT *, ts_rank(search_vector, websearch_to_tsquery('english', $1)) AS rank
     FROM topics
     WHERE search_vector @@ websearch_to_tsquery('english', $1)
     ORDER BY rank DESC
     LIMIT $2`,
    [query, limit],
  );
  return rows.map((row) => ({
    topic: rowToTopic(row),
    score: row.rank,
  }));
}

export interface ChapterRow {
  chapter: string;
  specialty: string;
  count: string;
}

export async function chapterSpecialtyCounts(): Promise<ChapterRow[]> {
  const { rows } = await pool.query<ChapterRow>(
    `SELECT chapter, specialty, COUNT(*) AS count
     FROM topics
     GROUP BY chapter, specialty
     ORDER BY chapter, specialty`,
  );
  return rows;
}

export interface SpecialtyStats {
  specialty: string;
  total: number;
  enriched: number;
  stub: number;
  pendingReview: number;
  rejected: number;
}

export interface KnowledgeHubStats {
  total: number;
  enriched: number;
  stub: number;
  pendingReview: number;
  rejected: number;
  bySpecialty: SpecialtyStats[];
}

interface StatsRow {
  specialty: string;
  total: string;
  enriched: string;
  stub: string;
  pending_review: string;
  rejected: string;
}

export async function statsBySpecialty(): Promise<KnowledgeHubStats> {
  const { rows } = await pool.query<StatsRow>(
    `SELECT specialty,
            COUNT(*) AS total,
            COUNT(*) FILTER (WHERE content_version > 0 AND review_status = 'approved') AS enriched,
            COUNT(*) FILTER (WHERE content_version = 0) AS stub,
            COUNT(*) FILTER (WHERE review_status IN ('pending_clinician_check', 'pending_board_review')) AS pending_review,
            COUNT(*) FILTER (WHERE review_status = 'rejected') AS rejected
     FROM topics
     GROUP BY specialty
     ORDER BY specialty`,
  );

  const bySpecialty: SpecialtyStats[] = rows.map((row) => ({
    specialty: row.specialty,
    total: Number(row.total),
    enriched: Number(row.enriched),
    stub: Number(row.stub),
    pendingReview: Number(row.pending_review),
    rejected: Number(row.rejected),
  }));

  return {
    total: bySpecialty.reduce((sum, s) => sum + s.total, 0),
    enriched: bySpecialty.reduce((sum, s) => sum + s.enriched, 0),
    stub: bySpecialty.reduce((sum, s) => sum + s.stub, 0),
    pendingReview: bySpecialty.reduce((sum, s) => sum + s.pendingReview, 0),
    rejected: bySpecialty.reduce((sum, s) => sum + s.rejected, 0),
    bySpecialty,
  };
}

export async function updateReviewStatus(
  topicId: string,
  reviewStatus: string,
  reviewedBy: string,
) {
  const { rows } = await pool.query<TopicRow>(
    `UPDATE topics
     SET review_status = $2, reviewed_by = $3, reviewed_at = now(), updated_at = now()
     WHERE topic_id = $1
     RETURNING *`,
    [topicId, reviewStatus, reviewedBy],
  );
  return rows[0] ? rowToTopic(rows[0]) : null;
}

export interface CatalogStub {
  topicId: string;
  title: string;
  specialty: string;
  chapter: string;
  tier?: string;
}

const TIERS = ['tier1', 'tier2', 'tier3'];

export async function insertCatalogStubs(
  stubs: CatalogStub[],
): Promise<{ inserted: number; skipped: number }> {
  if (!stubs.length) return { inserted: 0, skipped: 0 };

  const result = await pool.query(
    `INSERT INTO topics (
       topic_id, title, specialty, chapter, tier,
       content_version, review_status, content, source_file
     )
     SELECT t.topic_id, t.title, t.specialty, t.chapter, t.tier,
            0, 'approved', '{}'::jsonb, 'catalog'
     FROM UNNEST($1::text[], $2::text[], $3::text[], $4::text[], $5::text[])
       AS t(topic_id, title, specialty, chapter, tier)
     ON CONFLICT (topic_id) DO NOTHING`,
    [
      stubs.map((s) => s.topicId),
      stubs.map((s) => s.title),
      stubs.map((s) => s.specialty ?? ''),
      stubs.map((s) => s.chapter ?? ''),
      stubs.map((s) => (s.tier && TIERS.includes(s.tier) ? s.tier : 'tier2')),
    ],
  );

  const inserted = result.rowCount ?? 0;
  return { inserted, skipped: stubs.length - inserted };
}
