import { describe, it, expect, beforeEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useCronUnreadStore, useCronUnreadFor } from './cronUnreadStore';
import type { CronRecentRun } from '@/api/cron';

function run(sid: string, status: string, agoMs = 1000): CronRecentRun {
  return {
    run_id: `${sid}-${status}-${agoMs}-${Math.round(Math.random() * 1e6)}`,
    job_id: 'j',
    job_name: 'job',
    session_id: sid,
    status,
    started_at: new Date(Date.now() - agoMs).toISOString(),
  };
}

describe('useCronUnreadFor', () => {
  beforeEach(() => {
    useCronUnreadStore.setState({ runs: [], lastSeen: {} });
  });

  // Regression for React #185: a selector returning a fresh object each call
  // under zustand v5 throws "Maximum update depth exceeded" on mount. If this
  // renders without throwing, the snapshot-stable selector is intact.
  it('mounts without an infinite render loop', () => {
    expect(() => renderHook(() => useCronUnreadFor('sess-1'))).not.toThrow();
  });

  it('starts at zero', () => {
    const { result } = renderHook(() => useCronUnreadFor('sess-1'));
    expect(result.current).toEqual({ count: 0, hasFailure: false });
  });

  it('counts unseen runs for the session and flags failures', () => {
    const { result } = renderHook(() => useCronUnreadFor('sess-1'));
    act(() => {
      useCronUnreadStore.getState().setRuns([
        run('sess-1', 'ok'),
        run('sess-1', 'failed'),
        run('other', 'ok'), // different session — ignored
      ]);
    });
    expect(result.current.count).toBe(2);
    expect(result.current.hasFailure).toBe(true);
  });

  it('markSeen clears the badge for that session', () => {
    const { result } = renderHook(() => useCronUnreadFor('sess-1'));
    act(() => {
      useCronUnreadStore.getState().setRuns([run('sess-1', 'ok')]);
    });
    expect(result.current.count).toBe(1);
    act(() => {
      useCronUnreadStore.getState().markSeen('sess-1');
    });
    expect(result.current.count).toBe(0);
  });

  it('returns a memoized object stable across pure re-renders', () => {
    // With no store change, a re-render must return the SAME object identity —
    // the useMemo guarantee. (A selector that built a fresh object each call
    // would instead loop, which is the #185 regression this guards.)
    const { result, rerender } = renderHook(() => useCronUnreadFor('sess-1'));
    const first = result.current;
    rerender();
    expect(result.current).toBe(first);
  });
});
