/**
 * `/doctor context` — what the model is actually being sent, and what is
 * wasteful about it.
 *
 * Backed by GET /api/doctor/context (server/context_report.py). Every row is a
 * cost paid on some cadence: a cached section is paid once per session, an
 * uncached one on every round of every turn, and a duplicated phrase twice over.
 */

export interface ContextReport {
  workspace: string | null;
  sections: Array<{
    name: string;
    layer: number;
    layer_name: string;
    cached: boolean;
    chars: number;
    tokens_est: number;
  }>;
  totals: {
    chars: number;
    tokens_est: number;
    cached_chars: number;
    cached_tokens_est: number;
    cached_pct: number;
  };
  tools: {
    advertised: number;
    advertised_tokens_est: number;
    deferred: number;
    deferred_tokens_est: number;
  };
  duplication: Array<{ section: string; tool: string; words: number; phrase: string }>;
  missing_tool_refs: Array<{ section: string; tool: string }>;
  warnings: string[];
}

const CARD: React.CSSProperties = {
  background: 'var(--bg-surface)',
  border: '1px solid var(--border)',
  borderRadius: 8,
  overflow: 'hidden',
  fontSize: 12,
};

const HEAD: React.CSSProperties = {
  padding: '8px 12px',
  background: 'var(--bg-elevated)',
  borderBottom: '1px solid var(--border)',
  fontWeight: 600,
  display: 'flex',
  alignItems: 'center',
  gap: 8,
};

const ROW: React.CSSProperties = {
  display: 'flex',
  alignItems: 'baseline',
  gap: 10,
  padding: '6px 12px',
  borderBottom: '1px solid var(--border)',
};

const MUTED: React.CSSProperties = { color: 'var(--text-muted)' };

/** Share of the prompt this section accounts for, as a bar the eye can scan. */
function Bar({ pct, cached }: { pct: number; cached: boolean }) {
  return (
    <span
      aria-hidden
      style={{
        display: 'inline-block',
        width: 60,
        height: 6,
        borderRadius: 3,
        background: 'var(--bg-inset)',
        overflow: 'hidden',
        flexShrink: 0,
      }}
    >
      <span
        style={{
          display: 'block',
          width: `${Math.max(2, Math.min(100, pct))}%`,
          height: '100%',
          background: cached ? '#5dba6e' : '#ffa657',
        }}
      />
    </span>
  );
}

export function ContextReportPanel({ report }: { report: ContextReport }) {
  const { totals, tools, sections, duplication, missing_tool_refs: missing, warnings } = report;
  const uncached = totals.tokens_est - totals.cached_tokens_est;
  const max = Math.max(1, ...sections.map((s) => s.tokens_est));

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      <div style={CARD}>
        <div style={HEAD}>
          <span>📏</span> System prompt: ~{totals.tokens_est} tokens, {totals.cached_pct}% cached
        </div>
        <div style={{ ...ROW, ...MUTED }}>
          <span>
            ~{totals.cached_tokens_est} cached (once per session) · ~{uncached} uncached
            (every round) · tools: {tools.advertised} advertised ~
            {tools.advertised_tokens_est}t, {tools.deferred} deferred ~
            {tools.deferred_tokens_est}t behind tool_search
          </span>
        </div>
        {sections.map((s) => (
          <div key={s.name} style={ROW}>
            <Bar pct={(s.tokens_est / max) * 100} cached={s.cached} />
            <span style={{ fontWeight: 500, minWidth: 150, flexShrink: 0 }}>{s.name}</span>
            <span style={{ ...MUTED, minWidth: 70, flexShrink: 0 }}>~{s.tokens_est}t</span>
            <span style={MUTED}>{s.cached ? 'cached' : `per-request (${s.layer_name})`}</span>
          </div>
        ))}
      </div>

      {warnings.length > 0 && (
        <div style={CARD}>
          <div style={HEAD}>
            <span style={{ color: '#ffa657' }}>⚠</span> {warnings.length} finding
            {warnings.length === 1 ? '' : 's'}
          </div>
          {warnings.map((w, i) => (
            <div key={i} style={ROW}>
              <span>{w}</span>
            </div>
          ))}
        </div>
      )}

      {duplication.length > 0 && (
        <div style={CARD}>
          <div style={HEAD}>Duplicated between the prompt and a tool description</div>
          {duplication.map((d, i) => (
            <div key={i} style={{ ...ROW, flexDirection: 'column', gap: 3 }}>
              <span style={{ fontWeight: 500 }}>
                {d.section} ↔ {d.tool}{' '}
                <span style={MUTED}>({d.words} words)</span>
              </span>
              <span style={{ ...MUTED, fontFamily: 'var(--font-mono, monospace)' }}>{d.phrase}</span>
            </div>
          ))}
        </div>
      )}

      {missing.length > 0 && (
        <div style={CARD}>
          <div style={HEAD}>
            <span style={{ color: '#e06060' }}>✗</span> Prompt names tools that do not exist
          </div>
          {missing.map((m, i) => (
            <div key={i} style={ROW}>
              <span style={{ fontWeight: 500, minWidth: 150 }}>{m.section}</span>
              <span style={{ fontFamily: 'var(--font-mono, monospace)' }}>{m.tool}</span>
            </div>
          ))}
        </div>
      )}

      {warnings.length === 0 && duplication.length === 0 && missing.length === 0 && (
        <div style={{ ...CARD, ...ROW, color: '#5dba6e' }}>
          <span>✓ No duplication, no dead tool references, nothing stranded uncached.</span>
        </div>
      )}
    </div>
  );
}
