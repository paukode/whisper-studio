export interface DiffLine {
  type: 'added' | 'removed' | 'unchanged';
  text: string;
}

/**
 * Ceiling on the LCS table, which is (old+1) x (new+1) cells of both time
 * and memory. Past this the tab would stall or run out of heap, so callers
 * get a throw they can fall back from instead.
 *
 * Measured against the *changed span* only — the common head and tail are
 * stripped first — so this bites on files that genuinely differ throughout,
 * not merely on long files. The server's FULL_MODE_MAX_CELLS applies the
 * same number to whole files, which is the stricter of the two and keeps
 * `full` payloads small; it is a bound on the wire, not on this table.
 */
export const MAX_DIFF_CELLS = 9_000_000;

export class DiffTooLargeError extends Error {
  constructor() {
    super('Diff too large to compute');
    this.name = 'DiffTooLargeError';
  }
}

/**
 * Compute a line-level diff between two strings.
 *
 * Uses a simple LCS (longest common subsequence) approach on the
 * split lines to produce a minimal diff.
 *
 * Throws DiffTooLargeError when the inputs exceed MAX_DIFF_CELLS.
 */
export function computeLineDiff(oldText: string, newText: string): DiffLine[] {
  return computeLineDiffFromLines(oldText.split('\n'), newText.split('\n'));
}

/**
 * Line-array variant, for callers that have already split the sides (and
 * trimmed artifacts like a file's trailing newline). Same LCS, same bound.
 */
export function computeLineDiffFromLines(oldLines: string[], newLines: string[]): DiffLine[] {
  // Real edits touch a handful of lines in the middle of a file that is
  // otherwise identical on both sides, so peel off the matching head and
  // tail before diffing. The LCS then runs over the changed span alone:
  // a 30-line edit in a 1000-line file costs a table of hundreds of cells
  // instead of a million, and only the span has to fit MAX_DIFF_CELLS.
  let prefix = 0;
  const maxPrefix = Math.min(oldLines.length, newLines.length);
  while (prefix < maxPrefix && oldLines[prefix] === newLines[prefix]) prefix++;

  let suffix = 0;
  const maxSuffix = maxPrefix - prefix;
  while (
    suffix < maxSuffix &&
    oldLines[oldLines.length - 1 - suffix] === newLines[newLines.length - 1 - suffix]
  ) {
    suffix++;
  }

  const oldSpan = oldLines.slice(prefix, oldLines.length - suffix);
  const newSpan = newLines.slice(prefix, newLines.length - suffix);

  const result: DiffLine[] = [];
  for (let k = 0; k < prefix; k++) result.push({ type: 'unchanged', text: oldLines[k] });
  result.push(...lcsDiff(oldSpan, newSpan));
  for (let k = newLines.length - suffix; k < newLines.length; k++) {
    result.push({ type: 'unchanged', text: newLines[k] });
  }
  return result;
}

/** LCS diff of two line spans already stripped of their common ends. */
function lcsDiff(oldLines: string[], newLines: string[]): DiffLine[] {
  const m = oldLines.length;
  const n = newLines.length;

  // One side empty: the other side is wholly added or wholly removed, and
  // there is no table to build.
  if (m === 0) return newLines.map((text) => ({ type: 'added', text }) as DiffLine);
  if (n === 0) return oldLines.map((text) => ({ type: 'removed', text }) as DiffLine);

  if ((m + 1) * (n + 1) > MAX_DIFF_CELLS) {
    throw new DiffTooLargeError();
  }

  // Flat Int32Array rather than nested number[][]: one contiguous
  // allocation instead of m+1 of them, and no per-row pointer chase in the
  // hot loop. Row i starts at i * stride.
  const stride = n + 1;
  const dp = new Int32Array((m + 1) * stride);

  for (let i = 1; i <= m; i++) {
    const row = i * stride;
    const prev = row - stride;
    const oldLine = oldLines[i - 1];
    for (let j = 1; j <= n; j++) {
      if (oldLine === newLines[j - 1]) {
        dp[row + j] = dp[prev + j - 1] + 1;
      } else {
        const up = dp[prev + j];
        const left = dp[row + j - 1];
        dp[row + j] = up >= left ? up : left;
      }
    }
  }

  // Backtrack to produce diff
  const result: DiffLine[] = [];
  let i = m;
  let j = n;

  while (i > 0 || j > 0) {
    if (i > 0 && j > 0 && oldLines[i - 1] === newLines[j - 1]) {
      result.push({ type: 'unchanged', text: oldLines[i - 1] });
      i--;
      j--;
    } else if (j > 0 && (i === 0 || dp[i * stride + j - 1] >= dp[(i - 1) * stride + j])) {
      result.push({ type: 'added', text: newLines[j - 1] });
      j--;
    } else {
      result.push({ type: 'removed', text: oldLines[i - 1] });
      i--;
    }
  }

  return result.reverse();
}
