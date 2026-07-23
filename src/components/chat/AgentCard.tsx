/**
 * AgentCard — inline grouping of consecutive agent-orchestration tool calls.
 *
 * Replaces a vertical stack of standalone `<details>` skill-trace chips
 * (spawn_agent, send_message×N, list_agents, team_results) with a single
 * collapsible card. Header shows "🤖 Agent · <task> — N/M steps · status".
 * Body, when expanded, lists every step with its own collapsible output.
 *
 * Industry pattern parity: Cursor / Aider / Continue / Claude Code all
 * group sub-agent activity into one card with a step count. The user
 * keeps full transparency (one click to expand) without each tool call
 * shouting for attention in the conversation flow.
 *
 * Pure-frontend grouping — no backend changes. We detect group boundaries
 * by `toolName ∈ AGENT_GROUP_TOOLS`. Non-agent tools fall through to the
 * existing skill-trace renderer, so a turn that mixes file edits with
 * agent calls still reads naturally.
 */

import type { ToolUseEvent } from '@/types/chat';

/**
 * Tool names whose consecutive runs collapse into one AgentCard. The full
 * AGENT_TOOLS set on the backend includes utility tools (config_get,
 * skill_invoke, etc.) that aren't agent-orchestration — keep those out
 * of the grouping so they continue to render as standalone chips.
 *
 * `team_results` is the synthetic SSE event that lands the orchestrator's
 * final aggregated output; it belongs in the card, not as its own bubble.
 */
export const AGENT_GROUP_TOOLS = new Set<string>([
  'spawn_agent',
  'send_message',
  'list_agents',
  'team_create',
  'team_delete',
  'team_results',
]);

export function isAgentGroupTool(name: string): boolean {
  return AGENT_GROUP_TOOLS.has(name);
}

/** Partition a flat tool list into either single tools or consecutive
 *  agent-tool groups, preserving original order. Used by both
 *  ChatMessage (completed messages) and StreamingMessage (in-flight). */
export type GroupedTool = ToolUseEvent | ToolUseEvent[];

export function groupAgentTools(tools: ToolUseEvent[]): GroupedTool[] {
  const out: GroupedTool[] = [];
  let group: ToolUseEvent[] = [];
  for (const t of tools) {
    if (isAgentGroupTool(t.toolName)) {
      group.push(t);
    } else {
      if (group.length > 0) {
        out.push(group);
        group = [];
      }
      out.push(t);
    }
  }
  if (group.length > 0) out.push(group);
  return out;
}

/** Tool names that should never collapse into an ActivityRow because
 *  they have their own dedicated chrome (workspace folder picker,
 *  ask_user_question, emitted program). The list is small on purpose —
 *  most tools are "internal" reads/writes/searches that benefit from
 *  collapsing into a single Activity strip. */
export const ACTIVITY_EXEMPT_TOOLS: ReadonlySet<string> = new Set([
  'ws_workspace_prompt',
  'ask_user_question',
  'create_artifact',
  'ws_open_folder',
  // Renders an actual image (PreviewScreenshotCard) — collapsing it into the
  // terse Activity strip would hide the one thing the tool exists to show.
  'preview_screenshot',
  // Workflow runtime cards (WS-D) have their own interactive UI.
  'workflow_preview',
  'workflow_started',
  // CI watch + autofix cards (WS-J) render their own status/findings UI.
  'ci_started',
  'ci_diagnosis',
]);

/** Human-friendly label for a tool name in trace chips. Falls back to the
 *  raw name for tools without a nicer label. */
const TOOL_LABELS: Record<string, string> = {
  create_artifact: 'Artifact',
};
export function friendlyToolName(name: string): string {
  return TOOL_LABELS[name] ?? name;
}

/**
 * Second-pass grouping: walk the output of ``groupAgentTools`` and bundle
 * runs of "individual" tool calls into ``{kind:'activity'}`` entries so the
 * conversation collapses N reads + M greps into one expandable row.
 *
 * Special tools (workspace picker, ask-user, emitted program) pass through
 * untouched because they have their own interactive UI. ``minRun`` controls
 * how many consecutive calls are needed before the bundle kicks in:
 *   - Completed messages (ChatMessage): minRun = 2 — a single isolated call
 *     keeps its standalone <details> chip rather than paying the chrome
 *     cost of an "Activity · 1" wrapper.
 *   - Streaming messages (StreamingMessage): minRun = 1 — the collapsed row
 *     appears from tool #1 and counts up as more arrive, instead of a flash
 *     of individual cards that get bundled the moment the stream commits.
 */
export type ActivityEntry = { kind: 'activity'; tools: ToolUseEvent[] };

/** Task-management tool calls collapse into a TaskCard — the inline,
 *  live-updating task list rendered in the conversation (the floating
 *  Tasks panel was retired in its favour). */
export const TASK_CARD_TOOLS: ReadonlySet<string> = new Set([
  'task_create',
  'task_update',
  'task_list',
  'task_get',
  'task_stop',
]);

export type TasksEntry = { kind: 'tasks'; tools: ToolUseEvent[] };

