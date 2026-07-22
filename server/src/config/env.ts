import 'dotenv/config';

function required(name: string): string {
  const value = process.env[name];
  if (!value) throw new Error(`Missing required env var: ${name}`);
  return value;
}

function int(name: string, fallback: number): number {
  const raw = process.env[name];
  if (!raw) return fallback;
  const parsed = Number.parseInt(raw, 10);
  return Number.isFinite(parsed) ? parsed : fallback;
}

export const env = {
  nodeEnv: process.env.NODE_ENV ?? 'development',
  isProduction: process.env.NODE_ENV === 'production',
  port: int('PORT', 4400),
  databaseUrl: required('DATABASE_URL'),
  ingestSourceDir: process.env.INGEST_SOURCE_DIR ?? '',
  apiKey: required('API_KEY'),
  corsAllowedOrigins: (process.env.CORS_ALLOWED_ORIGINS ?? '')
    .split(',')
    .map((origin) => origin.trim())
    .filter(Boolean),
  graphqlMaxDepth: int('GRAPHQL_MAX_DEPTH', 10),
  graphqlMaxComplexity: int('GRAPHQL_MAX_COMPLEXITY', 2000),
  rateLimitWindowMs: int('RATE_LIMIT_WINDOW_MS', 60_000),
  rateLimitMax: int('RATE_LIMIT_MAX', 200),
  cacheMaxEntries: int('CACHE_MAX_ENTRIES', 500),
  cacheTtlMs: int('CACHE_TTL_MS', 300_000),
};
