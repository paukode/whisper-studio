import React, { useEffect, useState } from 'react';
import { MarkdownRenderer } from '@/components/markdown/MarkdownRenderer';

/**
 * PlanPanel — renders a saved plan document (data/plans/*.md) in the dock.
 * Fetches GET /api/plans/{id}; the endpoint returns the markdown (or a JSON
 * envelope with a `markdown` field). Gracefully handles a not-yet-available
 * plan so the panel degrades instead of throwing.
 */
export const PlanPanel: React.FC<{ planId: string }> = ({ planId }) => {
  const [markdown, setMarkdown] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    // Each plan is its own keyed dock panel, so `planId` is stable for this
    // instance — state starts null, no synchronous reset needed.
    let alive = true;
    fetch(`/api/plans/${encodeURIComponent(planId)}`)
      .then(async (r) => {
        if (!r.ok) throw new Error(String(r.status));
        return r.text();
      })
      .then((text) => {
        if (!alive) return;
        try {
          const parsed = JSON.parse(text) as { markdown?: string };
          setMarkdown(parsed.markdown ?? text);
        } catch {
          setMarkdown(text);
        }
      })
      .catch(() => { if (alive) setError('This plan isn’t available yet.'); });
    return () => { alive = false; };
  }, [planId]);

  return (
    <div style={{ flex: '1 1 auto', minHeight: 0, overflow: 'auto', padding: 12 }}>
      {error ? (
        <div style={{ color: 'var(--text-muted, #888)', fontSize: 13 }}>{error}</div>
      ) : markdown == null ? (
        <div style={{ color: 'var(--text-muted, #888)', fontSize: 13 }}>Loading plan…</div>
      ) : (
        <MarkdownRenderer content={markdown} />
      )}
    </div>
  );
};

export default PlanPanel;
