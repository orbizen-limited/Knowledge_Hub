-- Knowledge Hub schema. Idempotent: safe to run repeatedly.
-- Indexed metadata columns for filtering/sorting/search; everything else
-- (recommendations, drug regimens, DynaMed-style content blocks, etc.) lives
-- in `content` JSONB verbatim from the source KnowledgeTopic.toJson() shape.

CREATE TABLE IF NOT EXISTS topics (
  topic_id            TEXT PRIMARY KEY,
  title                TEXT NOT NULL,
  specialty            TEXT NOT NULL DEFAULT '',
  chapter              TEXT NOT NULL DEFAULT '',
  tier                 TEXT NOT NULL DEFAULT 'tier2'
                         CHECK (tier IN ('tier1', 'tier2', 'tier3')),
  content_version      INTEGER NOT NULL DEFAULT 1,
  last_updated         TIMESTAMPTZ,
  review_status        TEXT NOT NULL DEFAULT 'approved'
                         CHECK (review_status IN (
                           'pending_clinician_check', 'pending_board_review',
                           'approved', 'rejected'
                         )),
  reviewed_by          TEXT NOT NULL DEFAULT '',
  reviewed_at          TIMESTAMPTZ,
  bottom_line          TEXT NOT NULL DEFAULT '',
  agent_generated      BOOLEAN NOT NULL DEFAULT false,
  keywords             TEXT[] NOT NULL DEFAULT '{}',
  care_settings        TEXT[] NOT NULL DEFAULT '{}',
  summary_paragraphs   TEXT[] NOT NULL DEFAULT '{}',
  content              JSONB NOT NULL DEFAULT '{}'::jsonb,
  source_file          TEXT NOT NULL DEFAULT '',
  search_vector        TSVECTOR,
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- to_tsvector(regconfig, text) is STABLE, not IMMUTABLE, so it can't back a
-- GENERATED column directly — maintain search_vector via trigger instead.
CREATE OR REPLACE FUNCTION topics_search_vector_trigger() RETURNS trigger AS $$
BEGIN
  NEW.search_vector :=
    setweight(to_tsvector('english', coalesce(NEW.title, '')), 'A') ||
    setweight(to_tsvector('english', coalesce(NEW.bottom_line, '')), 'B') ||
    setweight(to_tsvector('english', array_to_string(NEW.keywords, ' ')), 'B') ||
    setweight(to_tsvector('english', array_to_string(NEW.summary_paragraphs, ' ')), 'C');
  RETURN NEW;
END
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_topics_search_vector ON topics;
CREATE TRIGGER trg_topics_search_vector
  BEFORE INSERT OR UPDATE ON topics
  FOR EACH ROW EXECUTE FUNCTION topics_search_vector_trigger();

CREATE INDEX IF NOT EXISTS idx_topics_search_vector ON topics USING GIN (search_vector);
CREATE INDEX IF NOT EXISTS idx_topics_keywords ON topics USING GIN (keywords);
CREATE INDEX IF NOT EXISTS idx_topics_content ON topics USING GIN (content jsonb_path_ops);
CREATE INDEX IF NOT EXISTS idx_topics_specialty ON topics (specialty);
CREATE INDEX IF NOT EXISTS idx_topics_chapter ON topics (chapter);
CREATE INDEX IF NOT EXISTS idx_topics_tier ON topics (tier);
CREATE INDEX IF NOT EXISTS idx_topics_review_status ON topics (review_status);
