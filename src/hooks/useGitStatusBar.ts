import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useCallback, useRef } from 'react';
import { getGitStatus, type GitStatusBar } from '@/api/git';

/** Live git status for the app status bar.
 *
 * Shares react-query key ['git-status-bar'] and subscribes to the same
 * /api/git/events SSE the GitChangesPanel uses (the backend GitFileWatcher
 * pushes a `git-changed` event within ~1s of any HEAD/config/branch-ref
 * change). Only enabled when a workspace is connected; a non-git workspace
 * yields a 4xx that react-query surfaces as no data (the bar hides its git
 * segment). */
export function useGitStatusBar(enabled: boolean): GitStatusBar | null {
  const queryClient = useQueryClient();
  const { data } = useQuery({
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

  useEffect(() => {
    if (!enabled || typeof EventSource === 'undefined') return;
    const es = new EventSource('/api/git/events');
    es.onmessage = (e) => {
      try {
        const parsed = JSON.parse(e.data) as { type?: string };
        if (parsed.type === 'git-changed') refresh();
      } catch {
        /* malformed frame — ignore */
      }
    };
    return () => es.close();
  }, [enabled, refresh]);

  return data ?? null;
}
