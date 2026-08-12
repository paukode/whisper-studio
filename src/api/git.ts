import { get, post } from './client';

export interface GitFileStatus {
  path: string;
  status: string;
  staged: boolean;
}

/** HEAD version of a tracked file. Returns ``{content: null, error}``
 *  if git rejects the request (e.g. untracked path), otherwise the
 *  raw file content. Use to power a "View HEAD" affordance next to
 *  modified files in the Git Changes panel. */
export interface GitHeadContent {
  content: string | null;
  error?: string;
}

export function getGitDiff(path: string): Promise<GitHeadContent> {
  return get<GitHeadContent>(`/api/git/show?path=${encodeURIComponent(path)}`);
}

/** One line of a server-parsed hunk. ``old``/``new`` are 1-based line
 *  numbers, null on the side where the line does not exist. */
export interface DiffHunkLine {
  type: 'context' | 'add' | 'del';
  old: number | null;
  new: number | null;
  text: string;
}

export interface DiffHunk {
  old_start: number;
  new_start: number;
  /** The `@@ ... @@` trailer, usually the enclosing function. */
  header: string;
  lines: DiffHunkLine[];
}

/** A single file's diff against HEAD.
 *
 *  ``mode`` decides how the viewer renders, and the server picks it:
 *  - ``full``   both sides present; align them client-side for a
 *               whole-file view with unchanged context everywhere.
 *  - ``hunks``  file too large to align in the browser; git did the diff
 *               and ``hunks`` carries it with real line numbers.
 *  - ``binary`` nothing to show line by line.
 *  - ``error``  ``error`` explains why there is no diff. */
export interface GitFileDiff {
  path: string;
  mode: 'full' | 'hunks' | 'binary' | 'error';
  status: 'modified' | 'added' | 'deleted';
  old: string | null;
  new: string | null;
  hunks: DiffHunk[];
  added: number;
  removed: number;
  truncated: boolean;
  error: string | null;
}

export function getGitFileDiff(path: string): Promise<GitFileDiff> {
  return get<GitFileDiff>(`/api/git/file-diff?path=${encodeURIComponent(path)}`);
}

/** Combined status + diff, server-cached for 1s. Prefer this over the
 *  separate status/diff calls for UI panels. */
export interface GitChanges {
  branch: string;
  files: GitFileStatus[];
  files_count: number;
  lines_added: number;
  lines_removed: number;
  per_file_stats: Record<string, { added: number; removed: number; is_binary?: boolean; is_untracked?: boolean }>;
}

export function getGitChanges(): Promise<GitChanges> {
  return get<GitChanges>('/api/git/changes');
}

/** Lightweight status for the app status bar: branch, dirty counts, and
 *  sync state vs upstream. Cheaper than getGitChanges (no per-file diff). */
export interface GitStatusBar {
  branch: string;
  clean: boolean;
  changed: number;
  untracked: number;
  ahead: number;
  behind: number;
}

export function getGitStatus(): Promise<GitStatusBar> {
  return get<GitStatusBar>('/api/git/status');
}

export function gitRestoreFile(path: string): Promise<void> {
  return post<void>('/api/git/restore', { path });
}

export interface GitBranchInfo {
  branch: string;
  default: string;
  is_default: boolean;
}

export function getGitBranch(): Promise<GitBranchInfo> {
  return get<GitBranchInfo>('/api/git/branch');
}

export interface GitWorktree {
  path: string;
  head?: string;
  branch?: string | null;
  bare?: boolean;
  is_current?: boolean;
}

export interface GitWorktreesResponse {
  worktrees: GitWorktree[];
  current: string;
}

export function getGitWorktrees(): Promise<GitWorktreesResponse> {
  return get<GitWorktreesResponse>('/api/git/worktrees');
}

export function addGitWorktree(branch: string, opts: { path?: string; createBranch?: boolean } = {}): Promise<{ success: boolean; path: string; branch: string }> {
  return post('/api/git/worktrees', {
    branch,
    path: opts.path,
    create_branch: opts.createBranch ?? false,
  });
}

export function removeGitWorktree(path: string, force = false): Promise<{ success: boolean; path: string }> {
  return post('/api/git/worktrees/remove', { path, force });
}

export interface WorktreeSession {
  session_id: string;
  original_cwd: string;
  worktree_path: string;
  worktree_name: string;
  worktree_branch: string;
  original_branch?: string | null;
  original_head_commit?: string | null;
  created_at: number;
}

export function getWorktreeSession(sessionId: string): Promise<{ session: WorktreeSession | null }> {
  return get<{ session: WorktreeSession | null }>(`/api/git/worktree-session?session_id=${encodeURIComponent(sessionId)}`);
}
