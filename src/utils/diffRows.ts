import { computeLineDiffFromLines } from './computeLineDiff';
import type { DiffHunk, DiffHunkLine } from '@/api/git';

/** One side of a rendered row: its 1-based line number and text. */
export interface DiffSide {
  no: number;
  text: string;
  /** On a replaced line whose ending is what changed: it has none (the end
   *  of a file with no final newline) or it ends in CRLF where the line
   *  opposite ends in LF. The grid draws a note in its place. */
  eol?: 'none' | 'crlf';
}

/**
 * A row in the side-by-side view. Rows are the alignment unit: the left
 * and right panes render the same row list, so the two sides cannot drift
 * out of sync no matter how the content wraps or scrolls.
 *
 * - `unchanged` present and identical on both sides
 * - `change`    a removal (right null), an addition (left null), or a
 *               replaced line (both present)
 * - `gap`       a skipped run of unchanged lines. `rows` holds them when
 *               the client has the content and can expand in place; it is
 *               empty for hunk payloads, where the server never sent them.
 */
export type DiffRow =
  | { kind: 'unchanged'; left: DiffSide; right: DiffSide }
  | { kind: 'change'; left: DiffSide | null; right: DiffSide | null }
  | { kind: 'gap'; count: number; rows: DiffRow[] };

/** Unchanged lines kept either side of a change when collapsing. */
export const DEFAULT_CONTEXT_LINES = 3;

/**
 * Split file text into lines for diffing.
 *
 * Drops the empty string a trailing newline leaves behind, which would
 * otherwise render as a phantom final line on both sides, and maps empty
 * content to no lines at all (a created or deleted file, not a blank one).
 */
export function toLines(text: string): string[] {
  if (text === '') return [];
  const lines = text.split('\n');
  if (lines[lines.length - 1] === '') lines.pop();
  return lines;
}

/** Ends the last line of a text that has no final newline when the other
 *  text has one, while the two are compared: "b" at the end of a file and
 *  "b\n" are then different lines, the way git and the server's counts
 *  (server/workspace/edits.py diff_stats) see them. Taken off again for the
 *  rows (DiffSide.eol). */
const NO_EOL = '\u0000';

/** The lines of both texts for the diff: toLines, and NO_EOL on the last
 *  line of the one that has no final newline when only one of them has it.
 *  A created or deleted file (one text empty) has nothing to compare it
 *  with. */
function linesForDiff(oldText: string, newText: string): [string[], string[]] {
  const oldLines = toLines(oldText);
  const newLines = toLines(newText);
  if (oldText !== '' && newText !== '' && oldText.endsWith('\n') !== newText.endsWith('\n')) {
    const open = oldText.endsWith('\n') ? newLines : oldLines;
    open[open.length - 1] += NO_EOL;
  }
  return [oldLines, newLines];
}

/**
 * Build rows for `full` mode: diff both sides, then pair each removal run
 * with the addition run that follows it so a replaced line sits opposite
 * its replacement. A line whose ending alone changed (CRLF to LF, a final
 * newline added or taken off) counts as changed, as git counts it, and its
 * row says so (DiffSide.eol).
 *
 * Throws DiffTooLargeError when the changed span is over MAX_DIFF_CELLS.
 * The Git panel gets `full` only within that bound. A stored edit and the
 * approval card can go over it, and their callers show a fallback.
 */
export function rowsFromTexts(oldText: string, newText: string): DiffRow[] {
  const diff = computeLineDiffFromLines(...linesForDiff(oldText, newText));

  let oldNo = 0;
  let newNo = 0;
  const ops: DiffHunkLine[] = diff.map((line) => {
    if (line.type === 'unchanged') {
      oldNo += 1;
      newNo += 1;
      return { type: 'context', old: oldNo, new: newNo, text: line.text };
    }
    if (line.type === 'removed') {
      oldNo += 1;
      return { type: 'del', old: oldNo, new: null, text: line.text };
    }
    newNo += 1;
    return { type: 'add', old: null, new: newNo, text: line.text };
  });

  return pairOps(ops);
}

/**
 * Build rows for `hunks` mode. Each hunk pairs the same way; between them
 * a gap row stands in for the unchanged lines git left out.
 */
