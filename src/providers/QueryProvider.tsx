import React from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

// The backend is on 127.0.0.1, so the browser's online/offline heuristics say
// nothing useful about whether it is reachable. Under react-query's default
// `networkMode: 'online'` a fetch that rejects with a TypeError — which is how
// a dropped connection surfaces — parks the query in `fetchStatus: 'paused'`
// rather than failing it. A paused query reports isLoading false, isError
// false, and no data, so every branch of a component's render is false and the
// panel goes silently blank with nothing to retry, waiting on an online event
// that never comes because we never actually went offline. 'always' makes a
// dropped request an ordinary error: retried under the policy below, then
// surfaced to the component.
const NETWORK_MODE = 'always';

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // Default freshness window. Polling queries (cron, skills) set their own
      // refetchInterval regardless; genuinely-static resources (config, plugins,
      // hooks, lsp-status, indexed-workspaces) override with a longer staleTime
      // at the call site so reopening a panel doesn't refetch needlessly.
      staleTime: 60_000,
      gcTime: 5 * 60_000,
      networkMode: NETWORK_MODE,
      // A 4xx is a verdict — the path is bad, the workspace is gone — and
      // asking again changes nothing. Transport failures and 5xx are worth
      // another attempt or two, since a request that lost a race with a
      // closing keep-alive socket succeeds on a fresh one.
      retry: (failureCount, error) => {
        const status = (error as { status?: number } | null)?.status;
        if (typeof status === 'number' && status >= 400 && status < 500) return false;
        return failureCount < 2;
      },
      refetchOnWindowFocus: false,
    },
    mutations: {
      networkMode: NETWORK_MODE,
    },
  },
});

export const QueryProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => (
  <QueryClientProvider client={queryClient}>
    {children}
  </QueryClientProvider>
);

export { queryClient };
