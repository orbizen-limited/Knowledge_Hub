// Internal write endpoints for pipeline automation (topic ingest, catalog
// stubs, review-status changes). Loopback-only on top of the shared API-key
// check — these must never be reachable from off-box even if the service is
// ever accidentally bound to a public interface.

import express, { Router } from 'express';
import type { NextFunction, Request, Response } from 'express';
import { apiKeyAuth } from '../middleware/apiKeyAuth.js';
import { clearCache } from '../cache/lruCache.js';
import { upsertTopic, isReviewStatus, REVIEW_STATUSES } from '../services/ingestTopic.js';
import {
  insertCatalogStubs,
  updateReviewStatus,
  type CatalogStub,
} from '../services/topicRepository.js';

const LOOPBACK_IPS = new Set(['127.0.0.1', '::1', '::ffff:127.0.0.1']);

function loopbackOnly(req: Request, res: Response, next: NextFunction): void {
  if (!req.ip || !LOOPBACK_IPS.has(req.ip)) {
    res.status(403).json({ ok: false, error: 'Forbidden: loopback only' });
    return;
  }
  next();
}

function badRequest(res: Response, error: string): void {
  res.status(400).json({ ok: false, error });
}

export const internalRouter = Router();

internalRouter.use(loopbackOnly, apiKeyAuth, express.json({ limit: '10mb' }));

internalRouter.post('/ingest-topic', async (req, res, next) => {
  try {
    const { topic, sourceFile, reviewStatus } = req.body ?? {};

    if (!topic || typeof topic !== 'object' || Array.isArray(topic)) {
      badRequest(res, 'topic (object) is required');
      return;
    }
    if (typeof topic.topicId !== 'string' || !topic.topicId.trim()) {
      badRequest(res, 'topic.topicId is required');
      return;
    }
    if (reviewStatus !== undefined && !isReviewStatus(reviewStatus)) {
      badRequest(res, `reviewStatus must be one of: ${REVIEW_STATUSES.join(', ')}`);
      return;
    }
    if (sourceFile !== undefined && typeof sourceFile !== 'string') {
      badRequest(res, 'sourceFile must be a string');
      return;
    }

    const result = await upsertTopic(topic, {
      sourceFile: sourceFile ?? 'internal-api',
      reviewStatus,
    });
    clearCache();
    res.json({ ok: true, topicId: result.topicId, action: result.action });
  } catch (err) {
    next(err);
  }
});

internalRouter.post('/ingest-catalog', async (req, res, next) => {
  try {
    const { stubs } = req.body ?? {};

    if (!Array.isArray(stubs) || stubs.length === 0) {
      badRequest(res, 'stubs (non-empty array) is required');
      return;
    }
    for (let i = 0; i < stubs.length; i += 1) {
      const stub = stubs[i];
      if (
        !stub ||
        typeof stub !== 'object' ||
        typeof stub.topicId !== 'string' ||
        !stub.topicId.trim() ||
        typeof stub.title !== 'string' ||
        !stub.title.trim()
      ) {
        badRequest(res, `stubs[${i}] must have non-empty string topicId and title`);
        return;
      }
    }

    const { inserted, skipped } = await insertCatalogStubs(stubs as CatalogStub[]);
    clearCache();
    res.json({ ok: true, inserted, skipped });
  } catch (err) {
    next(err);
  }
});

internalRouter.patch('/topics/:topicId/review-status', async (req, res, next) => {
  try {
    const { reviewStatus, reviewedBy } = req.body ?? {};

    if (!isReviewStatus(reviewStatus)) {
      badRequest(res, `reviewStatus must be one of: ${REVIEW_STATUSES.join(', ')}`);
      return;
    }
    if (reviewedBy !== undefined && typeof reviewedBy !== 'string') {
      badRequest(res, 'reviewedBy must be a string');
      return;
    }

    const topicId = req.params.topicId;
    const updated = await updateReviewStatus(topicId, reviewStatus, reviewedBy ?? '');
    if (!updated) {
      res.status(404).json({ ok: false, error: 'Topic not found' });
      return;
    }
    clearCache();
    res.json({ ok: true, topicId, reviewStatus });
  } catch (err) {
    next(err);
  }
});
