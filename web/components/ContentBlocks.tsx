import type { ContentBlock } from '@/lib/types';

export function ContentBlocks({ blocks }: { blocks: ContentBlock[] }) {
  return (
    <>
      {blocks.map((block, i) => (
        <div key={i} style={{ marginBottom: i === blocks.length - 1 ? 0 : 16 }}>
          {block.heading && (
            <h4 style={{ fontSize: '0.9rem', color: 'var(--text-secondary)', marginBottom: 6 }}>
              {block.heading}
            </h4>
          )}
          <ul style={{ paddingLeft: 20, margin: 0 }}>
            {block.points.map((point, j) => (
              <li key={j} style={{ marginLeft: point.level * 18, marginBottom: 4 }}>
                {point.text}
              </li>
            ))}
          </ul>
        </div>
      ))}
    </>
  );
}