export function rowsFromHunks(hunks: DiffHunk[]): DiffRow[] {
  const rows: DiffRow[] = [];
  let prevOldEnd: number | null = null;

  for (const hunk of hunks) {
    const firstOld = hunk.lines.find((l) => l.old !== null)?.old ?? hunk.old_start;
    if (prevOldEnd !== null && firstOld > prevOldEnd + 1) {
      rows.push({ kind: 'gap', count: firstOld - prevOldEnd - 1, rows: [] });
    }
    rows.push(...pairOps(hunk.lines));
    const lastOld = [...hunk.lines].reverse().find((l) => l.old !== null)?.old;
    if (lastOld != null) prevOldEnd = lastOld;
  }

  return rows;
}

/**
 * Replace long runs of unchanged rows with a single expandable gap,
 * keeping `context` rows next to the changes that need them. Runs at the
 * very start or end of the file only need context on their inner edge.
 */
export function collapseUnchanged(rows: DiffRow[], context = DEFAULT_CONTEXT_LINES): DiffRow[] {
  const out: DiffRow[] = [];
  let i = 0;

  while (i < rows.length) {
    if (rows[i].kind !== 'unchanged') {
      out.push(rows[i]);
      i += 1;
      continue;
    }

    let end = i;
    while (end < rows.length && rows[end].kind === 'unchanged') end += 1;
    const run = rows.slice(i, end);

    const atStart = i === 0;
    const atEnd = end === rows.length;
    const head = atStart ? 0 : context;
    const tail = atEnd ? 0 : context;

    // Not worth a gap unless it hides more rows than the marker costs.
    if (run.length <= head + tail + 1) {
      out.push(...run);
    } else {
      out.push(...run.slice(0, head));
      out.push({
        kind: 'gap',
        count: run.length - head - tail,
        rows: run.slice(head, run.length - tail),
      });
      if (tail > 0) out.push(...run.slice(run.length - tail));
    }

    i = end;
  }

  return out;
}

/** A line's text as the grid draws it, and how it ended: with no newline
 *  (NO_EOL), with CRLF, or with LF. */
interface BareLine {
  text: string;
  end: 'none' | 'crlf' | 'lf';
}

function bare(text: string): BareLine {
  if (text.endsWith(NO_EOL)) return { text: text.slice(0, -1).replace(/\r$/, ''), end: 'none' };
  if (text.endsWith('\r')) return { text: text.slice(0, -1), end: 'crlf' };
  return { text, end: 'lf' };
}

/** One side of a changed row, marked where its ending is the difference:
 *  against the line opposite it, or, standing alone, as a last line with
 *  no newline. */
function changeSide(no: number, own: BareLine, other: BareLine | null): DiffSide {
  const { text, end } = own;
  if (end === 'lf' || (other ? other.end === end : end !== 'none')) return { no, text };
  return { no, text, eol: end };
}

/** The two sides of a changed row. */
function changeSides(del: DiffHunkLine | undefined, add: DiffHunkLine | undefined): [DiffSide | null, DiffSide | null] {
  const left = del ? bare(del.text) : null;
  const right = add ? bare(add.text) : null;
  return [
    del && left ? changeSide(del.old ?? 0, left, right) : null,
    add && right ? changeSide(add.new ?? 0, right, left) : null,
  ];
}

/** Pair consecutive del/add runs into single rows. */
function pairOps(ops: DiffHunkLine[]): DiffRow[] {
  const rows: DiffRow[] = [];
  let i = 0;

  while (i < ops.length) {
    const op = ops[i];

    if (op.type === 'context') {
      const { text } = bare(op.text);
      rows.push({
        kind: 'unchanged',
        left: { no: op.old ?? 0, text },
        right: { no: op.new ?? 0, text },
      });
      i += 1;
      continue;
    }

    // git emits removals before additions, but the LCS backtrack can
    // yield either order: collect whichever leads, then its counterpart,
    // so a replaced line still lands opposite its replacement.
    const dels: DiffHunkLine[] = [];
    const adds: DiffHunkLine[] = [];
    const collect = (type: 'del' | 'add', into: DiffHunkLine[]) => {
      while (i < ops.length && ops[i].type === type) {
        into.push(ops[i]);
        i += 1;
      }
    };
    if (op.type === 'del') {
      collect('del', dels);
      collect('add', adds);
    } else {
      collect('add', adds);
      collect('del', dels);
    }

    const pairs = Math.max(dels.length, adds.length);
    for (let k = 0; k < pairs; k++) {
      const [left, right] = changeSides(dels[k], adds[k]);
      rows.push({ kind: 'change', left, right });
    }
  }

  return rows;
}
