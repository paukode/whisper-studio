export interface DiffLine {
  type: 'added' | 'removed' | 'unchanged';
  text: string;
}

/**
 * Ceiling on the LCS table, which is (old+1) x (new+1) cells of both time
 * and memory. Past this the tab would stall or run out of heap, so callers
 * get a throw they can fall back from instead. Mirrors the server's
 * FULL_MODE_MAX_CELLS, which keeps whole-file payloads under the same bound.
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
  // Build LCS table
  const m = oldLines.length;
  const n = newLines.length;

  if ((m + 1) * (n + 1) > MAX_DIFF_CELLS) {
    throw new DiffTooLargeError();
  }

  const dp: number[][] = Array.from({ length: m + 1 }, () => Array(n + 1).fill(0) as number[]);

  for (let i = 1; i <= m; i++) {
    for (let j = 1; j <= n; j++) {
      if (oldLines[i - 1] === newLines[j - 1]) {
        dp[i][j] = dp[i - 1][j - 1] + 1;
      } else {
        dp[i][j] = Math.max(dp[i - 1][j], dp[i][j - 1]);
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
    } else if (j > 0 && (i === 0 || dp[i][j - 1] >= dp[i - 1][j])) {
      result.push({ type: 'added', text: newLines[j - 1] });
      j--;
    } else {
      result.push({ type: 'removed', text: oldLines[i - 1] });
      i--;
    }
  }

  return result.reverse();
}
