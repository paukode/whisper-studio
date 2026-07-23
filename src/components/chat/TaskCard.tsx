import React, { useMemo } from 'react';
import type { ToolUseEvent } from '@/types/chat';
import { useTaskStore, taskProgress, type SessionTask } from '@/stores/taskStore';
import { useSessionStore } from '@/stores/sessionStore';
import { useDockStore } from '@/stores/dockStore';

/**
 * TaskCard — a compact, clickable "Tasks N/M" row in the conversation that
 * opens the standalone Tasks drawer (the full plan lives there).
 *
 * Rendering is driven by the conversation, not by each message in isolation:
 * `computeTaskCheckpoints` walks the message list and emits a row only when the
 * cumulative task state (done/total/active) CHANGED since the previous
 * task-bearing message, so a multi-turn plan shows a checkpoint at each change
 * instead of repeating the same "N/M done" row on every later turn. The owning
 * message passes that cumulative snapshot in via `tasks`.
 *
 * When rendered without `tasks` (the live streaming turn), the row reads the
 * session task store, falling back to this message's own task tool calls before
 * the store is hydrated, so the live row never lags.
 */

export interface TaskCardProps {
  /** This message's own task tool calls — used for the live streaming row. */
  tools?: ToolUseEvent[];
  /** Precomputed cumulative checkpoint to display (committed rows). When set,
   *  it is authoritative and the store/fallback are not consulted. */
  tasks?: SessionTask[];
}

/** Fallback: derive the cumulative task list from a message's task tool calls
 *  (used only until the session store is populated from todo_update / hydrate). */
function deriveTasks(tools: ToolUseEvent[]): SessionTask[] {
  const map = new Map<string, SessionTask>();
  let placeholderSeq = 0;

  const upsert = (task: { id?: unknown; subject?: unknown; status?: unknown }, fallbackId?: string) => {
    const id = String(task.id ?? fallbackId ?? `pending-${placeholderSeq++}`);
    const prev = map.get(id);
    map.set(id, {
      id,
      subject: typeof task.subject === 'string' && task.subject ? task.subject : prev?.subject ?? '(task)',
      status: typeof task.status === 'string' && task.status ? task.status : prev?.status ?? 'pending',
    });
  };

  for (const t of tools) {
    let parsed: Record<string, unknown> | null = null;
    if (t.result) {
      try { parsed = JSON.parse(t.result) as Record<string, unknown>; } catch { parsed = null; }
    }
    const input = (t.input ?? {}) as Record<string, unknown>;
    const resultTask = parsed?.task as Record<string, unknown> | undefined;

    switch (t.toolName) {
      case 'task_create':
        if (resultTask) upsert(resultTask, String(parsed?.task_id ?? ''));
        else upsert({ subject: input.subject, status: 'pending' });
        break;
      case 'task_update':
        if (resultTask) upsert(resultTask);
        else if (input.task_id) upsert({ id: input.task_id, status: input.status });
        break;
      case 'task_get':
        if (resultTask) upsert(resultTask);
        break;
      case 'task_list': {
        const list = parsed?.tasks;
        if (Array.isArray(list)) {
          for (const task of list) upsert(task as Record<string, unknown>);
        }
        break;
      }
      case 'task_stop':
        if (input.task_id) map.delete(String(input.task_id));
        break;
    }
  }

  return [...map.values()].filter((t) => t.status !== 'deleted');
}

/** True for any task tracker tool call (task_create/update/get/list/stop). */
function isTaskTool(t: ToolUseEvent): boolean {
  return typeof t.toolName === 'string' && t.toolName.startsWith('task_');
}

/** Change-detection key for a task list: completed/total plus whether anything
 *  is in progress. Two lists with the same signature look identical in the row. */
function taskSignature(tasks: SessionTask[]): string {
  const { done, total, active } = taskProgress(tasks);
  return `${done}/${total}/${active ? 'a' : ''}`;
}

/**
 * Decide which messages show a Tasks row and the cumulative list each one shows.
 * Walks the conversation once, accumulating task tool calls in order, and emits
 * an entry for a message only when the cumulative signature differs from the
 * previous task-bearing message — collapsing the "3/3 all done" row that would
 * otherwise repeat on every later turn. Returns message index -> cumulative tasks.
 */
export function computeTaskCheckpoints(
  messages: ReadonlyArray<{ toolUse?: ToolUseEvent[] }>,
): Map<number, SessionTask[]> {
  const result = new Map<number, SessionTask[]>();
  const acc: ToolUseEvent[] = [];
  let prevSig = '';
  messages.forEach((m, i) => {
    const taskTools = (m.toolUse ?? []).filter(isTaskTool);
    if (taskTools.length === 0) return;
    acc.push(...taskTools);
    const cumulative = deriveTasks(acc);
    const sig = taskSignature(cumulative);
    if (cumulative.length > 0 && sig !== prevSig) result.set(i, cumulative);
    prevSig = sig;
  });
  return result;
}

export const TaskCard: React.FC<TaskCardProps> = ({ tools, tasks: tasksProp }) => {
  const currentSessionId = useSessionStore((s) => s.currentSessionId);
  const storeTasks = useTaskStore((s) => (currentSessionId ? s.tasksBySession[currentSessionId] : undefined));
  const openPanel = useDockStore((s) => s.openPanel);

  const fallback = useMemo(() => deriveTasks(tools ?? []), [tools]);
  // A committed row passes its cumulative checkpoint in `tasks` (authoritative).
  // The live streaming row has no `tasks`, so it reads the session store, only
  // falling back to this message's own tool calls before the store is hydrated.
  const tasks = tasksProp ?? (storeTasks !== undefined ? storeTasks : fallback);
  if (tasks.length === 0) return null;

  const { done, total, active } = taskProgress(tasks);
  const pct = total ? Math.round((done / total) * 100) : 0;
  const allDone = total > 0 && done === total;
  const stateText = allDone ? 'all done' : active ? 'in progress' : 'pending';

  return (
    <button
      type="button"
      className={`task-row${active ? ' active' : ''}${allDone ? ' done' : ''}`}
      onClick={() => openPanel({ id: 'tasks', kind: 'tasks', title: 'Tasks' })}
      title="Open the tasks panel"
      aria-label={`Tasks: ${done} of ${total} done, ${stateText}. Open the tasks panel.`}
    >
      <svg className="task-row-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        <polyline points="9 11 12 14 22 4" />
        <path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11" />
      </svg>
      <span className="task-row-label">Tasks</span>
      <span className="task-row-count">{done}/{total}</span>
      <span className="task-row-bar" aria-hidden="true"><span className="task-row-bar-fill" style={{ width: `${pct}%` }} /></span>
      {active && <span className="task-row-status">in progress</span>}
      {allDone && <span className="task-row-status done">all done</span>}
      <span className="task-row-open" aria-hidden="true">
        Open
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="9 18 15 12 9 6" /></svg>
      </span>
    </button>
  );
};
