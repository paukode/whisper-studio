import type { ToolUseEvent } from '@/types/chat';
import {
  countFromResult,
  describeCall,
  isCommandTool,
  isReadTool,
  isRunning,
  isSearchTool,
  isWholeResult,
  num,
  type CallDesc,
} from '@/components/chat/activityLabel';

/**
 * The numbers on an activity row: the chips in the collapsed row's meta
 * column and the outcome cell of each call's detail line. Nothing here is
 * computed from a result preview that could be cut short: a line count comes
 * from meta.lines or is omitted, a match count from the search's own footer
 * or from rows the preview holds whole, a diff from meta.added and
 * meta.removed, an exit code from the server's exit_code (with the trailing
 * "(exit code N)" line of older transcripts as the fallback). A chip built
 * on a meta field renders only when that field is a number.
 */

/** A chip in the row's meta column or a call's outcome cell. */
export type Chip =
  | { kind: 'diff'; added: number; removed: number }
  | { kind: 'text'; text: string; tone: 'neutral' | 'err' | 'live' | 'muted' };

/** "1,120": every count the row prints, in one locale so tests and users
 *  read the same digits. */
export function formatCount(n: number): string {
  return n.toLocaleString('en-US');
}

/** "12 KB" style size for a tool call whose arguments are still arriving. */
export function formatKb(chars: number): string {
  const kb = chars / 1024;
  return `${kb < 10 ? kb.toFixed(1) : Math.round(kb)} KB`;
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  return formatKb(bytes);
}

function text(t: string, tone: 'neutral' | 'err' | 'live' | 'muted' = 'neutral'): Chip {
  return { kind: 'text', text: t, tone };
}

/** The exit code of a command: the server's field when it sent one, else
 *  the trailing "(exit code N)" line older transcripts carry. */
export function exitCodeOf(tool: ToolUseEvent): number | null {
  if (typeof tool.exitCode === 'number') return tool.exitCode;
  const m = /\(?exit(?: code)?[ :]+(\d+)\)?\s*$/i.exec((tool.result ?? '').trimEnd());
  return m ? Number(m[1]) : null;
}

/** The pass and fail counts of a test runner's summary (vitest, pytest and
 *  jest all print "N passed" and "N failed"), from the last such line. */
export function parseTestSummary(result?: string): { passed: number; failed: number } | null {
  if (!result) return null;
  const passed = [...result.matchAll(/\b(\d+)\s+passed\b/g)].pop();
  if (!passed) return null;
  const failed = [...result.matchAll(/\b(\d+)\s+failed\b/g)].pop();
  return { passed: Number(passed[1]), failed: failed ? Number(failed[1]) : 0 };
}

/** Both diff numbers of a call, or null when either is unknown. */
export function callDiff(tool: ToolUseEvent): { added: number; removed: number } | null {
  const added = num(tool.meta?.added);
  const removed = num(tool.meta?.removed);
  return added !== null && removed !== null ? { added, removed } : null;
}

/** A command's outcome: a non-zero exit always shows; a passing test summary
 *  beats "exit 0". */
function commandOutcome(tool: ToolUseEvent): Chip | null {
  const exit = exitCodeOf(tool);
  if (exit !== null && exit !== 0) return text(`exit ${exit}`, 'err');
  const summary = parseTestSummary(tool.result);
  if (summary && summary.failed === 0) return text(`${formatCount(summary.passed)} passed`);
  if (exit === 0) return text('exit 0');
  return null;
}

/** The outcome cell of a call's detail line: a line count or range for a
 *  read, the diff of an edit, a match count, an exit code or a test summary
 *  for a command. Null when the call has nothing to report. */
export function callOutcome(tool: ToolUseEvent, desc: CallDesc = describeCall(tool)): Chip | null {
  if (tool.status === 'stopped') return text('not run', 'muted');
  if (isRunning(tool)) {
    const chars = num(tool.progressChars);
    return text(chars ? `${formatKb(chars)} so far` : 'no output yet', 'muted');
  }
  if (desc.kind === 'card') return null;
  const input = (tool.input ?? {}) as Record<string, unknown>;
  if (desc.kind === 'file' && desc.verb === 'read') {
    const lines = num(tool.meta?.lines);
    if (lines !== null) return text(`${formatCount(lines)} ${lines === 1 ? 'line' : 'lines'}`);
    const offset = num(input.offset);
    const limit = num(input.limit);
    if (offset !== null && limit !== null) return text(`lines ${formatCount(offset)} to ${formatCount(offset + limit - 1)}`);
    if (offset !== null) return text(`from line ${formatCount(offset)}`);
    if (limit !== null) return text(`first ${formatCount(limit)} lines`);
    return null;
  }
  if (desc.kind === 'file') {
    const diff = callDiff(tool);
    return diff ? { kind: 'diff', ...diff } : null;
  }
  if (tool.status !== 'complete') {
    return desc.kind === 'command' ? commandOutcome(tool) : null;
  }
  if (desc.kind === 'search' || desc.kind === 'list') {
    const count = countFromResult(tool.toolName, tool.result, isWholeResult(tool));
    return count ? text(count) : null;
  }
  if (desc.kind === 'command') return commandOutcome(tool);
  if (desc.kind === 'fetch') {
    // The size of the page text the fetch returned (the server's full output
    // length; meta.bytes is a write's size and is never set for a fetch).
    const size = num(tool.meta?.outputChars);
    return size !== null ? text(formatBytes(size)) : null;
  }
  return null;
}

/** The chips of the collapsed row: the summed diff, the read total, a single
 *  search's matches, the failed command's exit code or the test summary, and
 *  how many calls a stop left unrun. The elapsed chip of a running group is
 *  the row's own, since it needs a clock. */
export function groupChips(tools: ToolUseEvent[]): Chip[] {
  const chips: Chip[] = [];

  const diffs = tools.map(callDiff).filter((d): d is { added: number; removed: number } => d !== null);
  if (diffs.length > 0) {
    chips.push({
      kind: 'diff',
      added: diffs.reduce((sum, d) => sum + d.added, 0),
      removed: diffs.reduce((sum, d) => sum + d.removed, 0),
    });
  }

  const lineCounts = tools.filter((t) => isReadTool(t.toolName)).map((t) => num(t.meta?.lines));
  if (lineCounts.length > 0 && lineCounts.every((n): n is number => n !== null)) {
    const total = lineCounts.reduce((sum, n) => sum + n, 0);
    chips.push(text(`${formatCount(total)} ${total === 1 ? 'line' : 'lines'}`));
  }

  const searches = tools.filter((t) => isSearchTool(t.toolName));
  if (searches.length === 1 && searches[0].status === 'complete') {
    const count = countFromResult(searches[0].toolName, searches[0].result, isWholeResult(searches[0]));
    if (count) chips.push(text(count));
  }

  const commands = tools.filter((t) => isCommandTool(t.toolName) && (t.status === 'complete' || t.status === 'error'));
  const failed = commands.map(exitCodeOf).find((exit) => exit !== null && exit !== 0);
  if (failed !== undefined && failed !== null) {
    chips.push(text(`exit ${failed}`, 'err'));
  } else {
    const summary = commands.map((t) => parseTestSummary(t.result)).filter((s) => s !== null).pop();
    if (summary && summary.failed === 0) chips.push(text(`${formatCount(summary.passed)} passed`));
  }

  const stopped = tools.filter((t) => t.status === 'stopped').length;
  if (stopped > 0) chips.push(text(`${formatCount(stopped)} not run`, 'muted'));

  return chips;
}