export function isTasksEntry(e: unknown): e is TasksEntry {
  return typeof e === 'object' && e !== null && !Array.isArray(e) && (e as { kind?: string }).kind === 'tasks';
}

/** Discriminating type guard so callers can narrow a
 *  ``GroupedTool | ActivityEntry`` union to one branch. */
export function isActivityEntry(e: unknown): e is ActivityEntry {
  return typeof e === 'object' && e !== null && !Array.isArray(e) && (e as { kind?: string }).kind === 'activity';
}

/** The minRun both the streaming view (StreamingMessage) and the committed
 *  view (ChatMessage) must pass to groupForActivity. They render the same
 *  conversation moments — if these ever diverge, Activity rows pop in or
 *  vanish the instant a stream commits. Guarded by ActivityRow.test.ts. */
export const ACTIVITY_MIN_RUN = 1;

export function groupForActivity(
  entries: GroupedTool[],
  opts: { minRun?: number } = {},
): Array<GroupedTool | ActivityEntry | TasksEntry> {
  const minRun = opts.minRun ?? 2;
  const out: Array<GroupedTool | ActivityEntry | TasksEntry> = [];
  let pending: ToolUseEvent[] = [];
  // Task_* calls collapse into ONE tasks entry that updates in place: it is
  // created at the first task call and holds `taskRun` by reference, so later
  // task calls fold into the same card. This kills the per-file-write
  // fragmentation that duplicated the list and left an early card's task
  // spinning forever (its completion landed in a separate, later card).
  //
  // An order-significant interruption — an agent group, or an exempt
  // interactive card (ask_user_question / create_artifact / …) — is a phase
  // boundary: we reset `taskRun` to null there so any task calls AFTER it start
  // a fresh card at their correct later position, rather than folding back up
  // above the interrupting card. Plain activity (reads/writes/greps) is NOT a
  // boundary, so the common case still consolidates into one card.
  let taskRun: ToolUseEvent[] | null = null;

  const flushPending = () => {
    if (pending.length === 0) return;
    if (pending.length >= minRun) {
      out.push({ kind: 'activity', tools: pending });
    } else {
      // Below threshold — push each tool through as-is so its original
      // render path applies (single <details> chip).
      for (const t of pending) out.push(t);
    }
    pending = [];
  };

  for (const entry of entries) {
    if (Array.isArray(entry)) {
      // Agent group — pass through; flush any pending activity run first.
      // It's a phase boundary, so later task calls start a fresh card below it.
      flushPending();
      taskRun = null;
      out.push(entry);
      continue;
    }
    // entry is a single ToolUseEvent at this point.
    if (TASK_CARD_TOOLS.has(entry.toolName)) {
      flushPending();
      if (taskRun === null) {
        // First task call fixes the card's position; the array is mutated in
        // place as later task calls arrive.
        taskRun = [];
        out.push({ kind: 'tasks', tools: taskRun });
      }
      taskRun.push(entry);
      continue;
    }
    if (ACTIVITY_EXEMPT_TOOLS.has(entry.toolName)) {
      // Exempt interactive card (question / artifact / picker) — a phase
      // boundary too, so trailing task calls don't bury it.
      flushPending();
      taskRun = null;
      out.push(entry);
      continue;
    }
    pending.push(entry);
  }
  flushPending();
  return out;
}

// ── Render helpers ─────────────────────────────────────────────────────

function stepIcon(name: string): string {
  switch (name) {
    case 'spawn_agent': return '\u{1F680}'; // 🚀
    case 'send_message': return '✉️'; // ✉
    case 'list_agents': return '\u{1F4CB}'; // 📋
    case 'team_create': return '\u{1F465}'; // 👥
    case 'team_delete': return '\u{1F5D1}️'; // 🗑
    case 'team_results': return '\u{1F4E5}'; // 📥
    default: return '\u{1F527}'; // 🔧
  }
}

function statusGlyph(status: ToolUseEvent['status']) {
  if (status === 'complete') return <span className="trace-check">{'✓'}</span>;
  if (status === 'error') return <span className="trace-check" style={{ color: 'var(--error, #f87171)' }}>{'✕'}</span>;
  return <span className="trace-spinner">{'⟳'}</span>;
}

/** Pull the human-readable subject from a group's most informative tool —
 *  prefer team_create.team_name, fall back to spawn_agent.task, then the
 *  first agent's task in a team, then the literal "Agent". */
function deriveAgentTitle(tools: ToolUseEvent[]): string {
  for (const t of tools) {
    const input = (t.input ?? {}) as Record<string, unknown>;
    if (t.toolName === 'team_create') {
      const name = (input.team_name as string | undefined) ?? '';
      if (name.trim()) return name.trim();
    }
    if (t.toolName === 'spawn_agent') {
      const task = (input.task as string | undefined) ?? '';
      if (task.trim()) {
        // Trim long tasks so the header stays one line.
        const oneLine = task.split('\n')[0].trim();
        return oneLine.length > 70 ? oneLine.slice(0, 67) + '…' : oneLine;
      }
    }
  }
  return 'Agent activity';
}

