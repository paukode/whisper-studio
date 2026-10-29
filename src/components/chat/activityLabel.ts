import type { ToolUseEvent } from '@/types/chat';
import { friendlyToolName } from '@/components/chat/AgentCard';

/**
 * The words on an activity row, computed from the ToolUseEvent rows alone:
 * the deterministic group label and its tail, and the verb and subject of
 * each call's detail line. Pure functions; ActivityRow.tsx lays the results
 * out and activityChips.ts adds the numbers. A written label
 * (ChatMessage.activityLabels) replaces the sentence on the row, and the
 * sentence then becomes the tail, except on a failed group: the failure
 * stays the label and the written label becomes the tail.
 */

export type GroupState = 'running' | 'error' | 'stopped' | 'unchecked' | 'done';

export type CallKind = 'file' | 'command' | 'search' | 'list' | 'fetch' | 'git' | 'memory' | 'other' | 'card';

/** One call, reduced to the words the row needs. */
export interface CallDesc {
  kind: CallKind;
  /** Past tense, as the label phrases it: "read", "edited", "ran", "checked out". */
  verb: string;
  /** The base form for a failure label: "Edit chat.ts failed". */
  base: string;
  /** What one of these is called when a running group counts them. */
  noun: string;
  nounPlural: string;
  /** The short subject for the label and the tail: a file basename, a
   *  command head, a quoted pattern, a host, a branch. */
  subject: string | null;
  /** What the detail line prints when it has no path or command of its own:
   *  a card's reason, a commit message, a tool's one-line summary. */
  line: string;
  /** A file or listing's input path, for the link. */
  path: string | null;
  /** A command's whole line. */
  command: string | null;
  /** A search's scope as the input named it, and its basename for the label. */
  scopePath: string | null;
  scope: string | null;
}

export interface GroupDescription {
  state: GroupState;
  label: string;
  tail: string;
  /** The deterministic sentence, whether or not a written label replaced it. */
  sentence: string;
  /** The title attribute: the deterministic sentence and the whole tail,
   *  which the row may ellipsise. Under a written label that took the
   *  sentence's place, the sentence alone. */
  title?: string;
}

const MAX_LABEL_COMMAND = 60;
const MAX_TAIL_ITEMS = 3;

// ── Tool vocabulary ───────────────────────────────────────────────────

const FILE_VERBS: Record<string, [verb: string, base: string]> = {
  ws_read_file: ['read', 'read'],
  Read: ['read', 'read'],
  notebook_read: ['read', 'read'],
  read_artifact: ['read', 'read'],
  analyze_document: ['read', 'read'],
  inspect_document: ['read', 'read'],
  ws_edit_file: ['edited', 'edit'],
  Edit: ['edited', 'edit'],
  notebook_edit: ['edited', 'edit'],
  edit_artifact: ['edited', 'edit'],
  ws_create_file: ['created', 'create'],
  create_docx: ['created', 'create'],
  create_pdf: ['created', 'create'],
  create_pptx: ['created', 'create'],
  create_xlsx: ['created', 'create'],
  create_chart: ['created', 'create'],
  create_visual: ['created', 'create'],
  create_artifact: ['created', 'create'],
  create_program: ['created', 'create'],
  save_file: ['created', 'create'],
  ws_write_file: ['wrote', 'write'],
  Write: ['wrote', 'write'],
  ws_delete_file: ['deleted', 'delete'],
};

const SEARCH_TOOLS = new Set([
  'ws_grep', 'Grep', 'ws_glob', 'Glob', 'workspace_semantic_search', 'workspace_graph_query',
  'tool_search', 'session_search', 'web_search', 'WebSearch',
]);
const LIST_TOOLS = new Set(['ws_list_directory', 'LS']);
const COMMAND_TOOLS = new Set([
  'ws_run_command', 'Bash', 'terminal_run', 'terminal_send', 'run_python', 'office_script', 'run_tool_script',
]);
const FETCH_TOOLS = new Set(['web_fetch', 'WebFetch']);

/** Git tools that change something, with the input key that names the
 *  subject when one does. */
