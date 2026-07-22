import Link from 'next/link';
import { graphqlFetch } from '@/lib/graphqlClient';
import { CHAPTERS_QUERY, TOPICS_BY_CHAPTER_QUERY } from '@/lib/queries';
import type { ChapterSummary, Topic } from '@/lib/types';
import SearchBox from '@/components/SearchBox';

export const revalidate = 300;

interface HomePageProps {
  searchParams: Promise<{ chapter?: string }>;
}

export default async function HomePage({ searchParams }: HomePageProps) {
  const { chapter } = await searchParams;
  const { chapters } = await graphqlFetch<{ chapters: ChapterSummary[] }>(CHAPTERS_QUERY);
  const sorted = [...chapters].sort((a, b) => b.totalCount - a.totalCount);
  const totalTopics = chapters.reduce((sum, c) => sum + c.totalCount, 0);

  let chapterTopics: Pick<Topic, 'topicId' | 'title' | 'specialty' | 'bottomLine'>[] | null = null;
  if (chapter) {
    const data = await graphqlFetch<{
      topics: { totalCount: number; items: Pick<Topic, 'topicId' | 'title' | 'specialty' | 'bottomLine'>[] };
    }>(TOPICS_BY_CHAPTER_QUERY, { chapter, limit: 100, offset: 0 });
    chapterTopics = data.topics.items;
  }

  return (
    <main style={{ maxWidth: 960, margin: '0 auto', padding: '48px 24px 80px' }}>
      <header style={{ marginBottom: 32 }}>
        <h1 style={{ fontSize: '2rem' }}>DoctorsHero Knowledge Hub</h1>
        <p style={{ color: 'var(--text-secondary)' }}>
          {totalTopics.toLocaleString()} evidence-based clinical topics across {chapters.length}{' '}
          chapters.
        </p>
      </header>

      <section style={{ marginBottom: 48 }}>
        <SearchBox />
      </section>

      {chapterTopics ? (
        <section>
          <Link href="/" className="mono" style={{ fontSize: '0.8rem' }}>
            ← All chapters
          </Link>
          <h2 style={{ fontSize: '1.25rem', margin: '12px 0 16px' }}>
            {chapter || 'Uncategorized'} ({chapterTopics.length})
          </h2>
          <div className="card" style={{ padding: 0 }}>
            {chapterTopics.map((topic, i) => (
              <Link
                key={topic.topicId}
                href={`/topics/${encodeURIComponent(topic.topicId)}`}
                style={{
                  display: 'block',
                  padding: '14px 18px',
                  borderBottom: i === chapterTopics.length - 1 ? 'none' : '1px solid var(--border)',
                }}
              >
                <div style={{ fontWeight: 600, color: 'var(--text-primary)' }}>{topic.title}</div>
                <div className="mono" style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>
                  {topic.specialty}
                </div>
              </Link>
            ))}
          </div>
        </section>
      ) : (
        <section>
          <h2 style={{ fontSize: '1.25rem', marginBottom: 16 }}>Browse by chapter</h2>
          <div
            style={{
              display: 'grid',
              gridTemplateColumns: 'repeat(auto-fill, minmax(260px, 1fr))',
              gap: 16,
            }}
          >
            {sorted.map((c) => (
              <div key={c.chapter || '(uncategorized)'} className="card" style={{ padding: 18 }}>
                <h3 style={{ fontSize: '1rem', marginBottom: 8 }}>{c.chapter || 'Uncategorized'}</h3>
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginBottom: 10 }}>
                  {c.specialties.map((s) => (
                    <span key={s.specialty} className="chip">
                      {s.specialty} · {s.count}
                    </span>
                  ))}
                </div>
                <Link href={`/?chapter=${encodeURIComponent(c.chapter)}`} className="mono" style={{ fontSize: '0.8rem' }}>
                  View {c.totalCount} topic{c.totalCount === 1 ? '' : 's'} →
                </Link>
              </div>
            ))}
          </div>
        </section>
      )}
    </main>
  );
}
