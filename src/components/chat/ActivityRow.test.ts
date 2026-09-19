import { describe, it, expect } from 'vitest';
import { groupAgentTools, groupForActivity, isActivityEntry } from './AgentCard';
import type { ToolUseEvent } from '@/types/chat';

const tool = (name: string, opts: Partial<ToolUseEvent> = {}): ToolUseEvent => ({
  toolId: opts.toolId ?? `${name}-${Math.random().toString(36).slice(2, 7)}`,
  toolName: name,
  input: opts.input ?? {},
  result: opts.result,
  status: opts.status ?? 'complete',
});

describe('groupForActivity', () => {
  it('bundles 2+ consecutive individual tools into one activity entry', () => {
    const tools = [
      tool('ws_read_file'),
      tool('ws_read_file'),
      tool('ws_grep'),
      tool('ws_run_command'),
    ];
    const grouped = groupForActivity(groupAgentTools(tools));
    expect(grouped).toHaveLength(1);
    expect(isActivityEntry(grouped[0])).toBe(true);
    if (isActivityEntry(grouped[0])) {
      expect(grouped[0].tools).toHaveLength(4);
    }
  });

  it('leaves a single isolated tool call as an individual entry (minRun 2)', () => {
    const grouped = groupForActivity(groupAgentTools([tool('ws_read_file')]));
    expect(grouped).toHaveLength(1);
    expect(isActivityEntry(grouped[0])).toBe(false);
    expect(Array.isArray(grouped[0])).toBe(false);
  });

  it('bundles even a single tool when minRun is 1 (streaming view)', () => {
    const grouped = groupForActivity(groupAgentTools([tool('ws_read_file')]), { minRun: 1 });
    expect(grouped).toHaveLength(1);
    expect(isActivityEntry(grouped[0])).toBe(true);
  });

  it('splits around agent-orchestration groups', () => {
    const tools = [
      tool('ws_read_file'),
      tool('ws_grep'),
      tool('spawn_agent'),
      tool('send_message'),
      tool('ws_read_file'),
      tool('ws_read_file'),
    ];
    const grouped = groupForActivity(groupAgentTools(tools));
    // activity[2 reads/greps] · agent-array · activity[2 reads]
    expect(grouped).toHaveLength(3);
    expect(isActivityEntry(grouped[0])).toBe(true);
    expect(Array.isArray(grouped[1])).toBe(true);
    expect(isActivityEntry(grouped[2])).toBe(true);
  });

  it('keeps activity-exempt tools (workspace picker, ask-user) standalone', () => {
    const tools = [
      tool('ws_read_file'),
      tool('ws_workspace_prompt'),
      tool('ask_user_question'),
      tool('ws_read_file'),
      tool('ws_grep'),
    ];
    const grouped = groupForActivity(groupAgentTools(tools));
    // read(lone, minRun2 → standalone) · prompt · ask · activity[read+grep]
    expect(isActivityEntry(grouped[0])).toBe(false); // lone read
    expect((grouped[1] as ToolUseEvent).toolName).toBe('ws_workspace_prompt');
    expect((grouped[2] as ToolUseEvent).toolName).toBe('ask_user_question');
    expect(isActivityEntry(grouped[grouped.length - 1])).toBe(true);
  });
});

/* ── Task-card grouping ──────────────────────────────────────────────
 * Runs of task_* calls collapse into a {kind:'tasks'} entry (rendered as
 * the inline TaskCard) instead of joining the generic Activity row. */
import { isTasksEntry } from './AgentCard';

