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
