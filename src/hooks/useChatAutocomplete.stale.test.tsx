/**
 * Regression test for the file-search stale-response race in
 * useChatAutocomplete.
 *
 * The `@file:`/`/file:` search is debounced (200ms) and fetches over the
 * network. A slow response for an earlier query could resolve AFTER a newer
 * query and overwrite the newer, correct results. searchFiles now stamps each
 * call with a monotonic request id and drops any response whose id is no
 * longer current. This test fires two searches, resolves them OUT OF ORDER
 * (newer first, then the stale older one), and asserts the stale response is
 * discarded.
 */
import { renderHook, act } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// searchFiles calls get() from the api client — mock it so we control exactly
// when each fetch resolves. Each call parks its resolver in `deferreds`.
const { getMock, deferreds } = vi.hoisted(() => {
  const deferreds: Array<(v: unknown) => void> = [];
  const getMock = vi.fn(
    () => new Promise((resolve) => { deferreds.push(resolve); }),
  );
  return { getMock, deferreds };
});
vi.mock('@/api/client', () => ({ get: getMock }));

import { useChatAutocomplete } from './useChatAutocomplete';

function makeHook() {
  return renderHook(() =>
    useChatAutocomplete({
      text: '',
      setText: () => {},
      textareaRef: { current: null },
      slashCommands: [],
      skills: [],
      mcpServers: [],
    }),
  );
}

describe('useChatAutocomplete — file-search stale-response guard', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    getMock.mockClear();
    deferreds.length = 0;
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('drops an out-of-order (stale) response and keeps the newer results', async () => {
    const { result } = makeHook();

    // Search #1 ("a") — fire its debounced fetch (reqId 1, still pending).
    act(() => { result.current.computeAutocomplete('@file:a', 7); });
    act(() => { vi.advanceTimersByTime(200); });

    // Search #2 ("ab") — fire its debounced fetch (reqId 2, still pending).
    act(() => { result.current.computeAutocomplete('@file:ab', 8); });
    act(() => { vi.advanceTimersByTime(200); });

    expect(getMock).toHaveBeenCalledTimes(2);
    expect(deferreds).toHaveLength(2);

    // Resolve the NEWER fetch (#2) first — its results should show.
    await act(async () => {
      deferreds[1]({ results: [{ path: 'ab-result.md' }] });
      await Promise.resolve();
    });
    expect(result.current.acItems.some((i) => i.name === 'ab-result.md')).toBe(true);

    // Now resolve the STALE older fetch (#1). It must NOT overwrite #2.
    await act(async () => {
      deferreds[0]({ results: [{ path: 'a-result.md' }] });
      await Promise.resolve();
    });
    expect(result.current.acItems.some((i) => i.name === 'a-result.md')).toBe(false);
    expect(result.current.acItems.some((i) => i.name === 'ab-result.md')).toBe(true);
  });

  it('applies the response when it is still the current request', async () => {
    const { result } = makeHook();

    act(() => { result.current.computeAutocomplete('@file:main', 10); });
    act(() => { vi.advanceTimersByTime(200); });
    expect(getMock).toHaveBeenCalledTimes(1);

    await act(async () => {
      deferreds[0]({ results: [{ path: 'src/main.ts' }] });
      await Promise.resolve();
    });
    expect(result.current.acItems.some((i) => i.name === 'src/main.ts')).toBe(true);
  });
});
