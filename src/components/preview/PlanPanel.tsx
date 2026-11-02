import React, { useEffect, useState } from 'react';
import { MarkdownRenderer } from '@/components/markdown/MarkdownRenderer';
import { readErrorBody } from '@/api/client';

/**
 * PlanPanel: renders a saved plan document (data/plans/*.md) in the dock.
 * GET /api/plans/{id} returns the markdown as plain text, or a 404 when the
 * plan does not exist; any failure is shown with its reason.
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
        if (r.status === 404) throw new Error('Plan not found.');
        if (!r.ok) throw new Error(`Could not load the plan: ${await readErrorBody(r)}`);
        return r.text();
      })
      .then((text) => {
        if (alive) setMarkdown(text);
      })
      .catch((err: unknown) => {
        if (alive) setError(err instanceof Error ? err.message : String(err));
      });
    return () => { alive = false; };
  }, [planId]);

  return (
    <div style={{ flex: '1 1 auto', minHeight: 0, overflow: 'auto', padding: 12 }}>
      {error ? (
        <div style={{ color: 'var(--text-muted)', fontSize: 14 }}>{error}</div>
      ) : markdown == null ? (
        <div style={{ color: 'var(--text-muted)', fontSize: 14 }}>Loading plan…</div>
      ) : (
        <MarkdownRenderer content={markdown} />
      )}
    </div>
  );
};

