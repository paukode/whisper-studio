import { computeLineDiffFromLines } from './computeLineDiff';
import type { DiffHunk, DiffHunkLine } from '@/api/git';

/** One side of a rendered row: its 1-based line number and text. */
export interface DiffSide {
  no: number;
  text: string;
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

/**
 * Build rows for `full` mode: diff both sides, then pair each removal run
 * with the addition run that follows it so a replaced line sits opposite
 * its replacement.
 *
 * May throw DiffTooLargeError — the server only sends `full` within the
 * same bound, so that means the caller bypassed it.
 */
export function rowsFromTexts(oldText: string, newText: string): DiffRow[] {
  const diff = computeLineDiffFromLines(toLines(oldText), toLines(newText));

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

/** Pair consecutive del/add runs into single rows. */
function pairOps(ops: DiffHunkLine[]): DiffRow[] {
  const rows: DiffRow[] = [];
  let i = 0;

  while (i < ops.length) {
    const op = ops[i];

    if (op.type === 'context') {
      rows.push({
        kind: 'unchanged',
        left: { no: op.old ?? 0, text: op.text },
        right: { no: op.new ?? 0, text: op.text },
      });
      i += 1;
      continue;
    }

    // git emits removals before additions, but the LCS backtrack can
    // yield either order — collect whichever leads, then its counterpart,
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
      const del = dels[k];
      const add = adds[k];
      rows.push({
        kind: 'change',
        left: del ? { no: del.old ?? 0, text: del.text } : null,
        right: add ? { no: add.new ?? 0, text: add.text } : null,
      });
    }
  }

  return rows;
}
