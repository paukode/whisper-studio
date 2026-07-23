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
