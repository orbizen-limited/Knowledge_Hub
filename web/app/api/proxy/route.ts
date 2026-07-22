import { NextRequest, NextResponse } from 'next/server';
import { graphqlFetch } from '@/lib/graphqlClient';

// The only server-side bridge the browser ever talks to for interactive
// (client-rendered) queries — it attaches the Knowledge Hub API key itself,
// so that key never ships to the browser bundle. Static/initial page data is
// fetched directly from Server Components instead (see lib/graphqlClient.ts).
const MAX_QUERY_LENGTH = 4000;

export async function POST(req: NextRequest) {
  let body: { query?: unknown; variables?: unknown };
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ error: 'Invalid JSON body' }, { status: 400 });
  }

  if (typeof body.query !== 'string' || body.query.length === 0) {
    return NextResponse.json({ error: 'Missing query' }, { status: 400 });
  }
  if (body.query.length > MAX_QUERY_LENGTH) {
    return NextResponse.json({ error: 'Query too large' }, { status: 413 });
  }
  const variables =
    body.variables && typeof body.variables === 'object'
      ? (body.variables as Record<string, unknown>)
      : undefined;

  try {
    const data = await graphqlFetch(body.query, variables);
    return NextResponse.json({ data });
  } catch (err) {
    return NextResponse.json({ error: (err as Error).message }, { status: 502 });
  }
}
