'use client';

import { useEffect } from 'react';

export interface VisitedTopic {
  topicId: string;
  title: string;
  specialty: string;
  chapter: string;
}

// Renders nothing — just announces "this topic was opened" once per mount so
// the right-hand Recently Visited panel and the left chapter tree (which
// auto-expands the current chapter) can react, without either of them
// needing a prop path from this page.
export function RecordVisit({ topicId, title, specialty, chapter }: VisitedTopic) {
  useEffect(() => {
    window.dispatchEvent(
      new CustomEvent('kh-visit', { detail: { topicId, title, specialty, chapter, visitedAt: Date.now() } }),
    );
  }, [topicId, title, specialty, chapter]);

  return null;
}
