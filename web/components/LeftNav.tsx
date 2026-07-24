import Link from 'next/link';
import { graphqlFetch } from '@/lib/graphqlClient';
import { CHAPTERS_QUERY } from '@/lib/queries';
import type { ChapterSummary } from '@/lib/types';
import SearchBox from '@/components/SearchBox';
import { ChapterTree } from '@/components/ChapterTree';

export default async function LeftNav() {
  const { chapters } = await graphqlFetch<{ chapters: ChapterSummary[] }>(CHAPTERS_QUERY);
  const sorted = [...chapters].sort((a, b) => b.totalCount - a.totalCount);

  return (
    <div className="app-shell-left">
      <div style={{ padding: '18px 16px 12px', position: 'sticky', top: 0, background: 'var(--bg-surface)', zIndex: 10 }}>
        <Link href="/" style={{ display: 'block', marginBottom: 12, color: 'var(--text-primary)' }}>
          <span style={{ fontFamily: '"Product Sans", var(--font-inter), sans-serif', fontWeight: 700, fontSize: '1.05rem' }}>
            Knowledge Hub
          </span>
        </Link>
        <SearchBox compact />
      </div>
      <ChapterTree chapters={sorted} />
    </div>
  );
}
