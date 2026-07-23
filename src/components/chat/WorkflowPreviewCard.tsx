/**
 * Approval preview for a NEW workflow script (WS-D). Shows the phases as a
 * stepper, the script (collapsible), the budget, and Approve/Deny. Approve
 * launches the run via the REST API; the run then executes detached and its
 * completion arrives as a WorkflowRunCard.
 */
import React, { useState } from 'react';
import { launchRun } from '@/api/workflows';
import { useSessionStore } from '@/stores/sessionStore';
import { useWorkflowStore } from '@/stores/workflowStore';
import { WorkflowRunCard } from '@/components/chat/WorkflowRunCard';

interface Preview {
  script: string;
  name?: string;
  description?: string;
  phases?: unknown[];
  budget_usd?: number | null;
  args?: unknown;
  model_id?: string;
}

function phaseTitle(p: unknown): string {
  if (typeof p === 'string') return p;
  if (p && typeof p === 'object' && 'title' in p) return String((p as { title: unknown }).title);
  return String(p);
}

// A preview message persists in chat history, but the approve state is
// component-local — so a page reload would reset it to 'idle' and let the SAME
// script be launched twice. We remember launched (scriptHash -> runId) in
// localStorage, keyed by a stable hash of the script, so a reloaded preview
// restores its launched run instead of offering Approve again.
const LAUNCHED_KEY = 'whisper.wf-launched';

function hashScript(s: string): string {
  let h = 5381;
  for (let i = 0; i < s.length; i++) h = ((h << 5) + h + s.charCodeAt(i)) | 0;
  return (h >>> 0).toString(36);
}
function readLaunched(): Record<string, string> {
  try {
    return JSON.parse(localStorage.getItem(LAUNCHED_KEY) || '{}') as Record<string, string>;
  } catch {
    return {};
  }
}
function rememberLaunched(scriptHash: string, runId: string): void {
  try {
    const m = readLaunched();
    m[scriptHash] = runId;
    // Bound growth: keep the most-recent ~50 entries.
    const keys = Object.keys(m);
    if (keys.length > 50) delete m[keys[0]];
    localStorage.setItem(LAUNCHED_KEY, JSON.stringify(m));
  } catch {
    /* ignore quota / disabled storage */
  }
}

export const WorkflowPreviewCard: React.FC<{ preview: Preview }> = ({ preview }) => {
  const sessionId = useSessionStore((s) => s.currentSessionId);
  const scriptHash = hashScript(preview.script);
  const priorRun = readLaunched()[scriptHash] ?? null;
  const [state, setState] = useState<'idle' | 'launching' | 'launched' | 'denied' | 'error'>(
    priorRun ? 'launched' : 'idle',
  );
  const [runId, setRunId] = useState<string | null>(priorRun);
  const [showScript, setShowScript] = useState(false);
  const phases = (preview.phases ?? []).map(phaseTitle);

  const approve = async () => {
    if (!sessionId) return;
    setState('launching');
    try {
      const r = await launchRun({
        script: preview.script,
        session_id: sessionId,
        args: preview.args,
        budget_usd: preview.budget_usd ?? null,
        model_id: preview.model_id,
      });
      setRunId(r.run_id);
      setState('launched');
      rememberLaunched(scriptHash, r.run_id);
      useWorkflowStore.getState().upsertRun({
        run_id: r.run_id, name: preview.name ?? '', status: 'running',
        agents_spawned: 0, tokens_in: 0, tokens_out: 0, cost_usd: 0, cap_reached: false, error: '',
      });
    } catch {
      setState('error');
    }
  };

  return (
    <div className="workflow-card workflow-preview">
      <div className="workflow-card-head">
        <span className="workflow-card-icon" aria-hidden="true">⚙️</span>
        <span className="workflow-card-title">{preview.name || 'Workflow'}</span>
        <span className="workflow-card-badge">{phases.length} phase{phases.length === 1 ? '' : 's'}</span>
      </div>
      {preview.description && <div className="workflow-card-desc">{preview.description}</div>}
      {phases.length > 0 && (
        <ol className="workflow-stepper">
          {phases.map((p, i) => (
            <li key={i} className="workflow-step">{p}</li>
          ))}
        </ol>
      )}
      <button type="button" className="workflow-script-toggle" onClick={() => setShowScript((v) => !v)}>
        {showScript ? '▾ Hide script' : '▸ Show script'}
      </button>
      {showScript && <pre className="workflow-script"><code>{preview.script}</code></pre>}
      {preview.budget_usd != null && (
        <div className="workflow-card-meta">Budget cap: ${preview.budget_usd}</div>
      )}

      {state === 'idle' && (
        <div className="workflow-card-actions">
          <button type="button" className="btn btn-primary btn-sm" onClick={() => void approve()} disabled={!sessionId}>
            Approve &amp; run
          </button>
          <button type="button" className="btn btn-sm" onClick={() => setState('denied')}>Deny</button>
        </div>
      )}
      {state === 'launching' && <div className="workflow-card-meta">Launching…</div>}
      {state === 'launched' && runId && (
        <div className="workflow-card-meta workflow-ok">Approved &amp; launched.</div>
      )}
      {state === 'denied' && <div className="workflow-card-meta">Denied — not run.</div>}
      {state === 'error' && <div className="workflow-card-meta workflow-err">Failed to launch.</div>}
      {state === 'launched' && runId && <WorkflowRunCard runId={runId} name={preview.name} />}
    </div>
  );
};
