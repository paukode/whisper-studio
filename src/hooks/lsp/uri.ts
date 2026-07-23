/**
 * Filesystem-path <-> file:// URI helpers for the LSP client.
 *
 * The backend proxy runs on macOS/Linux and hands the editor workspace-relative
 * file paths (e.g. `src/app.py`) plus an absolute workspace root (uiStore.wsPath).
 * The language server wants `file://` URIs, so we join the two and encode.
 */

/** Join an absolute workspace root with a workspace-relative path (POSIX). */
export function joinWorkspacePath(workspacePath: string, relPath: string): string {
  const root = workspacePath.replace(/\/+$/, '');
  const rel = relPath.replace(/^\/+/, '');
  return rel ? `${root}/${rel}` : root;
}

/** Convert an absolute POSIX path to a `file://` URI, encoding each segment. */
export function pathToFileUri(absPath: string): string {
  const encoded = absPath.split('/').map(encodeURIComponent).join('/');
  return `file://${encoded}`;
}

/** file:// URI for a workspace-relative file within the given workspace root. */
export function fileUriFor(workspacePath: string, relPath: string): string {
  return pathToFileUri(joinWorkspacePath(workspacePath, relPath));
}

/**
 * Compare two file:// URIs tolerantly. Servers may re-encode segments
 * differently than we did (e.g. spaces), so fall back to comparing the decoded
 * form when the raw strings differ.
 */
export function fileUriMatches(a: string, b: string): boolean {
  if (a === b) return true;
  try {
    return decodeURI(a) === decodeURI(b);
  } catch {
    return false;
  }
}
