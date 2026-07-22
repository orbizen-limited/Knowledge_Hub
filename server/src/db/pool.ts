import { Pool } from 'pg';
import { env } from '../config/env.js';

export const pool = new Pool({
  connectionString: env.databaseUrl,
  max: 10,
  idleTimeoutMillis: 30_000,
});

pool.on('error', (err) => {
  // Idle client errors (e.g. connection dropped) must not crash the process.
  // eslint-disable-next-line no-console
  console.error('Unexpected Postgres pool error', err);
});
