import { afterEach, describe, it, expect } from 'vitest';
import { groupAgentTools, groupForActivity, isActivityEntry } from './AgentCard';
import { labelCommit } from '@/hooks/chatStream/activityLabels';
import { readSSEStream } from '@/hooks/chatStream/sseStream';
import { dropRuntime, getChatStore, useRuntimeIndex } from '@/stores/sessionRuntimes';
import { iconFor } from './activityIcons';
import { callVerb, describeGroup } from './activityLabel';
import { groupChips } from './activityChips';
import type { ToolMeta, ToolUseEvent, TurnModelSettings } from '@/types/chat';

const tool = (name: string, opts: Partial<ToolUseEvent> = {}): ToolUseEvent => ({
  ...opts,
  toolId: opts.toolId ?? `${name}-${Math.random().toString(36).slice(2, 7)}`,
  toolName: name,
  input: opts.input ?? {},
  result: opts.result,
  status: opts.status ?? 'complete',
});

const meta = (m: Partial<ToolMeta>): ToolMeta => ({
  durationMs: null, outputChars: null, truncated: null, lines: null, bytes: null, added: null, removed: null, ...m,
});

/** The toolIds of each activity group, as both views draw them. */
const groupIds = (tools: ToolUseEvent[]) => groupForActivity(groupAgentTools(tools), { minRun: ACTIVITY_MIN_RUN })
  .map((e) => (isActivityEntry(e) ? e.tools.map((t) => t.toolId) : []));

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

  it('splits the calls of two model rounds into two groups, and keeps a refusal card with the call it refused', () => {
    // hook_blocked carries no round: it lands after the call it refused and
    // stays in that call's group; the next round opens a group of its own.
    const tools = [
      tool('ws_read_file', { toolId: 'tu_a', round: 0 }),
      tool('ws_run_command', { toolId: 'tu_rm', round: 0, status: 'error' }),
      tool('hook_blocked', { toolId: 'hook_blocked', status: 'error' }),
      tool('ws_grep', { toolId: 'tu_g', round: 1 }),
      tool('ws_read_file', { toolId: 'tu_b', round: 1 }),
    ];
    expect(groupIds(tools)).toEqual([['tu_a', 'tu_rm', 'hook_blocked'], ['tu_g', 'tu_b']]);
  });

  it('keeps the rows of an older transcript, which carry no round, in one group', () => {
    const tools = [tool('ws_edit_file', { toolId: 'e' }), tool('ws_run_command', { toolId: 'c' }), tool('ws_read_file', { toolId: 'r' })];
    expect(groupIds(tools)).toEqual([['e', 'c', 'r']]);
  });

  it('a card at the head of a run keeps a group of its own, and the next round draws as its own group', () => {
    // A stop_hook_block card committed after a prose row starts the run; the
    // next round's calls, committed as a tool-only row, join that run. The
    // round is not the card's, so its group does not take the card's error.
    const tools = [
      tool('stop_hook_block', { toolId: 'stop_hook_block', status: 'error', input: { reason: 'Run the tests' } }),
      tool('ws_read_file', { toolId: 'tu_x', round: 5, input: { path: 'a.ts' } }),
      tool('ws_read_file', { toolId: 'tu_y', round: 5, input: { path: 'b.ts' } }),
    ];
    expect(groupIds(tools)).toEqual([['stop_hook_block'], ['tu_x', 'tu_y']]);
    const round = describeGroup(tools.slice(1), 'Read both halves of the fold');
    expect(round.state).toBe('done');
    expect(round.label).toBe('Read both halves of the fold');
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
    // card: that would render it above the question. Two cards bracket it.
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
 * StreamingMessage (live view) and TurnSegment (committed/restored view)
 * render the same conversation moments. If their minRun values diverge,
 * Activity rows pop in or vanish the instant a stream commits: the
 * disappearing-row bug. Both must use the shared ACTIVITY_MIN_RUN; a raw
 * source scan catches anyone reintroducing a hardcoded literal. */
import { ACTIVITY_MIN_RUN } from './AgentCard';

const chatSources = import.meta.glob<string>(
  ['./StreamingMessage.tsx', './TurnSegment.tsx'],
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

  // The same frames through the real parser: the live rows a stream leaves
  // in the store, and the rows a card commits, group alike, one per round.
  const SETTINGS: TurnModelSettings = { model: 'test-model', effort_level: 'normal', verbosity: 'medium', brief_mode: false };
  const sseResponse = (frames: unknown[]) => {
    const enc = new TextEncoder();
    const body = [...frames.map((f) => `data: ${JSON.stringify(f)}\n\n`), 'data: [DONE]\n\n'].join('');
    return new Response(new ReadableStream<Uint8Array>({
      start(c) { c.enqueue(enc.encode(body)); c.close(); },
    }), { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
  };
  const call = (id: string, name: string, round: number, isError = false) => [
    { skill: name, tool_use_id: id, round },
    { skill_input: name, input: { path: `${id}.ts`, command: id }, tool_use_id: id },
    { skill_result: name, output: 'ok', tool_use_id: id, is_error: isError, exit_code: isError ? 1 : null },
  ];

  afterEach(() => {
    for (const id of useRuntimeIndex.getState().liveIds) dropRuntime(id);
  });

  it('the live and the committed rows of the same rounds make the same groups', async () => {
    const frames = [
      ...call('tu_edit', 'ws_edit_file', 0),
      ...call('tu_tsc', 'ws_run_command', 0),
      ...call('tu_ruff', 'ws_run_command', 1, true),
      ...call('tu_r1', 'ws_read_file', 2),
      ...call('tu_r2', 'ws_read_file', 2),
    ];
    const signal = new AbortController().signal;
    await readSSEStream(sseResponse(frames), 'sess-parity-live', signal, SETTINGS);
    await readSSEStream(sseResponse([...frames, { workflow_started: { run_id: 'wf_1', name: 'nightly' } }]), 'sess-parity-commit', signal, SETTINGS);

    const live = getChatStore('sess-parity-live').getState().currentStreamToolUse;
    const committed = getChatStore('sess-parity-commit').getState().messages[0].toolUse ?? [];
    expect(groupIds(live)).toEqual([['tu_edit', 'tu_tsc'], ['tu_ruff'], ['tu_r1', 'tu_r2']]);
    expect(groupIds(committed)).toEqual(groupIds(live));
  });
});

/* ── Icon coverage ───────────────────────────────────────────────────
 * A tool with no entry in TOOL_ICON renders as the generic wrench, which
 * reads as an anonymous step. The web tools carry two spellings: this app's
 * web_search / web_fetch and the CamelCase of recorded transcripts. One representative per family the catalog
 * actually emits: a sample, not a frozen catalog, so adding tools never
 * breaks this but dropping a whole family does. */
describe('tool icon coverage', () => {
  const REPRESENTATIVES = [
    'terminal_run', 'terminal_send', 'terminal_close',
    'git_add_commit', 'git_diff', 'git_push', 'git_stash', 'git_branch_list',
    'github_api', 'github_actions', 'ci_status', 'verify_change',
    'memory_write', 'memory_delete', 'rules_write',
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

describe('web tool icons', () => {
  it('gives a web_search step the magnifier and a web_fetch step the globe', () => {
    const fallback = iconFor('a_tool_no_map_knows');
    expect(iconFor('web_search')).toBe(iconFor('ws_grep'));
    expect(iconFor('web_fetch')).not.toBe(fallback);
  });

  it('keeps the CamelCase spellings resolving to the same icon', () => {
    expect(iconFor('WebSearch')).toBe(iconFor('web_search'));
    expect(iconFor('WebFetch')).toBe(iconFor('web_fetch'));
  });
});

/* ── Group labels and chips ──────────────────────────────────────────
 * A collapsed row reads as a sentence built from the calls alone: verbs in
 * first-occurrence order, a basename for one file and a count for more, the
 * command for a single command. The chips are the numbers the server
 * measured, never ones counted from a cut preview. */
describe('group label', () => {
  const read = (path: string, lines: number | null = null, status: ToolUseEvent['status'] = 'complete') =>
    tool('ws_read_file', { input: { path }, status, meta: meta({ lines }) });
  const run = (command: string, opts: Partial<ToolUseEvent> = {}) =>
    tool('ws_run_command', { input: { command }, exitCode: 0, ...opts });

  it('reads only: a count of files, the basenames as the tail, the summed line total', () => {
    const tools = [
      read('src/hooks/chatStream/sseStream.ts', 1120),
      read('src/components/chat/ActivityRow.tsx', 566),
      read('src/components/chat/turnEntries.ts', 55),
    ];
    const g = describeGroup(tools);
    expect(g.state).toBe('done');
    expect(g.label).toBe('Read 3 files');
    expect(g.tail).toBe('sseStream.ts, ActivityRow.tsx, turnEntries.ts');
    expect(groupChips(tools)).toEqual([{ kind: 'text', text: '1,741 lines', tone: 'neutral' }]);
  });

  it('omits the line total when any read has no measured count', () => {
    const tools = [read('a.ts', 10), read('b.ts')];
    expect(groupChips(tools)).toEqual([]);
  });

  it('counts two files that share a basename as two, and one file read twice as one', () => {
    const two = describeGroup([read('src/a/index.ts'), read('src/b/index.ts')]);
    expect(two.label).toBe('Read 2 files');
    expect(two.tail).toBe('a/index.ts, b/index.ts');
    const one = describeGroup([read('src/a/index.ts'), read('./src/a/index.ts')]);
    expect(one.label).toBe('Read index.ts');
    expect(one.tail).toBe('');
  });

  it('caps the tail at three names', () => {
    const g = describeGroup(['a.ts', 'b.ts', 'c.ts', 'd.ts', 'e.ts'].map((p) => read(p)));
    expect(g.tail).toBe('a.ts, b.ts, c.ts +2 more');
    expect(g.title).toContain('a.ts, b.ts, c.ts, d.ts, e.ts');
  });

  it('mixed edit, create and commands: one phrase per verb and one summed diff pill', () => {
    const tools = [
      tool('ws_edit_file', { input: { path: 'src/components/chat/ActivityRow.tsx' }, meta: meta({ added: 48, removed: 12 }) }),
      tool('ws_create_file', { input: { path: 'src/components/chat/ActivityRow.test.ts' }, meta: meta({ added: 31, removed: 0 }) }),
      run('npx tsc --noEmit'),
      run('npm test -- ActivityRow', { result: ' Test Files  3 passed (3)\n      Tests  424 passed (424)\n' }),
    ];
    const g = describeGroup(tools);
    expect(g.label).toBe('Edited ActivityRow.tsx, created ActivityRow.test.ts, ran 2 commands');
    expect(g.tail).toBe('npx tsc --noEmit, npm test -- ActivityRow');
    expect(groupChips(tools)).toEqual([
      { kind: 'diff', added: 79, removed: 12 },
      { kind: 'text', text: '424 passed', tone: 'neutral' },
    ]);
  });

  it('a search and a read name the scope and the file, and a single search counts its matches', () => {
    const tools = [
      tool('ws_grep', {
        input: { pattern: 'ws_auto_applied', path: 'server/' },
        result: 'a.py:1: x\nb.py:2: y\nc.py:3: z\nd.py:4: w\n',
      }),
      read('server/tool_executor.py'),
    ];
    const g = describeGroup(tools);
    expect(g.label).toBe('Searched server, read tool_executor.py');
    expect(g.tail).toBe('"ws_auto_applied"');
    expect(groupChips(tools)).toEqual([{ kind: 'text', text: '4 matches', tone: 'neutral' }]);
  });

  it('a single command puts the command in the label and leaves the tail empty', () => {
    const g = describeGroup([run('venv/bin/ruff check .')]);
    expect(g.label).toBe('Ran venv/bin/ruff check .');
    expect(g.tail).toBe('');
  });

  it('running: the single running command, or a count of the running calls', () => {
    const one = describeGroup([run('venv/bin/python -m pytest tests/', { status: 'running', exitCode: undefined })]);
    expect(one.state).toBe('running');
    expect(one.label).toBe('Running venv/bin/python -m pytest tests/');
    expect(one.tail).toBe('');

    const three = describeGroup([read('a.ts', null, 'running'), read('b.ts', null, 'pending'), read('c.ts', null, 'running')]);
    expect(three.label).toBe('Running 3 reads');
    expect(three.tail).toBe('a.ts, b.ts, c.ts');

    // One file call says what it does to its file; "Running chat.ts"
    // would read as executing it.
    const file = describeGroup([read('src/types/chat.ts', null, 'running')]);
    expect(file.label).toBe('Reading chat.ts');
    expect(file.tail).toBe('');
  });

  it('error: a failed command is named by its executable and first argument, with a red exit chip', () => {
    const tools = [run('venv/bin/ruff check .', { status: 'error', exitCode: 1, result: 'E501\nFound 1 error.' })];
    const g = describeGroup(tools);
    expect(g.state).toBe('error');
    expect(g.label).toBe('ruff check failed');
    expect(g.tail).toBe('venv/bin/ruff check .');
    expect(groupChips(tools)).toEqual([{ kind: 'text', text: 'exit 1', tone: 'err' }]);
  });

  it('error: a failed file call names the verb and the file', () => {
    const g = describeGroup([tool('ws_edit_file', { input: { path: 'src/types/chat.ts' }, status: 'error' })]);
    expect(g.label).toBe('Edit chat.ts failed');
  });

  it('error: a refusal card names a group it shares with the calls it refused', () => {
    // sseStream commits the tools, then the card lands as its own row; the
    // turn's bubble draws both rows' traces as one list (traceRuns), so the
    // calls and the card make one group named by the refusal.
    const entries = groupForActivity(groupAgentTools([
      read('a.ts'),
      run('rm -rf build', { status: 'error', exitCode: undefined, result: 'Blocked by hook' }),
      tool('hook_blocked', { input: { tool_name: 'ws_run_command', reason: 'rm is not allowed' }, status: 'error' }),
    ]));
    expect(entries).toHaveLength(1);
    const group = entries[0];
    if (!isActivityEntry(group)) throw new Error('expected one activity group');
    const g = describeGroup(group.tools);
    expect(g.state).toBe('error');
    expect(g.label).toBe('Blocked by hook');
    expect(g.tail).toBe('a.ts, rm -rf build');

    for (const [card, label] of [['security_blocked', 'Blocked (security)'], ['plan_blocked', 'Blocked in plan mode']]) {
      const mixed = describeGroup([run('git push', { status: 'error', exitCode: undefined }), tool(card, { input: { reason: 'no' }, status: 'error' })]);
      expect(mixed.label).toBe(label);
    }
  });

  it('stopped: what landed, then ", stopped", "Stopped by you" as the tail and a not-run chip', () => {
    const tools = [
      read('src/types/chat.ts'),
      read('src/types/schemas/chat.schema.ts'),
      tool('ws_read_file', { input: { path: 'src/hooks/chatStream/deliveries.ts' }, status: 'stopped', result: '[Stopped by user]' }),
    ];
    const g = describeGroup(tools);
    expect(g.state).toBe('stopped');
    expect(g.label).toBe('Read 2 files, stopped');
    expect(g.tail).toBe('Stopped by you');
    expect(groupChips(tools)).toEqual([{ kind: 'text', text: '1 not run', tone: 'muted' }]);
  });

  it('a written label takes the label; the deterministic sentence becomes the tail and the title', () => {
    const tools = [
      tool('ws_edit_file', { input: { path: 'ActivityRow.tsx' } }),
      run('npx tsc --noEmit'),
    ];
    const g = describeGroup(tools, 'Named each activity group; tests green');
    expect(g.label).toBe('Named each activity group; tests green');
    expect(g.tail).toBe('Edited ActivityRow.tsx, ran npx tsc --noEmit');
    expect(g.title).toBe(g.tail);
  });

  it('a failed group keeps its failure as the label; a written label becomes the tail', () => {
    const failed = [run('venv/bin/ruff check .', { status: 'error', exitCode: 1, result: 'E501\nFound 1 error.' })];
    const g = describeGroup(failed, 'Checked the new rows for lint');
    expect(g.state).toBe('error');
    expect(g.label).toBe('ruff check failed');
    expect(g.tail).toBe('Checked the new rows for lint');
    // The title starts with the failure and keeps the command, which the
    // collapsed row no longer shows.
    expect(g.title).toBe('ruff check failed\nvenv/bin/ruff check .');
    // Every other state: the written label takes the label, as before.
    const others: ToolUseEvent[][] = [
      [run('npm test', { status: 'running', exitCode: undefined })],
      [read('a.ts'), tool('ws_read_file', { input: { path: 'b.ts' }, status: 'stopped', result: '[Stopped by user]' })],
      [read('a.ts')],
    ];
    for (const tools of others) {
      const other = describeGroup(tools, 'Checked the new rows for lint');
      expect(other.state).not.toBe('error');
      expect(other.label).toBe('Checked the new rows for lint');
      expect(other.tail).toBe(other.sentence);
    }
  });

  it('a streamed caption is the written label of the group that holds its calls, with the deterministic tail', () => {
    // The caption names the round's second call; the committed view reads it
    // under the group's first call, the way ChatMessage looks it up.
    const edit = tool('ws_edit_file', { toolId: 'tu_edit', input: { path: 'ActivityRow.tsx' } });
    const check = run('npx tsc --noEmit', { toolId: 'tu_tsc' });
    const pending = [{ ids: ['tu_tsc'], label: 'Named each activity group; tests green' }];
    const message = labelCommit({ role: 'assistant', content: '', timestamp: 't', toolUse: [edit, check] }, [], pending);
    const [group] = groupForActivity(groupAgentTools(message.toolUse ?? []), { minRun: ACTIVITY_MIN_RUN })
      .filter(isActivityEntry);
    const g = describeGroup(group.tools, message.activityLabels?.[group.tools[0].toolId]);
    expect(g.label).toBe('Named each activity group; tests green');
    expect(g.tail).toBe('Edited ActivityRow.tsx, ran npx tsc --noEmit');
    expect(pending).toEqual([]);
  });

  it('without a caption the deterministic sentence stays the label', () => {
    const edit = tool('ws_edit_file', { toolId: 'tu_edit', input: { path: 'ActivityRow.tsx' } });
    const message = labelCommit({ role: 'assistant', content: '', timestamp: 't', toolUse: [edit] }, [], []);
    expect(message.activityLabels).toBeUndefined();
    expect(describeGroup([edit], message.activityLabels?.tu_edit).label).toBe('Edited ActivityRow.tsx');
  });

  it('a synthetic goal card keeps its verdict label', () => {
    const goal = (verdict: string, status: ToolUseEvent['status']) =>
      tool('goal_eval', { input: { verdict, feedback: 'All tests pass.' }, status });
    const achieved = describeGroup([goal('achieved', 'complete')]);
    expect(achieved.state).toBe('done');
    expect(achieved.label).toBe('Goal check: achieved');
    expect(achieved.tail).toBe('All tests pass.');
    expect(describeGroup([goal('not_checked', 'complete')])).toMatchObject({ state: 'unchecked', label: 'Goal check: not checked' });
    expect(describeGroup([goal('blocked', 'error')])).toMatchObject({ state: 'error', label: 'Goal check: blocked' });
  });

  it('a call without a subject gets the verb alone, never a placeholder', () => {
    const g = describeGroup([tool('ws_edit_file')]);
    expect(g.label).toBe('Edited');
    expect(g.tail).toBe('');
    expect(`${g.label} ${g.tail}`).not.toContain('(no path)');
  });

  it('names git and memory calls by what they did, and anything else by its name', () => {
    const g = describeGroup([
      tool('git_status'),
      tool('git_add_commit', { input: { message: 'Fix the row' } }),
      tool('memory_write', { input: { name: 'release notes style' } }),
      tool('lsp_diagnostics'),
    ]);
    expect(g.label).toBe('Ran git status, committed, remembered release notes style, ran lsp diagnostics');
    const memory = describeGroup([
      tool('memory_read', { input: { filename: 'user_role.md' } }),
      tool('memory_delete', { input: { filename: 'old_note.md' } }),
    ]);
    expect(memory.label).toBe('Recalled user_role.md, forgot old_note.md');
  });

  it('says a rules_write saved a rule, in the file its target names', () => {
    const one = tool('rules_write', { input: { target: 'global_rules', content: 'Never use em dashes.' } });
    expect(describeGroup([one]).label).toBe('Saved global rules');
    expect(callVerb(one)).toBe('Saved');
    const two = describeGroup([
      one,
      tool('rules_write', { input: { target: 'project_whisper_md', content: 'Build with npm.' } }),
    ]);
    expect(two.label).toBe('Saved 2 rules');
    expect(iconFor('rules_write')).toBe(iconFor('skill_list'));
  });

  it('never writes an em dash', () => {
    const tools = [
      read('a.ts', 3),
      run('npm test', { status: 'error', exitCode: 2 }),
      tool('stop_hook_block', { input: { reason: 'tests are red' }, status: 'error' }),
    ];
    for (const g of [describeGroup(tools), describeGroup(tools.slice(0, 1)), describeGroup(tools.slice(2))]) {
      expect(`${g.label} ${g.tail} ${g.title ?? ''}`).not.toMatch(/\u2014/);
    }
  });
});
