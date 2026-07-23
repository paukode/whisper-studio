/** Findings list for a CI autofix (WS-J), shown just above the fix preview. */
import React from 'react';

interface Diagnosis {
  branch?: string;
  run_id?: number | null;
  url?: string | null;
  findings?: Array<Record<string, unknown>>;
}

const s = (v: unknown): string => (v == null ? '' : String(v));

export const CIDiagnosisCard: React.FC<{ data: Diagnosis }> = ({ data }) => {
  const findings = data.findings ?? [];
  return (
    <div className="workflow-card ci-diagnosis">
      <div className="workflow-card-head">
        <span className="workflow-card-icon" aria-hidden="true">🔍</span>
        <span className="workflow-card-title">CI diagnosis · {s(data.branch) || 'branch'}</span>
        <span className="workflow-card-badge">{findings.length} finding{findings.length === 1 ? '' : 's'}</span>
      </div>
      {findings.length === 0 ? (
        <div className="workflow-card-meta">No actionable failure found in the logs.</div>
      ) : (
        <ul className="ci-findings">
          {findings.map((f, i) => (
            <li key={i} className="ci-finding">
              <span className="ci-finding-cat">{s(f.category) || 'other'}</span>
              <strong>{s(f.check)}</strong>
              {s(f.summary) && <>: {s(f.summary)}</>}
              {s(f.suggested_fix) && <div className="ci-finding-fix">→ {s(f.suggested_fix)}</div>}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
};
