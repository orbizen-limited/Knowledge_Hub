import express from 'express';
import helmet from 'helmet';
import cors from 'cors';
import rateLimit from 'express-rate-limit';
import { ApolloServer } from '@apollo/server';
import { expressMiddleware } from '@as-integrations/express5';
import depthLimit from 'graphql-depth-limit';

import { env } from './config/env.js';
import { typeDefs } from './graphql/typeDefs.js';
import { resolvers } from './graphql/resolvers.js';
import { fieldCountLimit } from './graphql/complexityRule.js';
import { apiKeyAuth } from './middleware/apiKeyAuth.js';
import { errorHandler } from './middleware/errorHandler.js';
import { logger } from './utils/logger.js';
import { pool } from './db/pool.js';

async function main() {
  const app = express();
  app.disable('x-powered-by');
  app.use(helmet());
  app.use(
    cors({
      origin: env.corsAllowedOrigins.length ? env.corsAllowedOrigins : false,
      methods: ['GET', 'POST'],
    }),
  );

  app.get('/health', (_req, res) => {
    res.json({ status: 'ok' });
  });

  const graphqlLimiter = rateLimit({
    windowMs: env.rateLimitWindowMs,
    limit: env.rateLimitMax,
    standardHeaders: true,
    legacyHeaders: false,
  });

  const apollo = new ApolloServer({
    typeDefs,
    resolvers,
    introspection: !env.isProduction,
    includeStacktraceInErrorResponses: !env.isProduction,
    validationRules: [depthLimit(env.graphqlMaxDepth), fieldCountLimit(env.graphqlMaxComplexity)],
    formatError: (formattedError, error) => {
      logger.error({ err: error }, 'GraphQL execution error');
      if (env.isProduction) {
        return { message: formattedError.message, extensions: { code: formattedError.extensions?.code } };
      }
      return formattedError;
    },
  });
  await apollo.start();

  app.use(
    '/graphql',
    graphqlLimiter,
    apiKeyAuth,
    express.json({ limit: '1mb' }),
    expressMiddleware(apollo),
  );

  app.use(errorHandler);

  app.listen(env.port, () => {
    logger.info(`Knowledge Hub API listening on http://127.0.0.1:${env.port}/graphql`);
  });
}

main().catch(async (err) => {
  logger.error({ err }, 'Fatal startup error');
  await pool.end().catch(() => undefined);
  process.exit(1);
});
