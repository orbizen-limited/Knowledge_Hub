import type { ReactNode } from 'react';

interface SectionCardProps {
  id: string;
  title: string;
  colorVar: string;
  children: ReactNode;
}

export function SectionCard({ id, title, colorVar, children }: SectionCardProps) {
  return (
    <section
      id={id}
      className="card"
      style={{ padding: '20px 24px', marginBottom: 20, borderLeft: `4px solid var(${colorVar})`, scrollMarginTop: 16 }}
    >
      <h3 style={{ fontSize: '1.05rem', color: `var(${colorVar})`, marginBottom: 12 }}>{title}</h3>
      {children}
    </section>
  );
}

export function BulletList({ items }: { items: string[] }) {
  return (
    <ul style={{ paddingLeft: 20, margin: 0 }}>
      {items.map((item, i) => (
        <li key={i} style={{ marginBottom: 6 }}>
          {item}
        </li>
      ))}
    </ul>
  );
}
