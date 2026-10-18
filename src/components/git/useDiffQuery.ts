import { useQuery, useQueryClient, type QueryKey } from '@tanstack/react-query';
import type { DiffBody } from './DiffDialog';

/** The data, or the body a diff dialog shows while there is none to draw.
 *  `pending` is a skeleton while the fetch runs and the failure (with a
 *  Retry) once it fails or stalls; it is null only when `data` is in. */
export type DiffQueryState<T> =
  | { data: T | undefined; error: Error | null; pending: DiffBody }
  | { data: T; error: null; pending: null };

/**
 * The fetch behind a diff dialog (GitDiffViewer, EditDiffViewer), so no
 * state of the query leaves the dialog body blank.
 */
export function useDiffQuery<T>(queryKey: QueryKey, queryFn: () => Promise<T>): DiffQueryState<T> {
  const queryClient = useQueryClient();
  const { data, error, isPending, isFetching, fetchStatus } = useQuery({
    queryKey,
    queryFn,
    // A diff is a snapshot; refetching on window focus would swap the
    // content out from under someone mid-read.
    refetchOnWindowFocus: false,
  });

  // A retry only proceeds while the window has focus (query-core gates it on
  // focusManager as well as the network), so a first attempt that fails just
  // as the user tabs away leaves the query parked in `paused`: pending, not
  // fetching, no error, no data. That combination used to satisfy no branch
  // of the dialog at all and it sat blank with nothing to act on. Treat a
  // paused query as a stall the reader can retry out of.
  const stalled = isPending && fetchStatus === 'paused';

  // Not refetch(): with no data cached, Query.fetch sees a non-idle
  // fetchStatus and hands back the parked retryer promise instead of
  // starting anything, so the button would do nothing in exactly the case
  // it exists for. Resetting drops the stuck retryer and refetches clean.
  const retry = () => {
    void queryClient.resetQueries({ queryKey, exact: true });
  };

  if (isPending && isFetching) return { data, error, pending: { kind: 'loading' } };
  if (error || stalled) {
    const message = stalled && !error
      ? 'Lost the connection to the backend before the diff arrived.'
      : error instanceof Error
        ? error.message
        : 'Could not load diff.';
    return { data, error, pending: { kind: 'error', message, onRetry: retry, retrying: isFetching } };
  }
  if (data === undefined) return { data, error, pending: { kind: 'loading' } };
  return { data, error: null, pending: null };
}
