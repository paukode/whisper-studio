import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useCallback, useRef } from 'react';
import { getGitStatus, type GitStatusBar } from '@/api/git';
import { ApiError } from '@/types/api';
import { useGitChanged } from './useGitChanged';

export interface GitStatusBarState {
  status: GitStatusBar | null;
  /** git's own message when the status read failed (HTTP 5xx or the
   *  server unreachable). A 4xx (no workspace, not a git repository) is
   *  not an error: the bar just has no git segment. */
  error: string | null;
}

/** The message to show for a failed git query, or null when the failure
 *  only means there is no repository to show (a 4xx verdict). */
export function gitQueryErrorMessage(error: unknown): string | null {
  if (!error) return null;
  if (error instanceof ApiError && error.status >= 400 && error.status < 500) return null;
  return error instanceof Error ? error.message : String(error);
}

/** Live git status for the app status bar.
 *
 * Shares react-query key ['git-status-bar'] and subscribes to the same
 * /api/git/events SSE the GitChangesPanel uses (the backend GitFileWatcher
 * pushes a `git-changed` event within ~1s of any HEAD/config/branch-ref
 * change). Only enabled when a workspace is connected; a non-git workspace
 * yields a 4xx that react-query surfaces as no data (the bar hides its git
 * segment). A git failure is returned as ``error`` so the bar shows it. */
export function useGitStatusBar(enabled: boolean): GitStatusBarState {
  const queryClient = useQueryClient();
  const { data, error } = useQuery({
    queryKey: ['git-status-bar'],
    queryFn: getGitStatus,
    enabled,
    retry: false,
  });

  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const refresh = useCallback(() => {
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => {
      debounceRef.current = null;
      void queryClient.invalidateQueries({ queryKey: ['git-status-bar'] });
    }, 200);
  }, [queryClient]);

  // Shares one /api/git/events connection with the changes and terminal
  // panels; only active while a workspace is connected.
  useGitChanged(refresh, enabled);

  return { status: data ?? null, error: enabled ? gitQueryErrorMessage(error) : null };
}
