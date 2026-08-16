import { describe, it, expect } from 'vitest';
import {
  toLines,
  rowsFromTexts,
  rowsFromHunks,
  collapseUnchanged,
  type DiffRow,
} from './diffRows';
import { computeLineDiff, DiffTooLargeError, MAX_DIFF_CELLS } from './computeLineDiff';
import type { DiffHunk } from '@/api/git';

/** Compact row shape for readable assertions: kind + both line numbers. */
function shape(rows: DiffRow[]) {
  return rows.map((r) =>
    r.kind === 'gap'
      ? ['gap', r.count]
      : [r.kind, r.left ? r.left.no : null, r.right ? r.right.no : null],
  );
}

describe('toLines', () => {
  it('drops the empty line a trailing newline leaves behind', () => {
    expect(toLines('a\nb\n')).toEqual(['a', 'b']);
  });

  it('keeps a genuinely blank final line', () => {
    expect(toLines('a\n\n')).toEqual(['a', '']);
  });

  it('treats empty content as no lines, not one blank line', () => {
    expect(toLines('')).toEqual([]);
  });
});

describe('rowsFromTexts', () => {
  it('numbers both sides independently', () => {
    const rows = rowsFromTexts('one\ntwo\nthree\n', 'one\nTWO\nthree\n');

    expect(shape(rows)).toEqual([
      ['unchanged', 1, 1],
      ['change', 2, 2],
      ['unchanged', 3, 3],
    ]);
  });

  it('pairs a replaced line opposite its replacement', () => {
    const rows = rowsFromTexts('keep\nold\n', 'keep\nnew\n');
    const change = rows[1];

    expect(change.kind).toBe('change');
    if (change.kind !== 'change') throw new Error('expected a change row');
    expect(change.left?.text).toBe('old');
    expect(change.right?.text).toBe('new');
  });

  it('leaves the opposite side empty for a pure insertion', () => {
    const rows = rowsFromTexts('a\nb\n', 'a\nmiddle\nb\n');

    expect(shape(rows)).toEqual([
      ['unchanged', 1, 1],
      ['change', null, 2],
      ['unchanged', 2, 3],
    ]);
  });

  it('leaves the opposite side empty for a pure deletion', () => {
    const rows = rowsFromTexts('a\ngone\nb\n', 'a\nb\n');

    expect(shape(rows)).toEqual([
      ['unchanged', 1, 1],
      ['change', 2, null],
      ['unchanged', 3, 2],
    ]);
  });

  it('renders a created file as additions with no left side', () => {
    const rows = rowsFromTexts('', 'alpha\nbeta\n');

    expect(shape(rows)).toEqual([
      ['change', null, 1],
      ['change', null, 2],
    ]);
  });

  it('renders a deleted file as removals with no right side', () => {
    const rows = rowsFromTexts('alpha\nbeta\n', '');

    expect(shape(rows)).toEqual([
      ['change', 1, null],
      ['change', 2, null],
    ]);
  });

  it('pairs uneven runs and lets the surplus stand alone', () => {
    const rows = rowsFromTexts('x\ny\n', 'X\nY\nZ\n');
    const changes = rows.filter((r) => r.kind === 'change');

    expect(changes).toHaveLength(3);
    expect(shape(rows).filter((s) => s[0] === 'change')).toEqual([
      ['change', 1, 1],
      ['change', 2, 2],
      ['change', null, 3],
    ]);
  });
});

describe('rowsFromHunks', () => {
  const hunks: DiffHunk[] = [
    {
      old_start: 5,
      new_start: 5,
      header: 'def a():',
      lines: [
        { type: 'context', old: 5, new: 5, text: 'ctx' },
        { type: 'del', old: 6, new: null, text: 'was' },
        { type: 'add', old: null, new: 6, text: 'now' },
      ],
    },
    {
      old_start: 40,
      new_start: 40,
      header: 'def b():',
      lines: [{ type: 'add', old: null, new: 40, text: 'extra' }],
    },
  ];

  it('keeps the server line numbers and pairs within a hunk', () => {
    const rows = rowsFromHunks([hunks[0]]);

    expect(shape(rows)).toEqual([
      ['unchanged', 5, 5],
      ['change', 6, 6],
    ]);
  });

  it('stands a gap between hunks for the lines git omitted', () => {
    const rows = rowsFromHunks(hunks);

    expect(shape(rows)).toEqual([
      ['unchanged', 5, 5],
      ['change', 6, 6],
      ['gap', 33],
      ['change', null, 40],
    ]);
  });

  it('leaves hunk gaps unexpandable — the client never got those lines', () => {
    const gap = rowsFromHunks(hunks).find((r) => r.kind === 'gap');

    expect(gap?.kind === 'gap' && gap.rows).toEqual([]);
  });
});

