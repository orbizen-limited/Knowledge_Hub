// Server-only GraphQL client. Never import this from a Client Component —
// it reads KH_API_KEY, which must never reach the browser bundle.
import 'server-only';

const KH_API_URL = process.env.KH_API_URL ?? 'http://127.0.0.1:4400/graphql';
const KH_API_KEY = process.env.KH_API_KEY ?? '';

export interface GraphQLResponse<T> {
  data?: T;
  errors?: { message: string }[];
}

export async function graphqlFetch<T>(
  query: string,
  variables?: Record<string, unknown>,
  init?: RequestInit,
): Promise<T> {
  const res = await fetch(KH_API_URL, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-Api-Key': KH_API_KEY,
    },
    body: JSON.stringify({ query, variables }),
    ...init,
  });

  if (!res.ok) {
    throw new Error(`Knowledge Hub API request failed: ${res.status}`);
  }

  const json = (await res.json()) as GraphQLResponse<T>;
  if (json.errors?.length) {
    throw new Error(json.errors.map((e) => e.message).join('; '));
  }
  if (json.data === undefined) {
    throw new Error('Knowledge Hub API returned no data');
  }
  return json.data;
}
