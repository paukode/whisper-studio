/**
 * Inline card for a CI watch (WS-J). Renders live from the ciStore (fed by the
 * session event stream); if the store has nothing yet (e.g. after a reload) it
 * fetches a one-shot status snapshot. When the run failed, an Autofix button
 * plans the fix and drops the approvable workflow preview into the chat.
 */
import React, { useEffect } from 'react';
import { ciStatus, getWatch, planAutofix, stopWatch } from '@/api/ci';
import { useCIStore } from '@/stores/ciStore';
import { useSessionStore } from '@/stores/sessionStore';
import { useSettingsStore } from '@/stores/settingsStore';
import { getChatStore } from '@/stores/sessionRuntimes';

const COLOR: Record<string, string> = {
  success: 'var(--accent-ok, #2e7d32)',
  completed: 'var(--accent-ok, #2e7d32)',
  failure: 'var(--accent-record)',
  timed_out: 'var(--accent-record)',
  startup_failure: 'var(--accent-record)',
  in_progress: 'var(--accent-warn, #b8860b)',
  queued: 'var(--accent-warn, #b8860b)',
  watching: 'var(--accent-warn, #b8860b)',
};

export const CIStatusCard: React.FC<{ taskId: string; branch?: string }> = ({ taskId, branch }) => {
  const watch = useCIStore((s) => s.watches[taskId]);
  const sessionId = useSessionStore((s) => s.currentSessionId);

  useEffect(() => {
    if (watch) return;
    let cancelled = false;
    const fresh = () => !cancelled && !useCIStore.getState().watches[taskId];
    // On mount/reload, re-attach to the EXACT run this watch followed (keyed by
    // task id), so a newer run pushed to the same branch can't be misattributed.
    void getWatch(taskId)
      .then((w) => {
        if (!fresh()) return;
        if (w.terminal && w.run) {
          useCIStore.getState().upsert(taskId, {
            branch: w.branch || branch || '',
            status: w.run.status || 'completed',
            conclusion: w.run.conclusion || '',
            run_id: w.run.run_id ?? null,
            url: w.run.url || '',
            failing: Boolean(w.run.failing),
            timed_out: Boolean(w.run.timed_out),
            cancelled: Boolean(w.run.cancelled),
            terminal: true,
          });
        } else {
          // Still running: seed the branch; live ci_progress ticks fill it in.
          useCIStore.getState().upsert(taskId, { branch: w.branch || branch || '', status: 'watching' });
        }
      })
      .catch(() => {
        // No task row (server restart / pruned) — fall back to the branch's
        // latest-run snapshot, which is approximate but better than nothing.
        if (!fresh() || !branch) return;
        void ciStatus(branch)
          .then((st) => {
            if (!fresh()) return;
            const r = st.run;
            useCIStore.getState().upsert(taskId, {
              branch,
              status: r?.status || 'watching',
              conclusion: r?.conclusion || '',
              run_id: r?.run_id ?? null,
              url: r?.url || '',
              jobs: r?.jobs || [],
              failing: Boolean(r?.failing),
              terminal: r?.status === 'completed',
            });
          })
          .catch(() => {});
      });
    return () => {
      cancelled = true;
    };
  }, [taskId, branch, watch]);

  const w = watch;
  const terminal = Boolean(w?.terminal);
  const label = w?.cancelled ? 'cancelled' : terminal ? w?.conclusion || 'completed' : w?.status || 'watching';
  const jobs = w?.jobs ?? [];
  const failing = Boolean(w?.failing);

  const runAutofix = async () => {
    const b = w?.branch || branch;
    if (!b || !sessionId) return;
    // Bind the OWNING session's store BEFORE the (multi-second) await so a
    // session switch during diagnosis can't land the cards in the wrong chat.
    const chat = getChatStore(sessionId).getState();
    const s = useSettingsStore.getState();
    const modelId = s.config.chatModels[s.selectedModel];
    const plan = await planAutofix({ branch: b, session_id: sessionId }).catch(() => null);
    if (!plan) return;
    if (plan.findings?.length) {
      chat.addMessage({
        role: 'assistant',
        content: '',
        timestamp: new Date().toISOString(),
        toolUse: [{
          toolId: 'ci_diagnosis',
          toolName: 'ci_diagnosis',
          input: { branch: plan.branch, run_id: plan.run_id, url: plan.url, findings: plan.findings },
          status: 'complete',
        }],
      });
    }
    if (plan.script) {
      chat.addMessage({
        role: 'assistant',
        content: '',
        timestamp: new Date().toISOString(),
        toolUse: [{
          toolId: 'workflow_preview',
          toolName: 'workflow_preview',
          input: {
            script: plan.script,
            name: 'ci-autofix',
            description: plan.summary,
            phases: [{ title: 'Fix' }, { title: 'Verify' }],
            budget_usd: plan.budget_usd ?? null,
            model_id: modelId,
          },
          status: 'complete',
        }],
      });
    }
  };

  return (
    <div className="workflow-card ci-card">
      <div className="workflow-card-head">
        <span className="workflow-card-icon" aria-hidden="true">🧪</span>
        <span className="workflow-card-title">CI · {w?.branch || branch || 'branch'}</span>
        <span className="workflow-card-badge" style={{ color: COLOR[label] ?? 'inherit' }}>{label}</span>
      </div>
      {jobs.length > 0 && (
        <ul className="ci-jobs">
          {jobs.map((j, i) => (
            <li key={i} className="ci-job">
              <span className="ci-job-dot" style={{ background: COLOR[j.conclusion || ''] ?? 'var(--text-secondary, gray)' }} />
              {j.name}
            </li>
          ))}
        </ul>
      )}
      {w?.timed_out && <div className="workflow-card-meta">Watch timed out before the run finished.</div>}
      <div className="workflow-card-actions">
        {w && !terminal && (
          <button type="button" className="btn btn-sm" onClick={() => void stopWatch(taskId)}>Stop</button>
        )}
        {failing && terminal && (
          <button type="button" className="btn btn-sm btn-primary" onClick={() => void runAutofix()}>Autofix</button>
        )}
        {w?.url && (
          <a className="btn btn-sm" href={w.url} target="_blank" rel="noreferrer">View run</a>
        )}
      </div>
    </div>
  );
};
