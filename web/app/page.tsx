import { graphqlFetch } from '@/lib/graphqlClient';
import { CHAPTERS_QUERY } from '@/lib/queries';
import type { ChapterSummary } from '@/lib/types';

// Was `revalidate = 300` (ISR) — but the per-request CSP nonce (proxy.ts)
// and ISR are fundamentally incompatible: an ISR-cached response bakes in
// whatever nonce was live at generation time, while every subsequent request
// gets a *different* fresh nonce in its CSP header. Browser rejects every
// inline script whose nonce doesn't match the header, so hydration silently
// fails on any cache hit — the page renders but nothing is clickable, and
// since this is the entry route, it takes the whole app's client-side router
// down with it. Rendering dynamically keeps the nonce consistent per request;
// this page is cheap enough (one GraphQL call, already LRU-cached server-side)
// that losing ISR here isn't a real cost.
export const dynamic = 'force-dynamic';

export default async function HomePage() {
  const { chapters } = await graphqlFetch<{ chapters: ChapterSummary[] }>(CHAPTERS_QUERY);
  const totalTopics = chapters.reduce((sum, c) => sum + c.totalCount, 0);
  const totalSpecialties = new Set(chapters.flatMap((c) => c.specialties.map((s) => s.specialty))).size;

  return (
    <main style={{ maxWidth: 720, margin: '0 auto', padding: '64px 24px 80px' }}>
      <h1 style={{ fontSize: '2rem' }}>DoctorsHero Knowledge Hub</h1>
      <p style={{ color: 'var(--text-secondary)', fontSize: '1.05rem' }}>
        {totalTopics.toLocaleString()} evidence-based clinical topics across {chapters.length} chapters
        and {totalSpecialties} specialties.
      </p>
      <div className="card" style={{ padding: '20px 24px', marginTop: 24 }}>
        <p style={{ margin: 0, color: 'var(--text-secondary)' }}>
          Use the search box or the chapter index on the left to find a topic. Anything you open shows
          up under &ldquo;Recently Visited&rdquo; on the right, so you can pick up where you left off.
        </p>
      </div>
    </main>
  );
}
