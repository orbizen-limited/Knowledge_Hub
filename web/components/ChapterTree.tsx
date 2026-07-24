'use client';

import { useCallback, useEffect, useState } from 'react';
import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { TOPICS_BY_CHAPTER_QUERY } from '@/lib/queries';
import type { ChapterSummary, Topic } from '@/lib/types';

type TopicStub = Pick<Topic, 'topicId' | 'title' | 'specialty'>;

interface VisitDetail {
  chapter: string;
}

export function ChapterTree({ chapters }: { chapters: ChapterSummary[] }) {
  const pathname = usePathname();
  const currentTopicId = pathname?.startsWith('/topics/')
    ? decodeURIComponent(pathname.slice('/topics/'.length))
    : null;

  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [topicsByChapter, setTopicsByChapter] = useState<Record<string, TopicStub[]>>({});
  const [loadingChapter, setLoadingChapter] = useState<string | null>(null);

  const loadChapter = useCallback(async (chapter: string) => {
    setLoadingChapter(chapter);
    try {
      const res = await fetch('/api/proxy', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          query: TOPICS_BY_CHAPTER_QUERY,
          variables: { chapter, limit: 200, offset: 0 },
        }),
      });
      const json = await res.json();
      setTopicsByChapter((prev) => ({ ...prev, [chapter]: json.data?.topics?.items ?? [] }));
    } finally {
      setLoadingChapter((current) => (current === chapter ? null : current));
    }
  }, []);

  const toggle = useCallback(
    (chapter: string) => {
      setExpanded((prev) => {
        const next = new Set(prev);
        if (next.has(chapter)) {
          next.delete(chapter);
        } else {
          next.add(chapter);
        }
        return next;
      });
      setTopicsByChapter((prev) => {
        if (!prev[chapter]) void loadChapter(chapter);
        return prev;
      });
    },
    [loadChapter],
  );

  // Auto-expand (and lazy-load) the chapter of whatever topic was just
  // visited, so the tree always reveals "where you are" in the book.
  useEffect(() => {
    function handler(e: Event) {
      const detail = (e as CustomEvent<VisitDetail>).detail;
      if (!detail?.chapter) return;
      setExpanded((prev) => (prev.has(detail.chapter) ? prev : new Set(prev).add(detail.chapter)));
      setTopicsByChapter((prev) => {
        if (!prev[detail.chapter]) void loadChapter(detail.chapter);
        return prev;
      });
    }
    window.addEventListener('kh-visit', handler);
    return () => window.removeEventListener('kh-visit', handler);
  }, [loadChapter]);

  return (
    <nav style={{ padding: '8px 8px 24px' }}>
      <ul style={{ listStyle: 'none', margin: 0, padding: 0 }}>
        {chapters.map((c) => {
          const label = c.chapter || 'Uncategorized';
          const isOpen = expanded.has(c.chapter);
          const topics = topicsByChapter[c.chapter];
          return (
            <li key={label} style={{ marginBottom: 2 }}>
              <button
                onClick={() => toggle(c.chapter)}
                style={{
                  display: 'flex',
                  justifyContent: 'space-between',
                  alignItems: 'center',
                  width: '100%',
                  padding: '8px 10px',
                  background: 'none',
                  border: 'none',
                  cursor: 'pointer',
                  borderRadius: 6,
                  fontFamily: 'var(--font-inter), sans-serif',
                  fontSize: '0.88rem',
                  color: 'var(--text-primary)',
                  textAlign: 'left',
                }}
              >
                <span>{label}</span>
                <span className="mono" style={{ fontSize: '0.7rem', color: 'var(--text-muted)' }}>
                  {isOpen ? '−' : '+'} {c.totalCount}
                </span>
              </button>
              {isOpen && (
                <ul
                  style={{
                    listStyle: 'none',
                    margin: '2px 0 6px',
                    padding: '0 0 0 14px',
                    borderLeft: '1px solid var(--border)',
                  }}
                >
                  {loadingChapter === c.chapter && !topics && (
                    <li className="mono" style={{ fontSize: '0.75rem', color: 'var(--text-muted)', padding: '4px 10px' }}>
                      Loading…
                    </li>
                  )}
                  {topics?.map((t) => {
                    const active = t.topicId === currentTopicId;
                    return (
                      <li key={t.topicId}>
                        <Link
                          href={`/topics/${encodeURIComponent(t.topicId)}`}
                          style={{
                            display: 'block',
                            padding: '5px 10px',
                            fontSize: '0.82rem',
                            color: active ? 'var(--accent-primary)' : 'var(--text-secondary)',
                            fontWeight: active ? 600 : 400,
                            background: active ? 'var(--bg-surface-hover)' : 'transparent',
                            borderRadius: 4,
                          }}
                        >
                          {t.title}
                        </Link>
                      </li>
                    );
                  })}
                </ul>
              )}
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
