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
  budget_tokens?: number | null;
  args?: unknown;
  model_id?: string;
  /** The session's effort at the moment the model proposed this run. Replayed
   *  on approval so clicking Approve does not quietly reason shallower than an
   *  auto-approved launch would have. */
  effort_label?: string;
  /** The resolved root every agent this run spawns will be pinned to, frozen
   *  at the moment the script was proposed. Shown so the user approves the
   *  actual folder, and replayed on approval unchanged — a workspace switch
   *  during the approval delay must not silently redirect an already-shown
   *  run to a different repo. */
  workspace_path?: string;
}

function phaseTitle(p: unknown): string {
  if (typeof p === 'string') return p;
  if (p && typeof p === 'object' && 'title' in p) return String((p as { title: unknown }).title);
  return String(p);
}

// A preview message persists in chat history, but the approve state is
// component-local — so a page reload would reset it to 'idle' and let the SAME
// script be launched twice. We remember launched (previewKey -> runId) in
// localStorage so a reloaded preview restores its launched run instead of
// offering Approve again.
const LAUNCHED_KEY = 'whisper.wf-launched';

function hashString(s: string): string {
  let h = 5381;
  for (let i = 0; i < s.length; i++) h = ((h << 5) + h + s.charCodeAt(i)) | 0;
  return (h >>> 0).toString(36);
}

/** Identity of a preview = everything that changes what the run will DO.
 *
 *  Hashing the script alone made two genuinely different launches collide:
 *  re-running the same saved script against another workspace, model, effort,
 *  budget, or args would show as "already launched" and refuse a legitimate
 *  second run — or, worse, surface an unrelated older run's card in its
 *  place. Session is included too, so the same script proposed in two chats
 *  tracks separately. */
function previewKey(preview: Preview, sessionId: string | null): string {
  return hashString(
    JSON.stringify([
      preview.script,
      sessionId ?? '',
      preview.model_id ?? '',
      preview.effort_label ?? '',
      preview.workspace_path ?? '',
      preview.budget_tokens ?? null,
      // args is arbitrary JSON; stringify is stable enough for identity here
      // because it comes back verbatim from the same server payload.
      JSON.stringify(preview.args ?? null),
    ]),
  );
}
function readLaunched(): Record<string, string> {
  try {
    return JSON.parse(localStorage.getItem(LAUNCHED_KEY) || '{}') as Record<string, string>;
  } catch {
    return {};
  }
}
function rememberLaunched(key: string, runId: string): void {
  try {
    const m = readLaunched();
    m[key] = runId;
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
  const launchKey = previewKey(preview, sessionId);
  const priorRun = readLaunched()[launchKey] ?? null;
  const [state, setState] = useState<'idle' | 'launching' | 'launched' | 'denied' | 'error'>(
    priorRun ? 'launched' : 'idle',
  );
  const [runId, setRunId] = useState<string | null>(priorRun);
  const [showScript, setShowScript] = useState(false);
  const [alwaysAllow, setAlwaysAllow] = useState(false);
  const [trustedAs, setTrustedAs] = useState<string | null>(null);
  const phases = (preview.phases ?? []).map(phaseTitle);

  const approve = async () => {
    if (!sessionId) return;
    setState('launching');
    try {
      const r = await launchRun({
        script: preview.script,
        session_id: sessionId,
        args: preview.args,
        budget_tokens: preview.budget_tokens ?? null,
        model_id: preview.model_id,
        effort_label: preview.effort_label || undefined,
        // `??`, not `||`: an empty string here means the preview genuinely
        // had nothing to pin (shown with no "Workspace:" line) and that must
        // reach the server AS an empty string, not collapse to a dropped key
        // — a dropped key tells the server "no card was involved, snapshot
        // whatever is connected now", which would let a workspace connected
        // during the approval delay silently become this run's root even
        // though the card never showed the user a folder at all.
        workspace_path: preview.workspace_path ?? undefined,
        trust: alwaysAllow,
      });
      setTrustedAs(r.trusted_as ?? null);
      setRunId(r.run_id);
      setState('launched');
      rememberLaunched(launchKey, r.run_id);
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
      {preview.workspace_path && (
        <div className="workflow-card-meta" title={preview.workspace_path}>
          Workspace: <code>{preview.workspace_path}</code>
        </div>
      )}
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
      {preview.budget_tokens != null && (
        <div className="workflow-card-meta">Budget cap: {preview.budget_tokens.toLocaleString()} output tokens</div>
      )}

      {state === 'idle' && (
        <>
          <label className="workflow-card-meta" style={{ display: 'flex', alignItems: 'center', gap: 6, cursor: 'pointer' }}>
            <input
              type="checkbox"
              checked={alwaysAllow}
              onChange={(e) => setAlwaysAllow(e.target.checked)}
            />
            Always allow this workflow (saves it as trusted; approving without this covers this session only)
          </label>
          <div className="workflow-card-actions">
            <button type="button" className="btn btn-primary btn-sm" onClick={() => void approve()} disabled={!sessionId}>
              Approve &amp; run
            </button>
            <button type="button" className="btn btn-sm" onClick={() => setState('denied')}>Deny</button>
          </div>
        </>
      )}
      {state === 'launching' && <div className="workflow-card-meta">Launching…</div>}
      {state === 'launched' && runId && (
        <div className="workflow-card-meta workflow-ok">
          Approved &amp; launched.
          {trustedAs ? ` Saved as trusted workflow '${trustedAs}'.` : ''}
        </div>
      )}
      {state === 'denied' && <div className="workflow-card-meta">Denied — not run.</div>}
      {state === 'error' && <div className="workflow-card-meta workflow-err">Failed to launch.</div>}
      {state === 'launched' && runId && <WorkflowRunCard runId={runId} name={preview.name} />}
    </div>
  );
};