describe('task-card grouping', () => {
  it('bundles consecutive task calls into a tasks entry, even a single one', () => {
    const grouped = groupForActivity(groupAgentTools([tool('task_create')]));
    expect(grouped).toHaveLength(1);
    expect(isTasksEntry(grouped[0])).toBe(true);
  });

  it('consolidates ALL task calls into one entry even when activity interleaves', () => {
    const grouped = groupForActivity(groupAgentTools([
      tool('task_create'),
      tool('task_update'),
      tool('ws_read_file'),
      tool('ws_grep'),
      tool('task_update'),
    ]));
    // ONE tasks entry (at the first task position, holding all 3 task calls)
    // · activity[read+grep]. The trailing task_update folds into the same card
    // rather than spawning a second one.
    expect(grouped.filter(isTasksEntry)).toHaveLength(1);
    expect(isTasksEntry(grouped[0])).toBe(true);
    expect(isActivityEntry(grouped[1])).toBe(true);
    expect(grouped).toHaveLength(2);
    if (isTasksEntry(grouped[0])) expect(grouped[0].tools).toHaveLength(3);
  });

  it('brackets task phases around an exempt interactive card (chronological order)', () => {
    // A task_update AFTER ask_user_question must NOT fold up into the earlier
    // card — that would render it above the question. Two cards bracket it.
    const grouped = groupForActivity(groupAgentTools([
      tool('task_create'),
      tool('ask_user_question'),
      tool('task_update'),
    ]), { minRun: 1 });
    expect(grouped).toHaveLength(3);
    expect(isTasksEntry(grouped[0])).toBe(true);
    expect((grouped[1] as ToolUseEvent).toolName).toBe('ask_user_question');
    expect(isTasksEntry(grouped[2])).toBe(true);
  });

  it('brackets task phases around an agent group (chronological order)', () => {
    const grouped = groupForActivity(groupAgentTools([
      tool('task_update'),
      tool('spawn_agent'),
      tool('send_message'),
      tool('task_update'),
    ]), { minRun: 1 });
    // tasks · agent-array · tasks
    expect(grouped).toHaveLength(3);
    expect(isTasksEntry(grouped[0])).toBe(true);
    expect(Array.isArray(grouped[1])).toBe(true);
    expect(isTasksEntry(grouped[2])).toBe(true);
  });

  it('regression: interleaved create→write→update→write→update is one live list', () => {
    // The exact shape that produced duplicate cards + a stuck spinner: a task
    // marked done in a later card while an earlier snapshot card kept spinning.
    const grouped = groupForActivity(groupAgentTools([
      tool('task_create'), tool('task_create'), tool('task_create'),
      tool('ws_create_file'),
      tool('task_update'),
      tool('ws_create_file'),
      tool('task_update'),
    ]), { minRun: 1 });
    const taskEntries = grouped.filter(isTasksEntry);
    expect(taskEntries).toHaveLength(1);                 // one card, not one per run
    if (isTasksEntry(taskEntries[0])) {
      expect(taskEntries[0].tools).toHaveLength(5);       // every task call lands in it
    }
  });
});

/* ── Streaming/committed grouping parity ─────────────────────────────
 * StreamingMessage (live view) and ChatMessage (committed/restored view)
 * render the same conversation moments. If their minRun values diverge,
 * Activity rows pop in or vanish the instant a stream commits — the
 * disappearing-row bug. Both must use the shared ACTIVITY_MIN_RUN; a raw
 * source scan catches anyone reintroducing a hardcoded literal. */
import { ACTIVITY_MIN_RUN } from './AgentCard';

const chatSources = import.meta.glob<string>(
  ['./StreamingMessage.tsx', './ChatMessage.tsx'],
  { query: '?raw', import: 'default', eager: true },
);

describe('streaming/committed grouping parity', () => {
  it('both views pass the shared ACTIVITY_MIN_RUN to groupForActivity', () => {
    const files = Object.entries(chatSources);
    expect(files).toHaveLength(2);
    for (const [file, src] of files) {
      expect(src, `${file} must use the shared constant`).toContain(
        'minRun: ACTIVITY_MIN_RUN',
      );
      expect(src, `${file} must not hardcode a minRun literal`).not.toMatch(
        /minRun:\s*\d/,
      );
    }
  });

  it('a single streamed tool stays an Activity row after commit', () => {
    const live = groupForActivity(groupAgentTools([tool('ws_read_file')]), { minRun: ACTIVITY_MIN_RUN });
    const committed = groupForActivity(groupAgentTools([tool('ws_read_file')]), { minRun: ACTIVITY_MIN_RUN });
    expect(isActivityEntry(live[0])).toBe(isActivityEntry(committed[0]));
    expect(isActivityEntry(live[0])).toBe(true);
  });
});