describe('collapseUnchanged', () => {
  const unchanged = (n: number): DiffRow => ({
    kind: 'unchanged',
    left: { no: n, text: `l${n}` },
    right: { no: n, text: `l${n}` },
  });
  const change = (n: number): DiffRow => ({
    kind: 'change',
    left: { no: n, text: 'a' },
    right: { no: n, text: 'b' },
  });

  it('keeps context on both sides of a change and hides the middle', () => {
    const rows = [...Array.from({ length: 20 }, (_, i) => unchanged(i + 1))];
    rows[10] = change(11);

    const out = collapseUnchanged(rows, 3);

    // Leading run collapses to its last 3 rows, then the change, then 3
    // trailing rows, then the rest hidden.
    expect(shape(out)).toEqual([
      ['gap', 7],
      ['unchanged', 8, 8],
      ['unchanged', 9, 9],
      ['unchanged', 10, 10],
      ['change', 11, 11],
      ['unchanged', 12, 12],
      ['unchanged', 13, 13],
      ['unchanged', 14, 14],
      ['gap', 6],
    ]);
  });

  it('keeps the hidden rows so a gap can expand in place', () => {
    const rows = Array.from({ length: 12 }, (_, i) => unchanged(i + 1));
    rows[11] = change(12);

    const gap = collapseUnchanged(rows, 3).find((r) => r.kind === 'gap');

    expect(gap?.kind === 'gap' && gap.rows).toHaveLength(8);
    expect(gap?.kind === 'gap' && gap.rows.every((r) => r.kind === 'unchanged')).toBe(true);
  });

  it('leaves a short run alone rather than trading rows for a marker', () => {
    const rows = [change(1), unchanged(2), unchanged(3), change(4)];

    expect(collapseUnchanged(rows, 3)).toEqual(rows);
  });

  it('collapses an unchanged file into a single gap', () => {
    const rows = Array.from({ length: 30 }, (_, i) => unchanged(i + 1));

    expect(shape(collapseUnchanged(rows, 3))).toEqual([['gap', 30]]);
  });

  it('passes hunk gaps through untouched', () => {
    const rows: DiffRow[] = [change(1), { kind: 'gap', count: 9, rows: [] }, change(2)];

    expect(collapseUnchanged(rows, 3)).toEqual(rows);
  });
});

describe('computeLineDiff guard', () => {
  it('throws rather than allocating an unbounded LCS table', () => {
    // Both sides must differ throughout: the guard measures the changed
    // span, and a shared head or tail is stripped before it is consulted.
    const n = Math.ceil(Math.sqrt(MAX_DIFF_CELLS)) + 10;
    const left = Array.from({ length: n }, (_, i) => `l${i}`).join('\n');
    const right = Array.from({ length: n }, (_, i) => `r${i}`).join('\n');

    expect(() => computeLineDiff(left, right)).toThrow(DiffTooLargeError);
  });

  it('strips the common head and tail so a small edit stays cheap', () => {
    // Far past the cell cap as whole files, but only one line differs, so
    // the LCS never sees a table anywhere near MAX_DIFF_CELLS.
    const n = Math.ceil(Math.sqrt(MAX_DIFF_CELLS)) + 10;
    const lines = Array.from({ length: n }, (_, i) => `line ${i}`);
    const edited = lines.slice();
    edited[Math.floor(n / 2)] = 'edited';
    const before = lines.join('\n');
    const after = edited.join('\n');

    const diff = computeLineDiff(before, after);

    expect(diff.filter((l) => l.type === 'removed')).toEqual([
      { type: 'removed', text: `line ${Math.floor(n / 2)}` },
    ]);
    expect(diff.filter((l) => l.type === 'added')).toEqual([
      { type: 'added', text: 'edited' },
    ]);
    expect(diff).toHaveLength(n + 1);
  });

  it('still diffs inputs inside the bound', () => {
    expect(computeLineDiff('a', 'b')).toEqual([
      { type: 'removed', text: 'a' },
      { type: 'added', text: 'b' },
    ]);
  });
});
