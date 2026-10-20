import React, { useEffect, useRef, useState } from 'react';
import { LINGER_MS, lingerUntil, useBackgroundTaskStore } from '@/stores/backgroundTaskStore';
import { useDockStore } from '@/stores/dockStore';
import type { BackgroundTaskInfo } from '@/types/backgroundTasks';
import { CheckIcon, ChevronIcon, CloseIcon } from '@/components/common/icons';
import { useNow } from '@/hooks/useNow';
import { formatMessageTimestamp } from '@/utils/formatTimestamp';

/** How often an unfolded running row refreshes its output tail. */
const OUTPUT_POLL_MS = 2000;

/**
 * The list under the header's tasks pill: every background task across all
 * sessions (shell commands, agents, workflow runs, CI watches), running
 * first. One row each, with a chevron that unfolds its details, Open for an
 * agent (the dock's agent pane) and Stop while it runs. A finished row stays
 * LINGER_MS with its exit code and a countdown line, or leaves on its x.
 * Closes on ESC (preventDefault so the chat-stream kill switch doesn't also
 * fire), on a click outside, or on the pill itself.
 */
export const BackgroundTasksPanel: React.FC = () => {
  const panelOpen = useBackgroundTaskStore((s) => s.panelOpen);
  const setPanelOpen = useBackgroundTaskStore((s) => s.setPanelOpen);
  const tasks = useBackgroundTaskStore((s) => s.tasks);
  const hydrate = useBackgroundTaskStore((s) => s.hydrate);
  const ref = useRef<HTMLDivElement>(null);
  const now = useNow(1000, panelOpen);

  useEffect(() => {
    if (panelOpen) void hydrate();
  }, [panelOpen, hydrate]);

  useEffect(() => {
    if (!panelOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        e.stopPropagation();
        setPanelOpen(false);
      }
    };
    const onPointer = (e: MouseEvent) => {
      const target = e.target as Node | null;
      if (!target) return;
      if (ref.current?.contains(target)) return;
      if (document.getElementById('bgTasksPill')?.contains(target)) return;
      setPanelOpen(false);
    };
    // Capture phase so this wins over the app-level ESC kill switch.
    window.addEventListener('keydown', onKey, true);
    document.addEventListener('mousedown', onPointer);
    return () => {
      window.removeEventListener('keydown', onKey, true);
      document.removeEventListener('mousedown', onPointer);
    };
  }, [panelOpen, setPanelOpen]);

  if (!panelOpen) return null;

  const rows = Object.values(tasks).sort(byRunningThenNewest);

  return (
    <div className="bg-tasks-panel" role="dialog" aria-label="Background tasks" ref={ref}>
      <div className="bg-tasks-head">Background tasks</div>
      <div className="bg-tasks-body">
        {rows.length === 0 && <div className="bg-tasks-empty">Nothing running.</div>}
        {rows.map((t) => (
          <TaskRow key={t.task_id} task={t} now={now} />
        ))}
      </div>
    </div>
  );
};

const TaskRow: React.FC<{ task: BackgroundTaskInfo; now: number }> = ({ task, now }) => {
  const expanded = useBackgroundTaskStore((s) => !!s.expanded[task.task_id]);
  const toggleExpanded = useBackgroundTaskStore((s) => s.toggleExpanded);
  const stopTask = useBackgroundTaskStore((s) => s.stopTask);
  const dismiss = useBackgroundTaskStore((s) => s.dismiss);
  const openAgent = useDockStore((s) => s.openAgent);
  const label = taskLabel(task);
  const running = task.status === 'running';
  const leaveAt = running ? null : lingerUntil(task);
  const secondsLeft = leaveAt === null ? 0 : Math.max(0, Math.ceil((leaveAt - now) / 1000));
  const lingerPct = leaveAt === null ? 0 : Math.max(0, Math.min(100, ((leaveAt - now) / LINGER_MS) * 100));

  return (
    <>
      <div className={`bg-tasks-row${running ? '' : ' bg-tasks-row-finished'}`} data-testid="bg-task-row">
        <button
          type="button"
          className="bg-tasks-chevron"
          onClick={() => toggleExpanded(task.task_id)}
          aria-expanded={expanded}
          aria-label={expanded ? 'Hide details' : 'Show details'}
        >
          <ChevronIcon direction={expanded ? 'down' : 'right'} />
        </button>
        <span className="bg-tasks-kind">{task.kind}</span>
        <span
          className={`bg-tasks-row-title${task.kind === 'shell' ? ' mono' : ''}`}
          title={label === task.title ? task.title : `${label}\n${task.title}`}
        >
          {label}
        </span>
        <span className="bg-tasks-status">
          {!running && task.status === 'completed' && <CheckIcon />}
          {statusText(task, now)}
          {!running && ` · clears in ${secondsLeft}s`}
        </span>
        {task.kind === 'agent' && (
          <button type="button" className="bg-tasks-btn" onClick={() => openAgent(task.task_id, label)}>
            Open
          </button>
        )}
        {running ? (
          <button
            type="button"
            className="bg-tasks-btn bg-tasks-stop"
            onClick={() => void stopTask(task.task_id)}
            title="Stop this task"
          >
            Stop
          </button>
        ) : (
          <button
            type="button"
            className="bg-tasks-dismiss"
            onClick={() => dismiss(task.task_id)}
            aria-label="Dismiss"
            title="Remove from the list"
          >
            <CloseIcon />
          </button>
        )}
      </div>
      {expanded && <TaskDetail task={task} />}
      {!running && (
        <div className="bg-tasks-linger" aria-hidden="true">
          <div className="bg-tasks-linger-fill" style={{ width: `${lingerPct}%` }} />
        </div>
      )}
    </>
  );
};

