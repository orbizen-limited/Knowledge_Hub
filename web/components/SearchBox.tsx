'use client';

import { useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import { SEARCH_QUERY } from '@/lib/queries';
import type { SearchResult } from '@/lib/types';

export default function SearchBox({ compact = false }: { compact?: boolean }) {
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
    <div style={{ position: 'relative', maxWidth: compact ? '100%' : 560 }}>
      <input
        type="search"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        placeholder={compact ? 'Search topics…' : 'Search topics — e.g. hyperkalemia, ACLS, mitral regurgitation…'}
        style={{
          width: '100%',
          padding: compact ? '9px 12px' : '12px 16px',
          fontSize: compact ? '0.85rem' : '1rem',
          fontFamily: 'var(--font-inter), sans-serif',
          border: '1px solid var(--border)',
          borderRadius: 'var(--radius)',
          background: 'var(--bg-surface)',
          color: 'var(--text-primary)',
        }}
      />
      {loading && (
        <div className="mono" style={{ marginTop: 6, fontSize: '0.75rem', color: 'var(--text-muted)' }}>
          Searching…
        </div>
      )}
      {visibleResults.length > 0 && (
        <ul
          className={compact ? 'card-floating' : 'card'}
          style={{
            listStyle: 'none',
            margin: 0,
            padding: 0,
            maxHeight: compact ? 360 : 420,
            overflowY: 'auto',
            ...(compact
              ? { position: 'absolute', top: '100%', left: 0, right: 0, marginTop: 6, zIndex: 20 }
              : { marginTop: 8 }),
          }}
        >
          {visibleResults.map(({ topic, score }) => (
            <li key={topic.topicId} style={{ borderBottom: '1px solid var(--border)' }}>
              <Link
                href={`/topics/${encodeURIComponent(topic.topicId)}`}
                style={{ display: 'block', padding: compact ? '8px 12px' : '10px 14px' }}
              >
                <div style={{ fontWeight: 600, fontSize: compact ? '0.85rem' : '1rem', color: 'var(--text-primary)' }}>
                  {topic.title}
                </div>
                <div className="mono" style={{ fontSize: '0.7rem', color: 'var(--text-muted)' }}>
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
