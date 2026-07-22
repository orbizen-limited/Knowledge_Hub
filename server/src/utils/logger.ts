import pino from 'pino';
import { env } from '../config/env.js';

// Never log request/response bodies or headers here — the API key must
// never end up in logs even at debug level.
export const logger = pino({
  level: env.isProduction ? 'info' : 'debug',
  redact: ['req.headers.authorization', 'req.headers["x-api-key"]'],
});