const GIT_VERBS: Record<string, [verb: string, base: string, subjectKey: string | null]> = {
  git_add_commit: ['committed', 'commit', null],
  git_push: ['pushed', 'push', null],
  git_push_pr: ['pushed', 'push', null],
  git_pull: ['pulled', 'pull', null],
  git_fetch: ['fetched', 'fetch', null],
  git_clone: ['cloned', 'clone', null],
  git_checkout: ['checked out', 'check out', 'branch'],
  git_merge: ['merged', 'merge', 'branch'],
  git_stash: ['stashed', 'stash', null],
  git_create_branch: ['branched', 'branch', 'name'],
  git_delete_branch: ['deleted the branch', 'delete the branch', 'branch'],
};

/** The read-only git tools read as the command they stand for. */
const GIT_READ: Record<string, string> = {
  git_status: 'git status',
  git_diff: 'git diff',
  git_log: 'git log',
  git_show: 'git show',
  git_blame: 'git blame',
  git_branch_list: 'git branch',
  git_stash_list: 'git stash list',
};

const MEMORY_VERBS: Record<string, [verb: string, base: string]> = {
  memory_write: ['remembered', 'remember'],
  memory_read: ['recalled', 'recall'],
  memory_list: ['recalled', 'recall'],
  memory_delete: ['forgot', 'forget'],
};

/** rules_write records a rule the user gave in one of four files under
 *  ~/.whisper (server/rules_tools.py), named by its target. */
const RULE_TARGETS: Record<string, string> = {
  project_rules: 'project rules',
  global_rules: 'global rules',
  project_whisper_md: 'project WHISPER.md',
  global_whisper_md: 'global WHISPER.md',
};

/** The synthetic cards sseEventCards.ts drops into the transcript. */
export const CARD_TOOLS: ReadonlySet<string> = new Set([
  'hook_blocked', 'stop_hook_feedback', 'stop_hook_block', 'security_blocked',
  'goal_eval', 'goal_cap_reached', 'plan_blocked',
]);

/** Friendly labels for the synthetic cards. */
const DISPLAY_NAMES: Record<string, string> = {
  security_blocked: 'Blocked (security)',
  hook_blocked: 'Blocked by hook',
  plan_blocked: 'Blocked in plan mode',
  stop_hook_feedback: 'Continuing (Stop hook)',
  stop_hook_block: 'Continuing (Stop hook)',
  goal_eval: 'Goal check',
  goal_cap_reached: 'Goal check paused (cap reached)',
};

/** How a goal_eval card names its verdict. */
const VERDICT_LABELS: Record<string, string> = {
  achieved: 'achieved',
  not_achieved: 'not achieved',
  blocked: 'blocked',
  not_checked: 'not checked',
};

export function isReadTool(name: string): boolean {
  return FILE_VERBS[name]?.[0] === 'read';
}

export function isSearchTool(name: string): boolean {
  return SEARCH_TOOLS.has(name);
}

/** A tool that runs a command line, including the read-only git tools. */
export function isCommandTool(name: string): boolean {
  return COMMAND_TOOLS.has(name) || name in GIT_READ;
}

// ── Small helpers ─────────────────────────────────────────────────────

export function str(v: unknown): string | null {
  return typeof v === 'string' && v.trim() ? v : null;
}

export function num(v: unknown): number | null {
  return typeof v === 'number' && Number.isFinite(v) ? v : null;
}

function oneLine(s: string): string {
  return s.replace(/\s+/g, ' ').trim();
}

/** The first `max` characters of a one-line form, marked when cut. */
function head(s: string, max: number): string {
  const line = oneLine(s);
  return line.length > max ? line.slice(0, max - 1).trimEnd() + '…' : line;
}

function capitalize(s: string): string {
  return s ? s[0].toUpperCase() + s.slice(1) : s;
}

export function plural(n: number, noun: string, pluralForm = `${noun}s`): string {
  return `${n} ${n === 1 ? noun : pluralForm}`;
}

function unique(items: string[]): string[] {
  return items.filter((item, i) => items.indexOf(item) === i);
}

/** The first item for each key, in order. */
function uniqueBy<T>(items: T[], key: (item: T) => string | null): T[] {
  const seen = new Set<string | null>();
  return items.filter((item) => {
    const k = key(item);
    if (seen.has(k)) return false;
    seen.add(k);
    return true;
  });
}

