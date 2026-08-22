import { pool } from '../db/pool.js';
import { cached } from '../cache/lruCache.js';
import {
  findTopicById,
  listTopics,
  searchTopics,
  chapterSpecialtyCounts,
  statsBySpecialty,
} from '../services/topicRepository.js';

interface TopicsArgs {
  specialty?: string;
  chapter?: string;
  tier?: string;
  careSetting?: string;
  status?: string;
  search?: string;
  limit: number;
  offset: number;
}

export const resolvers = {
  Query: {
    async topic(_: unknown, { topicId }: { topicId: string }) {
      return cached(`topic:${topicId}`, () => findTopicById(topicId));
    },

    async topics(_: unknown, args: TopicsArgs) {
      const limit = Math.min(Math.max(args.limit ?? 20, 1), 500);
      const offset = Math.max(args.offset ?? 0, 0);
      const key = `topics:${args.specialty ?? ''}:${args.chapter ?? ''}:${args.tier ?? ''}:${args.careSetting ?? ''}:${args.status ?? ''}:${args.search ?? ''}:${limit}:${offset}`;
      return cached(key, () =>
        listTopics({
          specialty: args.specialty,
          chapter: args.chapter,
          tier: args.tier,
          careSetting: args.careSetting,
          status: args.status,
          search: args.search,
          limit,
          offset,
        }),
      );
    },

    async search(_: unknown, { query, limit }: { query: string; limit: number }) {
      const trimmed = query.trim();
      if (!trimmed) return [];
      const cappedLimit = Math.min(Math.max(limit ?? 20, 1), 50);
      return cached(`search:${trimmed}:${cappedLimit}`, () =>
        searchTopics(trimmed, cappedLimit),
      );
    },

    async chapters() {
      return cached('chapters', async () => {
        const rows = await chapterSpecialtyCounts();
        const byChapter = new Map<string, { specialty: string; count: number }[]>();
        for (const row of rows) {
          const list = byChapter.get(row.chapter) ?? [];
          list.push({ specialty: row.specialty, count: Number(row.count) });
          byChapter.set(row.chapter, list);
        }
        return Array.from(byChapter.entries()).map(([chapter, specialties]) => ({
          chapter,
          specialties,
          totalCount: specialties.reduce((sum, s) => sum + s.count, 0),
        }));
      });
    },

    async stats() {
      return cached('stats', () => statsBySpecialty());
    },

    async health() {
      const { rows } = await pool.query<{ count: string }>('SELECT COUNT(*) AS count FROM topics');
      return { status: 'ok', topicCount: Number(rows[0]?.count ?? 0) };
    },
  },
};
