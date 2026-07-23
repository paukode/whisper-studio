import React, { useMemo, useState } from 'react';
import type { ToolUseEvent } from '@/types/chat';

/**
 * ActivityRow — collapsed strip of consecutive tool calls within an
 * assistant turn, rendered inline in the conversation pane.
 *
 * Replaces the old "one <details> per tool call" rendering which made a
 * turn that read 8 files + ran 3 commands look like 11 separate cards.
 * Now those 11 calls show up as:
 *
 *     ▸ ACTIVITY · 11 steps · ✓ 11
 *
 * Click the header to expand into one-line summaries per step (filename,
 * grep match count, command excerpt, …). Each step has its own ▾ toggle
 * that reveals that step's raw output in-place — everything stays inside
 * the chat column; there is no side pane.
 *
 * UX rules:
 *   - Default collapsed.
 *   - If any step errored, the row opens by default so failures aren't
 *     silently hidden behind a chevron.
 *   - Per-tool one-liners are derived from input + result without
 *     re-running anything.
 */

export interface ActivityRowProps {
  tools: ToolUseEvent[];
}

/** Compact stroke icons (emoji read as chat content, not chrome — the
 *  premium pass replaces them with the same SVG language used everywhere
 *  else in the app). */
const icon = (children: React.ReactNode) => (
  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    {children}
  </svg>
);