export function basename(path: string): string {
  const trimmed = path.replace(/\/+$/, '');
  return trimmed.slice(trimmed.lastIndexOf('/') + 1) || trimmed;
}

function hostOf(url: string): string {
  try {
    return new URL(url).host || head(url, 40);
  } catch {
    return head(url, 40);
  }
}

export function isRunning(tool: ToolUseEvent): boolean {
  return tool.status === 'running' || tool.status === 'pending';
}

/** A goal check whose judge could not run or could not be understood: neither
 *  a pass nor a failure, so it renders neutral and opens to show the reason. */
export function isUncheckedGoal(tool: ToolUseEvent): boolean {
  return tool.toolName === 'goal_eval' && (tool.input ?? {}).verdict === 'not_checked';
}

/** What a synthetic card is called. */
export function stepLabel(tool: ToolUseEvent): string {
  return DISPLAY_NAMES[tool.toolName] ?? tool.toolName;
}

/** The label of a synthetic card's group: the verdict for a goal check, the
 *  card's display name otherwise. */
function cardLabel(tool: ToolUseEvent): string {
  if (tool.toolName === 'goal_eval') {
    const verdict = str((tool.input ?? {}).verdict);
    return verdict ? `Goal check: ${VERDICT_LABELS[verdict] ?? verdict}` : 'Goal check';
  }
  return stepLabel(tool);
}

/** The tail of a synthetic card's group: the reason the server gave, whole;
 *  the row's CSS ellipsises it and the title carries it in full. */
function cardTail(tool: ToolUseEvent): string {
  const input = (tool.input ?? {}) as Record<string, unknown>;
  if (tool.toolName === 'goal_eval') {
    const attempt = num(input.attempt);
    const cap = num(input.cap);
    const feedback = oneLine(str(input.feedback) ?? '');
    const n = attempt && cap ? `${attempt}/${cap}` : '';
    return [n, feedback].filter(Boolean).join(': ');
  }
  const reason = oneLine(str(input.reason) ?? '');
  const toolName = str(input.tool_name);
  if (tool.toolName === 'hook_blocked' && toolName) return `${toolName}: ${reason}`;
  return reason;
}

/** The one-line summary of a call whose subject is not a path: a synthetic
 *  card's verdict and reason, a command, a query, a task. Empty when the
 *  tool has nothing to say beyond its name. */
export function summariseTool(tool: ToolUseEvent): string {
  const input = (tool.input ?? {}) as Record<string, unknown>;
  const name = tool.toolName;

  if (name === 'goal_eval') {
    const verdict = str(input.verdict) ?? '';
    const label = VERDICT_LABELS[verdict] ?? verdict;
    const attempt = num(input.attempt);
    const cap = num(input.cap);
    const n = attempt && cap ? ` (${attempt}/${cap})` : '';
    const feedback = oneLine(str(input.feedback) ?? '');
    return `${label}${n}${feedback ? `: ${feedback}` : ''}`;
  }
  if (CARD_TOOLS.has(name)) return cardTail(tool);

  const path = str(input.path ?? input.file_path ?? input.filepath);
  const pattern = str(input.pattern ?? input.query);

  if (name === 'ws_run_command' || name === 'Bash' || name === 'terminal_run') {
    return oneLine(str(input.command ?? input.cmd) ?? '');
  }
  if (name === 'terminal_send') {
    // The persistent terminal takes its line under `input`, not `command`.
    return oneLine(str(input.input) ?? '');
  }
  if (name === 'run_python') {
    return head(str(input.code) ?? '', 80);
  }
  if (name in FILE_VERBS) {
    return path ? path.split('/').slice(-2).join('/') : '';
  }
  if (name === 'ws_grep' || name === 'Grep') {
    const inPath = path ? ` in ${basename(path)}` : '';
    return pattern ? `"${head(pattern, 40)}"${inPath}` : (path ?? '');
  }
  if (name === 'ws_glob' || name === 'Glob' || name === 'ws_list_directory' || name === 'LS') {
    return head(str(input.pattern ?? input.path) ?? '', 60);
  }
  if (name === 'task_create' || name === 'task_update' || name === 'TodoWrite') {
    const subject = str(input.subject ?? input.title);
    const status = str(input.status);
    if (subject) return status ? `${subject}: ${status}` : subject;
    return status ?? '';
  }
  if (name === 'web_fetch' || name === 'WebFetch' || name === 'web_search' || name === 'WebSearch') {
    return oneLine(str(input.url ?? input.query) ?? '');
  }
  if (name === 'cron_create') {
    return head(str(input.name) ?? '', 50);
  }
  return '';
}

