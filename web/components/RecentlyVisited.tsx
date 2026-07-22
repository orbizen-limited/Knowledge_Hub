'use client';

import { useSyncExternalStore } from 'react';
import Link from 'next/link';
import type { VisitedTopic } from './RecordVisit';

interface RecentEntry extends VisitedTopic {
  visitedAt: number;
}

const STORAGE_KEY = 'kh_recent_topics';
const MAX_ENTRIES = 15;
const EMPTY: RecentEntry[] = [];

let cache: RecentEntry[] = EMPTY;
let cachedRaw: string | null = null;

function getSnapshot(): RecentEntry[] {
  const raw = window.localStorage.getItem(STORAGE_KEY);
  if (raw === cachedRaw) return cache;
  cachedRaw = raw;
  try {
    cache = raw ? (JSON.parse(raw) as RecentEntry[]) : EMPTY;
  } catch {
    cache = EMPTY;
  }
  return cache;
}

function getServerSnapshot(): RecentEntry[] {
  return EMPTY;
}

// localStorage is an external store — React's own escape hatch for
// subscribing to it (useSyncExternalStore) avoids ever calling setState
// inside an effect, which is what a plain useState+useEffect hydration
// pattern would otherwise require.
function subscribe(onStoreChange: () => void) {
  function handleVisit(e: Event) {
    const detail = (e as CustomEvent<RecentEntry>).detail;
    if (!detail?.topicId) return;
    const current = getSnapshot();
    const next = [detail, ...current.filter((p) => p.topicId !== detail.topicId)].slice(0, MAX_ENTRIES);
    try {
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
    } catch {
      // localStorage unavailable (private browsing, quota) — in-memory cache still updates below
    }
    cachedRaw = null;
    cache = next;
    onStoreChange();
  }
  window.addEventListener('kh-visit', handleVisit);
  window.addEventListener('storage', onStoreChange);
  return () => {
    window.removeEventListener('kh-visit', handleVisit);
    window.removeEventListener('storage', onStoreChange);
  };
}

export function RecentlyVisited() {
  const entries = useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot);

  return (
    <div className="app-shell-right">
      <h4
        className="mono"
        style={{
          fontSize: '0.75rem',
          textTransform: 'uppercase',
          letterSpacing: '0.06em',
          color: 'var(--text-muted)',
          padding: '18px 16px 10px',
          margin: 0,
        }}
      >
        Recently Visited
      </h4>
      {entries.length === 0 ? (
        <p className="mono" style={{ fontSize: '0.75rem', color: 'var(--text-muted)', padding: '0 16px' }}>
          Topics you open will appear here.
        </p>
      ) : (
        <ul style={{ listStyle: 'none', margin: 0, padding: '0 8px' }}>
          {entries.map((e) => (
            <li key={e.topicId}>
              <Link
                href={`/topics/${encodeURIComponent(e.topicId)}`}
                style={{ display: 'block', padding: '8px 10px', borderRadius: 6 }}
              >
                <div style={{ fontSize: '0.85rem', color: 'var(--text-primary)' }}>{e.title}</div>
                <div className="mono" style={{ fontSize: '0.7rem', color: 'var(--text-muted)' }}>
                  {e.specialty}
                </div>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
