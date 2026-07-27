import { useQuery } from '@tanstack/react-query';
import { get } from '@/api/client';

/**
 * Recent workspace paths, newest first — the single source of truth for
 * BOTH the toolbar Workspace dropdown and the Connect Workspace dialog.
 *
 * They share the react-query cache key ['workspace-recent'], so every consumer
 * must read the same shape — this hook is the only queryFn for that key.
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