/** The preview the server sends of a longer result: its first 2000
 *  characters plus "..." (server/tool_executor.py). */
const RESULT_PREVIEW_CHARS = 2000;

/** Whether a call's result preview holds the whole output the model got:
 *  not cut by the tool-result budget (meta.truncated) and not cut to the
 *  preview length. A count read from a cut preview would be wrong. */
export function isWholeResult(tool: ToolUseEvent): boolean {
  if (tool.meta?.truncated === true) return false;
  const chars = num(tool.meta?.outputChars);
  if (chars !== null) return chars <= RESULT_PREVIEW_CHARS;
  const result = tool.result ?? '';
  return !(result.length > RESULT_PREVIEW_CHARS && result.endsWith('...'));
}

// The footers and empty answers of server/search/grep.py and glob.py.
const FILES_FOOTER = /^\((\d+) files\)$/gm;
const COUNT_FOOTER = /^\((\d+) total matches across \d+\+? files\)/gm;
const PAGE_HINT = /^(\.\.\. )?\(showing (\d+) results, use offset=\d+ to see next page\)$/gm;
/** grep.py's tail-and-head form of a very large output: its lines are not
 *  all there to count. */
const PERSISTED = /^Full output saved to: /m;
/** A content-mode match line, "path:12:text"; a context line is "path-12-text". */
const MATCH_LINE = /^.+?:\d+:/;

/** The number in the last line a footer pattern matches. */
function lastFooter(text: string, re: RegExp, group = 1): number | null {
  const m = [...text.matchAll(re)].pop();
  return m ? Number(m[group]) : null;
}

/** ws_grep in its three output modes: files_with_matches ends on "(N
 *  files)", count on "(N total matches across M files)", content is one
 *  "path:line:text" row per matching line. A paged answer ends on a
 *  "(showing N results, ...)" hint, so its count is a floor. */
function grepCount(text: string, whole: boolean): string | null {
  if (text.startsWith('No matching files found.')) return '0 files';
  if (text.startsWith('No matches found.')) return '0 matches';
  const total = lastFooter(text, COUNT_FOOTER);
  if (total !== null) return plural(total, 'match', 'matches');
  const files = lastFooter(text, FILES_FOOTER);
  if (files !== null) return plural(files, 'file');
  const hint = [...text.matchAll(PAGE_HINT)].pop();
  if (hint && !hint[1]) return `${hint[2]}+ files`;
  if (!whole || PERSISTED.test(text)) return null;
  const matches = text.split('\n').filter((l) => MATCH_LINE.test(l)).length;
  if (matches === 0) return null;
  return hint ? `${matches}+ matches` : plural(matches, 'match', 'matches');
}

/** ws_glob: the paths, then "(N files)" or a paging hint. */
function globCount(text: string): string | null {
  if (text.startsWith('No files matched.')) return '0 files';
  const files = lastFooter(text, FILES_FOOTER);
  if (files !== null) return plural(files, 'file');
  const paged = lastFooter(text, PAGE_HINT, 2);
  return paged !== null ? `${paged}+ files` : null;
}

/** ws_list_directory: one indented line per entry. */
function listCount(text: string, whole: boolean): string | null {
  if (text === '(empty directory)') return '0 items';
  if (!whole) return null;
  const entries = text.split('\n').filter((l) => /^ {2}\S/.test(l)).length;
  return entries > 0 ? plural(entries, 'item') : null;
}

/** A match, file or item count from a search or listing's text output, read
 *  from the footer the tool prints, or counted from its rows when `whole`
 *  says the preview holds them all (isWholeResult). Null when nothing is
 *  parseable (an error, a format this app does not print), so the caller
 *  shows nothing rather than a wrong number. Never used for a read: a read's
 *  line count comes from meta.lines, because the result is a preview. */
