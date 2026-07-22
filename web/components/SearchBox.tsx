'use client';

import { useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import { SEARCH_QUERY } from '@/lib/queries';
import type { SearchResult } from '@/lib/types';

export default function SearchBox() {
  const [query, setQuery] = useState('');
  const [results, setResults] = useState<SearchResult[]>([]);
  const [loading, setLoading] = useState(false);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const trimmedQuery = query.trim();

  useEffect(() => {
    if (debounceRef.current) clearTimeout(debounceRef.current);
    const trimmed = trimmedQuery;
    if (!trimmed) return;
    debounceRef.current = setTimeout(async () => {
      setLoading(true);
      try {
        const res = await fetch('/api/proxy', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ query: SEARCH_QUERY, variables: { query: trimmed, limit: 12 } }),
        });
        const json = await res.json();
        setResults(json.data?.search ?? []);
      } catch {
        setResults([]);
      } finally {
        setLoading(false);
      }
    }, 300);
    return () => {
      if (debounceRef.current) clearTimeout(debounceRef.current);
    };
  }, [trimmedQuery]);

  const visibleResults = trimmedQuery ? results : [];

  return (
    <div style={{ position: 'relative', maxWidth: 560 }}>
      <input
        type="search"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        placeholder="Search topics — e.g. hyperkalemia, ACLS, mitral regurgitation…"
        style={{
          width: '100%',
          padding: '12px 16px',
          fontSize: '1rem',
          fontFamily: 'var(--font-source-serif), serif',
          border: '1px solid var(--border)',
          borderRadius: 'var(--radius)',
          background: 'var(--bg-surface)',
          color: 'var(--text-primary)',
        }}
      />
      {loading && (
        <div className="mono" style={{ marginTop: 6, fontSize: '0.8rem', color: 'var(--text-muted)' }}>
          Searching…
        </div>
      )}
      {visibleResults.length > 0 && (
        <ul
          className="card"
          style={{
            listStyle: 'none',
            margin: '8px 0 0 0',
            padding: 0,
            maxHeight: 420,
            overflowY: 'auto',
          }}
        >
          {visibleResults.map(({ topic, score }) => (
            <li key={topic.topicId} style={{ borderBottom: '1px solid var(--border)' }}>
              <Link
                href={`/topics/${encodeURIComponent(topic.topicId)}`}
                style={{ display: 'block', padding: '10px 14px' }}
              >
                <div style={{ fontWeight: 600, color: 'var(--text-primary)' }}>{topic.title}</div>
                <div className="mono" style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>
                  {topic.specialty} · relevance {score.toFixed(2)}
                </div>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
