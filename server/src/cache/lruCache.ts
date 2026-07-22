import { LRUCache } from 'lru-cache';
import { env } from '../config/env.js';

// Single in-process cache for GraphQL read results. Fine for a single-instance
// local service with rarely-changing content; swap for Redis if this ever
// needs to run as more than one instance.
const cache = new LRUCache<string, {}>({
  max: env.cacheMaxEntries,
  ttl: env.cacheTtlMs,
});

export async function cached<T>(key: string, load: () => Promise<T>): Promise<T> {
  const hit = cache.get(key);
  if (hit !== undefined) return hit as T;
  const value = await load();
  if (value !== null && value !== undefined) {
    cache.set(key, value as {});
  }
  return value;
}

export function clearCache(): void {
  cache.clear();
}