export function countFromResult(toolName: string, result?: string, whole = true): string | null {
  // trimEnd only: a listing's rows begin with their indent.
  const text = (result ?? '').trimEnd();
  if (!text.trim()) return null;
  if (toolName === 'ws_grep' || toolName === 'Grep') return grepCount(text, whole);
  if (toolName === 'ws_glob' || toolName === 'Glob') return globCount(text);
  if (toolName === 'ws_list_directory') return listCount(text, whole);
  return null;
}

/** "ruff check" for "venv/bin/ruff check .": the executable's basename plus
 *  its first argument (plus the next one when the first is a flag, so
 *  "python -m pytest" keeps its module). Of a chained command the last
 *  segment names the failure. */
export function commandName(command: string): string {
  const segment = oneLine(command).split(/\s*(?:&&|\|\||;)\s*/).filter(Boolean).pop() ?? '';
  const tokens = segment.split(' ').filter((t) => t && !/^[A-Za-z_][A-Za-z0-9_]*=/.test(t));
  if (tokens.length === 0) return head(command, 30);
  const parts = [basename(tokens[0])];
  if (tokens.length > 1 && tokens[1] !== '|') {
    parts.push(tokens[1]);
    if (tokens[1].startsWith('-') && tokens.length > 2 && tokens[2] !== '|') parts.push(tokens[2]);
  }
  return parts.join(' ');
}

// ── One call ──────────────────────────────────────────────────────────

const NOUNS: Record<string, [noun: string, pluralForm: string]> = {
  read: ['read', 'reads'],
  edited: ['edit', 'edits'],
  created: ['create', 'creates'],
  wrote: ['write', 'writes'],
  deleted: ['delete', 'deletes'],
  searched: ['search', 'searches'],
  listed: ['listing', 'listings'],
  ran: ['command', 'commands'],
  fetched: ['fetch', 'fetches'],
};

/** The path a file call's input names, which the detail line links to the
 *  editor. A bare `filename` is never one: save_file writes it to
 *  ~/Documents or to its destination_path, and analyze_document's names an
 *  attachment, so joining it to the workspace would open the wrong file. A
 *  save_file destination counts when it is absolute or under ~ (the row
 *  links only the absolute one, see resolvePath). */
function filePathOf(name: string, input: Record<string, unknown>): string | null {
  if (name === 'save_file') {
    const destination = str(input.destination_path);
    return destination && /^[/~]/.test(destination) ? destination : null;
  }
  return str(input.path ?? input.file_path ?? input.filepath ?? input.output_path);
}