/* ── Web tool naming ─────────────────────────────────────────────────
 * The Activity row keyed its web entries off CamelCase names this app
 * never emits, so a real web_search / web_fetch step rendered with the
 * generic wrench and an empty summary — the user saw an unlabelled step
 * instead of the query or URL. Both spellings must resolve. */
import { iconFor, summariseTool } from './ActivityRow';

describe('web tool icons and summaries', () => {
  it('gives a web_search step the magnifier, not the wrench fallback', () => {
    const fallback = iconFor('a_tool_no_map_knows');
    expect(iconFor('web_search')).not.toBe(fallback);
    // Same glyph as the other search-shaped tool, not merely "something".
    expect(iconFor('web_search')).toBe(iconFor('ws_grep'));
  });

  it('summarises a web_search step with its query', () => {
    const step = tool('web_search', { input: { query: 'vite config proxy websocket' } });
    expect(summariseTool(step)).toBe('vite config proxy websocket');
  });

  it('gives a web_fetch step the globe and summarises its URL', () => {
    expect(iconFor('web_fetch')).not.toBe(iconFor('a_tool_no_map_knows'));
    const step = tool('web_fetch', { input: { url: 'https://example.com/docs/page' } });
    expect(summariseTool(step)).toBe('https://example.com/docs/page');
  });

  it('keeps the CamelCase spellings resolving to the same icon and summary', () => {
    expect(iconFor('WebSearch')).toBe(iconFor('web_search'));
    expect(iconFor('WebFetch')).toBe(iconFor('web_fetch'));
    expect(summariseTool(tool('WebSearch', { input: { query: 'still labelled' } })))
      .toBe('still labelled');
  });
});

/* ── Icon coverage ───────────────────────────────────────────────────
 * A tool with no entry in TOOL_ICON renders as the generic wrench, which
 * reads as an anonymous step. One representative per family the catalog
 * actually emits — a sample, not a frozen catalog, so adding tools never
 * breaks this but dropping a whole family does. */
describe('tool icon coverage', () => {
  const REPRESENTATIVES = [
    'terminal_run', 'terminal_send', 'terminal_close',
    'git_add_commit', 'git_diff', 'git_push', 'git_stash', 'git_branch_list',
    'github_api', 'github_actions', 'ci_status', 'verify_change',
    'memory_write', 'memory_delete',
    'preview_navigate', 'preview_click', 'preview_logs', 'preview_screenshot',
    'cron_run', 'cron_update',
    'task_get', 'task_status', 'task_output', 'task_cancel',
    'lsp_diagnostics', 'lsp_hover', 'lsp_references',
    'skill_invoke', 'skill_list', 'workflow_run', 'workflow_status',
    'create_docx', 'create_xlsx', 'create_pptx', 'create_chart', 'create_plan',
    'create_artifact', 'save_file', 'analyze_document', 'summarize_transcript',
    'spawn_agent', 'send_message', 'notify_user', 'list_agents',
    'aws_cli', 'config_set', 'list_mcp_resources', 'sleep',
    'session_search', 'list_sessions', 'notebook_edit', 'run_tool_script',
    'ws_create_worktree', 'ws_diff_worktree', 'workspace_graph_query',
    'web_search', 'web_fetch',
  ];

  it('never renders a tool the app emits as the anonymous wrench', () => {
    const fallback = iconFor('a_tool_no_map_knows');
    const unmapped = REPRESENTATIVES.filter((name) => iconFor(name) === fallback);
    expect(unmapped, `fell through to the wrench: ${unmapped.join(', ')}`).toEqual([]);
  });

  it('still falls back to the wrench for a genuinely unknown tool', () => {
    expect(iconFor('some_tool_from_the_future')).toBe(iconFor('another_unknown'));
  });
});

/* ── Terminal summaries ──────────────────────────────────────────────
 * ws_run_command showed its command while this app's own terminal tools
 * showed nothing; they read their line from different input keys. */
describe('terminal step summaries', () => {
  it('summarises terminal_run with its command', () => {
    const step = tool('terminal_run', { input: { command: 'npm run build' } });
    expect(summariseTool(step)).toBe('npm run build');
  });

  it('summarises terminal_send with the line it typed', () => {
    const step = tool('terminal_send', { input: { input: 'print(len(rows))' } });
    expect(summariseTool(step)).toBe('print(len(rows))');
  });
});