function summariseStepInput(tool: ToolUseEvent): string | null {
  const input = (tool.input ?? {}) as Record<string, unknown>;
  if (tool.toolName === 'spawn_agent') {
    const task = (input.task as string | undefined) ?? '';
    const type = (input.agent_type as string | undefined) ?? 'general';
    return task ? `[${type}] ${task.split('\n')[0].slice(0, 80)}` : `[${type}]`;
  }
  if (tool.toolName === 'send_message') {
    const target = (input.to_agent_id as string | undefined) ?? (input.broadcast ? 'all agents' : '?');
    const content = ((input.content as string | undefined) ?? '').split('\n')[0];
    return content ? `→ ${target}: ${content.slice(0, 80)}` : `→ ${target}`;
  }
  if (tool.toolName === 'team_create') {
    const agents = (input.agents as Array<{ name?: string }> | undefined) ?? [];
    return `${agents.length} agents`;
  }
  return null;
}

// ── Components ────────────────────────────────────────────────────────

interface AgentStepProps {
  tool: ToolUseEvent;
}

function AgentStep({ tool }: AgentStepProps) {
  // team_results is the high-value payload — render its body inline,
  // not behind another `<details>`, so the user sees the agent's final
  // answer immediately when the card is expanded.
  if (tool.toolName === 'team_results') {
    const body = formatTeamResults(tool.input);
    return (
      <div className="agent-step agent-step-result">
        <div className="agent-step-header">
          <span className="agent-step-icon">{stepIcon(tool.toolName)}</span>
          <span className="agent-step-name">Result</span>
          {statusGlyph(tool.status)}
        </div>
        {body && <pre className="agent-step-output">{body}</pre>}
      </div>
    );
  }

  const subject = summariseStepInput(tool);
  return (
    <details className={`agent-step ${tool.status === 'complete' ? 'done' : tool.status}`}>
      <summary className="agent-step-header">
        <span className="agent-step-icon">{stepIcon(tool.toolName)}</span>
        <span className="agent-step-name">
          {tool.toolName}
          {subject && <span className="agent-step-subject"> · {subject}</span>}
        </span>
        {statusGlyph(tool.status)}
      </summary>
      {tool.result && (
        <pre className="agent-step-output">
          {tool.result.length > 4000 ? tool.result.slice(0, 4000) + '…' : tool.result}
        </pre>
      )}
    </details>
  );
}

function formatTeamResults(input: Record<string, unknown> | unknown): string {
  if (!input || typeof input !== 'object') return '';
  // team_results payload shape varies — handle the common cases:
  //   { results: [{ agent_id, name, output, ... }] }
  //   { output: string }
  //   <anything else>: pretty-print as JSON.
  const obj = input as Record<string, unknown>;
  if (typeof obj.output === 'string') return obj.output;
  if (Array.isArray(obj.results)) {
    return obj.results.map((r: unknown) => {
      const rr = (r ?? {}) as Record<string, unknown>;
      const name = (rr.name as string | undefined) ?? (rr.agent_id as string | undefined) ?? 'agent';
      const out = (rr.output as string | undefined) ?? '';
      return `── ${name} ──\n${out}`;
    }).join('\n\n');
  }
  try {
    return JSON.stringify(obj, null, 2);
  } catch {
    return String(obj);
  }
}

export interface AgentCardProps {
  tools: ToolUseEvent[];
}

/**
 * Single bordered card grouping a run of agent-orchestration tool calls.
 * Open while any step is still running; collapses by default once all
 * steps have a terminal status. Step count + overall status are visible
 * in the header so users can tell at a glance what the agent did
 * without expanding.
 */
export function AgentCard({ tools }: AgentCardProps) {
  const running = tools.filter(t => t.status === 'pending' || t.status === 'running').length;
  const errored = tools.filter(t => t.status === 'error').length;
  const done = tools.filter(t => t.status === 'complete').length;
  const total = tools.length;
  const allDone = running === 0;

  const title = deriveAgentTitle(tools);
  const statusLabel = running > 0
    ? `${done}/${total} steps · running`
    : errored > 0
      ? `${done}/${total} steps · ${errored} error${errored === 1 ? '' : 's'}`
      : `${done}/${total} steps · done`;

  return (
    <details className={`agent-card ${allDone ? (errored > 0 ? 'errored' : 'done') : 'running'}`} open={!allDone}>
      <summary className="agent-card-header">
        <span className="agent-card-icon" aria-hidden="true">{'\u{1F916}'}</span>
        <span className="agent-card-title">Agent · {title}</span>
        <span className="agent-card-status">
          {statusLabel}{' '}
          {running > 0 ? (
            <span className="trace-spinner">{'⟳'}</span>
          ) : errored > 0 ? (
            <span className="trace-check" style={{ color: 'var(--error, #f87171)' }}>{'✕'}</span>
          ) : (
            <span className="trace-check">{'✓'}</span>
          )}
        </span>
      </summary>
      <div className="agent-card-body">
        {tools.map((tool, idx) => (
          <AgentStep key={`${tool.toolId}-${idx}`} tool={tool} />
        ))}
      </div>
    </details>
  );
}