export function describeCall(tool: ToolUseEvent): CallDesc {
  const input = (tool.input ?? {}) as Record<string, unknown>;
  const name = tool.toolName;
  const make = (partial: Partial<CallDesc> & Pick<CallDesc, 'kind' | 'verb' | 'base'>): CallDesc => {
    const [noun, nounPlural] = NOUNS[partial.verb] ?? ['call', 'calls'];
    return {
      noun,
      nounPlural,
      subject: null,
      line: '',
      path: null,
      command: null,
      scopePath: null,
      scope: null,
      ...partial,
    };
  };

  if (CARD_TOOLS.has(name)) {
    // The detail line names the card, since a group can mix it with the
    // calls it judged and only the card says what stopped them.
    const summary = summariseTool(tool);
    const line = summary ? `${stepLabel(tool)}: ${summary}` : stepLabel(tool);
    return make({ kind: 'card', verb: cardLabel(tool), base: cardLabel(tool), line });
  }
  if (name in FILE_VERBS) {
    const [verb, base] = FILE_VERBS[name];
    const path = filePathOf(name, input);
    const filename = str(input.filename);
    const title = str(input.title);
    const subject = path ? basename(path) : filename ? basename(filename) : title ? head(title, 40) : null;
    return make({ kind: 'file', verb, base, subject, line: filename ?? subject ?? '', path });
  }
  if (name in GIT_READ) {
    const command = GIT_READ[name];
    return make({ kind: 'command', verb: 'ran', base: 'run', subject: command, line: command, command });
  }
  if (COMMAND_TOOLS.has(name)) {
    const command = name === 'terminal_send' ? str(input.input) : str(input.command ?? input.cmd);
    if (command) {
      return make({ kind: 'command', verb: 'ran', base: 'run', subject: head(command, MAX_LABEL_COMMAND), line: oneLine(command), command });
    }
    // Code tools carry a program, not a command line.
    const subject = name === 'run_python' ? 'python'
      : name === 'office_script' ? 'an office script'
        : name === 'run_tool_script' ? 'a tool script'
          : null;
    const code = str(input.code);
    return make({ kind: 'command', verb: 'ran', base: 'run', subject, line: code ? head(code, 80) : subject ?? '' });
  }
  if (SEARCH_TOOLS.has(name)) {
    const pattern = str(input.pattern ?? input.query);
    const scopePath = str(input.path);
    const subject = pattern ? `"${head(pattern, 40)}"` : null;
    return make({
      kind: 'search', verb: 'searched', base: 'search', subject, line: subject ?? '',
      scopePath, scope: scopePath ? basename(scopePath) : null,
    });
  }
  if (LIST_TOOLS.has(name)) {
    const path = str(input.path);
    const subject = path ? basename(path) : null;
    return make({ kind: 'list', verb: 'listed', base: 'list', subject, line: subject ?? '', path });
  }
  if (FETCH_TOOLS.has(name)) {
    const url = str(input.url);
    return make({ kind: 'fetch', verb: 'fetched', base: 'fetch', subject: url ? hostOf(url) : null, line: url ?? '' });
  }
  if (name in GIT_VERBS) {
    const [verb, base, key] = GIT_VERBS[name];
    const subject = key ? str(input[key]) : null;
    const message = str(input.message);
    return make({ kind: 'git', verb, base, subject, line: subject ?? (message ? head(message, 80) : '') });
  }
  if (name in MEMORY_VERBS) {
    const [verb, base] = MEMORY_VERBS[name];
    // memory_read and memory_delete name their memory by its file alone.
    const filename = str(input.filename);
    const subject = str(input.name ?? input.key ?? input.query) ?? (filename ? basename(filename) : null);
    return make({ kind: 'memory', verb, base, subject, line: subject ?? '', noun: 'memory', nounPlural: 'memories' });
  }
  if (name === 'rules_write') {
    const target = str(input.target);
    const subject = target ? RULE_TARGETS[target] ?? target.replace(/_/g, ' ') : null;
    return make({ kind: 'memory', verb: 'saved', base: 'save', subject, line: subject ?? '', noun: 'rule', nounPlural: 'rules' });
  }
  const friendly = friendlyToolName(name).replace(/_/g, ' ');
  return make({ kind: 'other', verb: 'ran', base: 'run', subject: friendly, line: summariseTool(tool) || friendly });
}

/** The verb cell of a call's detail line; a card's line has none. */
export function callVerb(tool: ToolUseEvent, desc: CallDesc = describeCall(tool)): string {
  if (desc.kind === 'card') return '';
  if (isRunning(tool)) return 'Running';
  return capitalize(desc.verb);
}

// ── The group ─────────────────────────────────────────────────────────

/** Aggregate state for the collapsed row. 'stopped' (the user cut the turn
 *  short) is terminal and never reads as running. 'unchecked' (a goal the
 *  judge could not check) is neither a pass nor a failure. */
export function groupState(tools: ToolUseEvent[]): GroupState {
  if (tools.some((t) => t.status === 'error')) return 'error';
  if (tools.some(isRunning)) return 'running';
  if (tools.some((t) => t.status === 'stopped')) return 'stopped';
  if (tools.some(isUncheckedGoal)) return 'unchecked';
  return 'done';
}

/** A label phrase and the calls it names (by callKey), so the tail does not
 *  repeat them. */
interface Phrase {
  text: string;
  named: string[];
}

/** What tells two calls' subjects apart: the path when there is one (two
 *  files may share a basename), else the subject itself. */