const ICON = {
  file: icon(<><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" /><polyline points="14 2 14 8 20 8" /></>),
  pencil: icon(<path d="M17 3a2.8 2.8 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5z" />),
  plus: icon(<><line x1="12" y1="5" x2="12" y2="19" /><line x1="5" y1="12" x2="19" y2="12" /></>),
  x: icon(<><line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" /></>),
  search: icon(<><circle cx="11" cy="11" r="7" /><line x1="21" y1="21" x2="16.5" y2="16.5" /></>),
  layers: icon(<><polygon points="12 2 2 7 12 12 22 7 12 2" /><polyline points="2 17 12 22 22 17" /><polyline points="2 12 12 17 22 12" /></>),
  folder: icon(<path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" />),
  terminal: icon(<><polyline points="4 17 10 11 4 5" /><line x1="12" y1="19" x2="20" y2="19" /></>),
  code: icon(<><polyline points="16 18 22 12 16 6" /><polyline points="8 6 2 12 8 18" /></>),
  checkSquare: icon(<><polyline points="9 11 12 14 22 4" /><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11" /></>),
  refresh: icon(<><path d="M21 12a9 9 0 1 1-2.6-6.4" /><path d="M21 3v6h-6" /></>),
  list: icon(<><line x1="8" y1="6" x2="21" y2="6" /><line x1="8" y1="12" x2="21" y2="12" /><line x1="8" y1="18" x2="21" y2="18" /><line x1="3" y1="6" x2="3.01" y2="6" /><line x1="3" y1="12" x2="3.01" y2="12" /><line x1="3" y1="18" x2="3.01" y2="18" /></>),
  globe: icon(<><circle cx="12" cy="12" r="9" /><path d="M3 12h18M12 3c2.5 2.6 3.9 5.7 3.9 9s-1.4 6.4-3.9 9c-2.5-2.6-3.9-5.7-3.9-9S9.5 5.6 12 3z" /></>),
  clock: icon(<><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3.5 2" /></>),
  wrench: icon(<path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z" />),
};

const TOOL_ICON: Record<string, React.ReactNode> = {
  ws_read_file: ICON.file,
  Read: ICON.file,
  ws_write_file: ICON.pencil,
  Write: ICON.pencil,
  ws_edit_file: ICON.pencil,
  Edit: ICON.pencil,
  ws_create_file: ICON.plus,
  ws_delete_file: ICON.x,
  ws_grep: ICON.search,
  Grep: ICON.search,
  ws_glob: ICON.layers,
  Glob: ICON.layers,
  ws_list_dir: ICON.folder,
  ws_list_directory: ICON.folder,
  LS: ICON.folder,
  ws_run_command: ICON.terminal,
  Bash: ICON.terminal,
  run_python: ICON.code,
  task_create: ICON.checkSquare,
  task_update: ICON.refresh,
  task_list: ICON.list,
  task_stop: ICON.x,
  TodoWrite: ICON.pencil,
  WebFetch: ICON.globe,
  WebSearch: ICON.search,
  cron_create: ICON.clock,
  cron_delete: ICON.clock,
  cron_list: ICON.clock,
  ws_open_folder: ICON.folder,
};

function iconFor(name: string): React.ReactNode {
  return TOOL_ICON[name] ?? ICON.wrench;
}

/** Trim long inputs to a compact preview. */
function preview(s: string, max = 60): string {
  const collapsed = s.replace(/\s+/g, ' ').trim();
  return collapsed.length > max ? collapsed.slice(0, max - 1) + '…' : collapsed;
}

/** Try to extract a match/line count from a tool's text output. Returns
 *  null when nothing's parseable so the caller falls back to a generic
 *  summary. */
function countFromResult(toolName: string, result?: string): string | null {
  if (!result) return null;
  if (toolName === 'ws_grep' || toolName === 'Grep') {
    const lines = result.split('\n').filter((l) => l.trim().length > 0);
    if (lines.length > 0) return `${lines.length} match${lines.length === 1 ? '' : 'es'}`;
  }
  if (toolName === 'ws_glob' || toolName === 'Glob') {
    const lines = result.split('\n').filter((l) => l.trim().length > 0);
    if (lines.length > 0) return `${lines.length} file${lines.length === 1 ? '' : 's'}`;
  }
  if (toolName === 'ws_read_file' || toolName === 'Read') {
    const lines = result.split('\n').length;
    if (lines > 0) return `${lines} line${lines === 1 ? '' : 's'}`;
  }
  if (toolName === 'ws_list_dir' || toolName === 'ws_list_directory' || toolName === 'LS') {
    const lines = result.split('\n').filter((l) => l.trim().length > 0).length;
    return `${lines} item${lines === 1 ? '' : 's'}`;
  }
  return null;
}

/** Friendly labels for the synthetic hook/security cards. */
const DISPLAY_NAMES: Record<string, string> = {
  security_blocked: 'Blocked (security)',
  hook_blocked: 'Blocked by hook',
  stop_hook_feedback: 'Continuing (Stop hook)',
  stop_hook_block: 'Continuing (Stop hook)',
  goal_eval: 'Goal check',
  goal_cap_reached: 'Goal check paused (cap reached)',
};

function displayName(name: string): string {
  return DISPLAY_NAMES[name] ?? name;
}

/** Build a one-line description of what this tool call did. */
function summariseTool(tool: ToolUseEvent): string {
  const input = (tool.input ?? {}) as Record<string, unknown>;
  const name = tool.toolName;

  if (name === 'hook_blocked' || name === 'stop_hook_feedback' || name === 'stop_hook_block' || name === 'security_blocked') {
    const reason = (input.reason ?? '') as string;
    const toolName = (input.tool_name ?? '') as string;
    if (name === 'hook_blocked' && toolName) return `${toolName}: ${preview(reason, 80)}`;
    return preview(reason, 90);
  }
  if (name === 'goal_eval') {
    const verdict = (input.verdict ?? '') as string;
    const feedback = (input.feedback ?? '') as string;
    const attempt = input.attempt as number | undefined;
    const cap = input.cap as number | undefined;
    const n = attempt && cap ? ` (${attempt}/${cap})` : '';
    return `${verdict}${n}${feedback ? ` — ${preview(feedback, 70)}` : ''}`;
  }
  const path = (input.path ?? input.file_path ?? input.filepath) as string | undefined;
  const pattern = (input.pattern ?? input.query) as string | undefined;

  if (name === 'ws_run_command' || name === 'Bash') {
    const cmd = (input.command ?? input.cmd ?? '') as string;
    return preview(cmd, 80);
  }
  if (name === 'run_python') {
    const code = (input.code ?? '') as string;
    return preview(code, 80);
  }
  if (name === 'ws_read_file' || name === 'Read' || name === 'ws_write_file' || name === 'Write' || name === 'ws_edit_file' || name === 'Edit' || name === 'ws_create_file' || name === 'ws_delete_file') {
    return path ? path.split('/').slice(-2).join('/') : '(no path)';
  }
  if (name === 'ws_grep' || name === 'Grep') {
    const inPath = path ? ` in ${path.split('/').pop()}` : '';
    return pattern ? `"${preview(pattern, 40)}"${inPath}` : (path ?? '(no pattern)');
  }
  if (name === 'ws_glob' || name === 'Glob' || name === 'ws_list_dir' || name === 'ws_list_directory' || name === 'LS') {
    return preview((input.pattern ?? input.path ?? '') as string, 60);
  }
  if (name === 'task_create' || name === 'task_update' || name === 'TodoWrite') {
    const subject = (input.subject ?? input.title ?? '') as string;
    const status = (input.status ?? '') as string;
    if (subject) return status ? `${subject} → ${status}` : subject;
    return status || '(task)';
  }
  if (name === 'WebFetch' || name === 'WebSearch') {
    return preview(((input.url ?? input.query ?? '') as string), 70);
  }
  if (name === 'cron_create') {
    return preview(((input.name ?? '') as string), 50);
  }
  return '';
}

/** Aggregate status for the collapsed header. */
function aggregateStatus(tools: ToolUseEvent[]): 'running' | 'error' | 'ok' {
  if (tools.some((t) => t.status === 'error')) return 'error';
  if (tools.some((t) => t.status === 'running' || t.status === 'pending')) return 'running';
  return 'ok';
}

export const ActivityRow: React.FC<ActivityRowProps> = ({ tools }) => {
  const agg = aggregateStatus(tools);
  const hasError = agg === 'error';
  // Open by default if anything errored — never hide failures behind a
  // chevron. Otherwise the user explicitly clicks to expand.
  const [open, setOpen] = useState<boolean>(hasError);
  // Set of expanded step keys — each step toggles independently.
  const [expandedKeys, setExpandedKeys] = useState<Set<string>>(() => new Set());

  // Stable per-step keys. Index-prefixed on purpose: streamed tools are
  // added with toolId = tool *name* (sseStream), so two ws_run_command
  // calls share an id — keying on that alone made one click expand every
  // matching step. The index guarantees uniqueness.
  const stepKeys = useMemo(
    () => tools.map((t, i) => `${i}-${t.toolId || t.toolName}`),
    [tools],
  );
  // Steps that actually have output get an expand toggle.
  const expandableKeys = useMemo(
    () => stepKeys.filter((_, i) => Boolean(tools[i].result)),
    [stepKeys, tools],
  );
  const allExpanded =
    expandableKeys.length > 0 && expandableKeys.every((k) => expandedKeys.has(k));

  const toggleStep = (key: string) => {
    setExpandedKeys((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };
  const toggleAll = () => {
    setExpandedKeys(allExpanded ? new Set() : new Set(expandableKeys));
  };

  const counts = useMemo(() => {
    const total = tools.length;
    const done = tools.filter((t) => t.status === 'complete').length;
    const errored = tools.filter((t) => t.status === 'error').length;
    const running = tools.filter((t) => t.status === 'running' || t.status === 'pending').length;
    return { total, done, errored, running };
  }, [tools]);

  return (
    <div className={`activity-row activity-${agg}${open ? ' open' : ''}`}>
      <div className="activity-header">
        <button
          type="button"
          className="activity-summary"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
        >
          <span className="activity-chevron" aria-hidden="true">{open ? '▾' : '▸'}</span>
          <span className="activity-label">Activity</span>
          <span className="activity-meta">
            {counts.total} step{counts.total === 1 ? '' : 's'}
          </span>
          {counts.running > 0 && (
            <span className="activity-badge running" title="Running">◐ {counts.running}</span>
          )}
          {counts.errored > 0 && (
            <span className="activity-badge error" title="Errored">⚠ {counts.errored}</span>
          )}
          {counts.errored === 0 && counts.running === 0 && (
            <span className="activity-badge ok" title="All complete">✓ {counts.done}</span>
          )}
        </button>
        {open && expandableKeys.length > 0 && (
          <button
            type="button"
            className="activity-expand-all"
            onClick={toggleAll}
            title={allExpanded ? 'Collapse all outputs' : 'Expand all outputs'}
          >
            {allExpanded ? 'Collapse all' : 'Expand all'}
          </button>
        )}
      </div>
      {open && (
        <ul className="activity-steps">
          {tools.map((tool, idx) => {
            const key = stepKeys[idx];
            const summary = summariseTool(tool);
            const count = countFromResult(tool.toolName, tool.result);
            const detail = [summary, count].filter(Boolean).join(' · ');
            const isError = tool.status === 'error';
            const isRunning = tool.status === 'running' || tool.status === 'pending';
            const result = tool.result ?? '';
            const isExpanded = expandedKeys.has(key);
            return (
              <li
                key={key}
                className={`activity-step status-${tool.status}${isError ? ' errored' : ''}`}
              >
                <span className="activity-step-icon" aria-hidden="true">
                  {iconFor(tool.toolName)}
                </span>
                <span className="activity-step-name">{displayName(tool.toolName)}</span>
                {detail && <span className="activity-step-detail">{detail}</span>}
                <span className="activity-step-spacer" />
                <span className="activity-step-status">
                  {isRunning && <span className="activity-spinner" aria-label="running">◐</span>}
                  {tool.status === 'complete' && <span className="activity-check">✓</span>}
                  {isError && <span className="activity-x">⚠</span>}
                </span>
                {result && (
                  <button
                    type="button"
                    className="activity-expand-btn"
                    onClick={(e) => { e.stopPropagation(); toggleStep(key); }}
                    aria-expanded={isExpanded}
                    title={isExpanded ? 'Hide output' : 'Show output'}
                  >
                    {isExpanded ? '▴' : '▾'}
                  </button>
                )}
                {isExpanded && (
                  <pre className="activity-step-output">{result}</pre>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
};
