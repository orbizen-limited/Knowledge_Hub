import type { NextFunction, Request, Response } from 'express';
import { logger } from '../utils/logger.js';

// Generic Express-level error handler (e.g. malformed JSON bodies). GraphQL
// execution errors are handled separately via Apollo's formatError so they
// never leak stack traces to the client either.
// eslint-disable-next-line @typescript-eslint/no-unused-vars
export function errorHandler(err: unknown, req: Request, res: Response, _next: NextFunction): void {
  logger.error({ err }, 'Unhandled request error');
  if (res.headersSent) return;
  res.status(500).json({ error: 'Internal server error' });
}