/** Unfolded row: where and when it ran, and its output. A running task's
 *  output is polled from its file; a finished one shows the result it ended
 *  with (an agent's is its report). */
const TaskDetail: React.FC<{ task: BackgroundTaskInfo }> = ({ task }) => {
  const running = task.status === 'running';
  const liveTail = useOutputTail(task.task_id, running && task.kind !== 'agent');
  const tail = running ? liveTail : (task.result_text ?? '');
  const cwd = typeof task.meta?.cwd === 'string' ? task.meta.cwd : '';
  const model = typeof task.meta?.model === 'string' ? task.meta.model : '';
  return (
    <div className="bg-tasks-detail">
      <div className="bg-tasks-detail-line">
        {cwd && (
          <>
            cwd <span className="mono">{homeRelative(cwd)}</span> ·{' '}
          </>
        )}
        started {formatMessageTimestamp(task.created_at)}
        {task.kind === 'shell' && task.pid ? ` · pid ${task.pid}` : ''}
        {model ? ` · ${model}` : ''}
      </div>
      {task.kind !== 'shell' && <div className="bg-tasks-detail-task">{task.title}</div>}
      {tail && <pre className="bg-tasks-tail">{tail}</pre>}
    </div>
  );
};

function useOutputTail(taskId: string, active: boolean): string {
  const [text, setText] = useState('');
  useEffect(() => {
    if (!active) return;
    let alive = true;
    const load = async () => {
      try {
        const r = await fetch(`/api/background-tasks/${encodeURIComponent(taskId)}/output`);
        if (!r.ok) return;
        const data = (await r.json()) as { text: string };
        if (alive) setText(data.text);
      } catch {
        // The next poll tries again.
      }
    };
    void load();
    const id = setInterval(() => void load(), OUTPUT_POLL_MS);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [taskId, active]);
  return text;
}

/** What a row is called: a named agent's name (a workflow label, a team
 *  member), so agents that share a brief stay apart; otherwise the title. The
 *  title, the start of the task, stays in the tooltip and the details. */
export function taskLabel(t: BackgroundTaskInfo): string {
  const name = t.kind === 'agent' ? t.meta?.agent_name : undefined;
  return typeof name === 'string' && name ? name : t.title;
}

function byRunningThenNewest(a: BackgroundTaskInfo, b: BackgroundTaskInfo): number {
  const ar = a.status === 'running' ? 0 : 1;
  const br = b.status === 'running' ? 0 : 1;
  if (ar !== br) return ar - br;
  return (b.created_at || '').localeCompare(a.created_at || '');
}

/** "running · 1.4s" while it runs; afterwards the outcome and how long it took. */
export function statusText(t: BackgroundTaskInfo, now: number): string {
  const started = Date.parse(t.created_at);
  const ended = t.finished_at ? Date.parse(t.finished_at) : now;
  const took = Number.isFinite(started) && Number.isFinite(ended) ? formatDuration(Math.max(0, ended - started)) : '';
  if (t.status === 'running') return took ? `running · ${took}` : 'running';
  const exit = typeof t.exit_code === 'number' ? `exit ${t.exit_code}` : '';
  const outcome =
    t.status === 'completed' ? exit || 'done'
    : t.status === 'failed' ? ['failed', exit].filter(Boolean).join(' · ')
    : t.status;
  return took ? `${outcome} · ${took}` : outcome;
}

/** "820ms" / "8.2s" / "1m 04s". Rounds before it splits, so 59.6 s of a
 *  minute carries into the next one instead of reading "45m 60s". */
export function formatDuration(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  if (Math.round(ms / 100) < 600) return `${(ms / 1000).toFixed(1)}s`;
  const total = Math.round(ms / 1000);
  return `${Math.floor(total / 60)}m ${String(total % 60).padStart(2, '0')}s`;
}

/** A path under the user's home folder shown as "~/..." (the app is macOS only). */
export function homeRelative(path: string): string {
  return path.replace(/^\/Users\/[^/]+(?=\/|$)/, '~');
}
