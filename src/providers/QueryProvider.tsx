import React from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // Default freshness window. Polling queries (cron, skills) set their own
      // refetchInterval regardless; genuinely-static resources (config, plugins,
      // hooks, lsp-status, indexed-workspaces) override with a longer staleTime
      // at the call site so reopening a panel doesn't refetch needlessly.
      staleTime: 60_000,
      gcTime: 5 * 60_000,
      retry: 1,
      refetchOnWindowFocus: false,
    },
  },
});

export const QueryProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => (
  <QueryClientProvider client={queryClient}>
    {children}
  </QueryClientProvider>
);

export { queryClient };
