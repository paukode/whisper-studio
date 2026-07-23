import { useQuery } from '@tanstack/react-query';
import { get } from '@/api/client';

/**
 * Recent workspace paths, newest first — the single source of truth for
 * BOTH the toolbar Workspace dropdown and the Connect Workspace dialog.
 *
 * They share the react-query cache key, so the cached shape must be
 * identical for every consumer: previously each component had its own
 * queryFn under the same key with different shapes (string[] vs
 * {path,name}[]), and whichever ran first poisoned the cache for the
 * other — opening the toolbar dropdown then the dialog crashed it on
 * `ws.path.split` of undefined.
 */
export function useRecentWorkspaces(enabled: boolean): string[] {
  const { data } = useQuery({
    queryKey: ['workspace-recent'],
    queryFn: async () => {
      const data = await get<{ recent: string[] }>('/api/workspace/recent');
      return Array.isArray(data.recent) ? data.recent : [];
    },
    enabled,
    // Refetch on each open so a just-connected workspace appears.
    staleTime: 0,
  });
  return data ?? [];
}