function callKey(c: CallDesc): string | null {
  return c.path ? c.path.replace(/^\.\//, '').replace(/\/+$/, '') : c.subject;
}

/** One phrase for the calls that share a verb: "read 3 files", "edited
 *  ActivityRow.tsx", "ran 2 commands", "searched server". */
function phrase(calls: CallDesc[]): Phrase {
  const first = calls[0];
  const n = calls.length;
  const one = (text: string, named: string | null): Phrase => ({ text, named: named ? [named] : [] });
  switch (first.kind) {
    case 'file': {
      // Distinct files, told apart by path: src/a/index.ts and
      // src/b/index.ts are two files, not one.
      const files = uniqueBy(calls.filter((c) => c.subject !== null), callKey);
      if (files.length === 1) return one(`${first.verb} ${files[0].subject}`, callKey(files[0]));
      if (files.length === 0) return one(first.verb, null);
      return one(`${first.verb} ${plural(files.length, 'file')}`, null);
    }
    case 'command':
      if (n === 1 && first.subject) return one(`ran ${first.subject}`, callKey(first));
      return one(`ran ${plural(n, 'command')}`, null);
    case 'search':
      if (n === 1) return first.scope ? one(`searched ${first.scope}`, null) : one('searched', null);
      return one(`searched ${n} times`, null);
    case 'list':
      if (n === 1) return first.subject ? one(`listed ${first.subject}`, callKey(first)) : one('listed', null);
      return one(`listed ${plural(n, 'folder')}`, null);
    case 'fetch':
      if (n === 1) return first.subject ? one(`fetched ${first.subject}`, callKey(first)) : one('fetched', null);
      return one(`fetched ${plural(n, 'page')}`, null);
    case 'memory':
      if (n === 1) return first.subject ? one(`${first.verb} ${first.subject}`, callKey(first)) : one(first.verb, null);
      return one(`${first.verb} ${plural(n, first.noun, first.nounPlural)}`, null);
    case 'other':
      if (n === 1) return one(`ran ${first.subject}`, null);
      return one(`ran ${first.subject} ${n} times`, null);
    case 'card':
      return one(first.verb, null);
    default:
      if (n === 1) return first.subject ? one(`${first.verb} ${first.subject}`, callKey(first)) : one(first.verb, null);
      return one(`${first.verb} ${n} times`, null);
  }
}

/** The calls grouped by verb in first-occurrence order, one phrase each. */
function phrases(calls: CallDesc[]): Phrase[] {
  const buckets = new Map<string, CallDesc[]>();
  for (const c of calls) {
    const key = c.kind === 'other' ? `other:${c.subject}` : `${c.kind}:${c.verb}`;
    const bucket = buckets.get(key);
    if (bucket) bucket.push(c);
    else buckets.set(key, [c]);
  }
  return [...buckets.values()].map(phrase);
}

/** The -ing form of a file verb's base: read, edit, create, write, delete,
 *  save. A final silent e is dropped; nothing in the file vocabulary doubles
 *  its last consonant. */
function progressive(base: string): string {
  return base.endsWith('e') && !base.endsWith('ee') ? `${base.slice(0, -1)}ing` : `${base}ing`;
}

/** What a running group is running: a single call's subject (a command's
 *  head, a file's basename), else a count of the running calls ("3 reads",
 *  "2 calls"). */
function runningPhrase(tools: ToolUseEvent[], calls: CallDesc[]): Phrase {
  const running = calls.filter((_, i) => isRunning(tools[i]));
  const only = running.length === 1 ? running[0] : null;
  // One file call says what it does to the file ("Reading chat.ts"):
  // "Running chat.ts" would read as executing it.
  if (only && only.kind === 'file' && only.subject) {
    return { text: `${capitalize(progressive(only.base))} ${only.subject}`, named: [callKey(only) ?? only.subject] };
  }
  if (only && (only.kind === 'command' || only.kind === 'other') && only.subject) {
    return { text: `Running ${only.subject}`, named: [callKey(only) ?? only.subject] };
  }
  const nouns = unique(running.map((c) => c.noun));
  const what = nouns.length === 1
    ? plural(running.length, running[0].noun, running[0].nounPlural)
    : plural(running.length, 'call');
  return { text: `Running ${what}`, named: [] };
}

/** The failure an errored group is named after: "ruff check failed",
 *  "Edit chat.ts failed". A failed card (a hook, the security check, plan
 *  mode) names the group over the call it refused: it says why the call
 *  failed, and the call's own words stay in the tail. */
function failurePhrase(tools: ToolUseEvent[], calls: CallDesc[]): Phrase {
  const failed = calls.filter((_, i) => tools[i].status === 'error');
  const c = failed.find((f) => f.kind === 'card') ?? failed[0];
  switch (c.kind) {
    case 'command':
      return { text: `${c.command ? commandName(c.command) : capitalize(c.subject ?? 'command')} failed`, named: [] };
    case 'search':
      return { text: 'Search failed', named: [] };
    case 'card':
      return { text: c.verb, named: [] };
    case 'other':
      return { text: `${capitalize(c.subject ?? 'call')} failed`, named: [] };
    default:
      return {
        text: `${capitalize(c.base)}${c.subject ? ` ${c.subject}` : ''} failed`,
        named: c.subject ? [callKey(c) ?? c.subject] : [],
      };
  }
}

/** What a call contributes to the tail: a basename, a quoted pattern, the
 *  command, a host. Null when it has nothing beyond the label. */
function tailItem(c: CallDesc): string | null {
  switch (c.kind) {
    case 'command':
      return c.command ? oneLine(c.command) : null;
    case 'other':
    case 'card':
      return null;
    default:
      return c.subject ?? (c.line || null);
  }
}

/** The tail's items, one per distinct path or text, in order. Two files that
 *  share a basename show their parent folder too, so they read as two. */
function tailItems(calls: CallDesc[]): string[] {
  const entries = uniqueBy(
    calls.flatMap((c) => {
      const item = tailItem(c);
      return item === null ? [] : [{ item, key: c.path ? callKey(c) : item, path: c.path }];
    }),
    (e) => e.key,
  );
  return entries.map(({ item, key, path }) => {
    const clash = path !== null && entries.some((o) => o.path !== null && o.item === item && o.key !== key);
    return clash && key ? key.split('/').slice(-2).join('/') : item;
  });
}

export function describeGroup(tools: ToolUseEvent[], written?: string): GroupDescription {
  const state = groupState(tools);
  const calls = tools.map(describeCall);
  const cardOnly = calls.length > 0 && calls.every((c) => c.kind === 'card');

  let parts: Phrase[];
  if (cardOnly) {
    parts = [{ text: unique(calls.map((c) => c.verb)).join(', '), named: [] }];
  } else if (state === 'running') {
    parts = [runningPhrase(tools, calls)];
  } else if (state === 'error') {
    parts = [failurePhrase(tools, calls)];
  } else {
    parts = phrases(calls.filter((_, i) => tools[i].status === 'complete'));
  }
  // A failure keeps the executable's own case: "ruff check failed".
  const joined = parts.map((p) => p.text).join(', ');
  let sentence = state === 'error' ? joined : capitalize(joined);
  if (state === 'stopped' && !cardOnly) sentence = sentence ? `${sentence}, stopped` : 'Stopped';

  const named = new Set(parts.flatMap((p) => p.named));
  const items = cardOnly
    ? unique(tools.map(cardTail).filter(Boolean))
    : tailItems(calls.filter((c) => {
        const key = callKey(c);
        return !(key && named.has(key));
      }));
  const stopped = state === 'stopped' && !cardOnly;
  const fullTail = stopped ? 'Stopped by you' : items.join(', ');
  const tail = !stopped && items.length > MAX_TAIL_ITEMS
    ? `${items.slice(0, MAX_TAIL_ITEMS).join(', ')} +${items.length - MAX_TAIL_ITEMS} more`
    : fullTail;

  const title = [sentence, fullTail].filter(Boolean).join('\n');
  // A failed group keeps its failure as the label ("ruff check failed", in
  // red), so a caption never hides what went wrong; the caption is the tail,
  // and the title keeps the deterministic tail (the command) in reach.
  if (written && state === 'error') {
    return { state, label: sentence, tail: written, sentence, title };
  }
  if (written) {
    return { state, label: written, tail: sentence, sentence, title: sentence };
  }
  return { state, label: sentence, tail, sentence, title };
}
