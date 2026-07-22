import type { NextFunction, Request, Response } from 'express';
import { timingSafeEqual } from 'node:crypto';
import { env } from '../config/env.js';

function constantTimeEquals(a: string, b: string): boolean {
  const bufA = Buffer.from(a);
  const bufB = Buffer.from(b);
  if (bufA.length !== bufB.length) return false;
  return timingSafeEqual(bufA, bufB);
}

export function apiKeyAuth(req: Request, res: Response, next: NextFunction): void {
  const provided = req.header('x-api-key') ?? '';
  if (!provided || !constantTimeEquals(provided, env.apiKey)) {
    res.status(401).json({ error: 'Unauthorized' });
    return;
  }
  next();
}
