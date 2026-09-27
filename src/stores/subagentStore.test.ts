import { beforeEach, describe, expect, it, vi } from 'vitest';
import { hasRunningSubagent, subagentsOf, useSubagentStore } from './subagentStore';

describe('subagentStore', () => {
  beforeEach(() => {
    useSubagentStore.setState({ stops: {}, owners: {} });
  });

  it('registers and exposes a stop handler by team id', () => {
    const stop = vi.fn();
    useSubagentStore.getState().register('subagent-1', 'sess-a', stop);
    expect(useSubagentStore.getState().stops['subagent-1']).toBe(stop);
  });

  it('unregisters a stop handler and its owner together', () => {
    const stop = vi.fn();
    useSubagentStore.getState().register('subagent-1', 'sess-a', stop);
    useSubagentStore.getState().unregister('subagent-1');
    expect(useSubagentStore.getState().stops['subagent-1']).toBeUndefined();
    expect(hasRunningSubagent(useSubagentStore.getState(), 'sess-a')).toBe(false);
  });

  it('keeps multiple agents independent', () => {
    const a = vi.fn();
    const b = vi.fn();
    useSubagentStore.getState().register('a', 'sess-a', a);
    useSubagentStore.getState().register('b', 'sess-a', b);
    useSubagentStore.getState().unregister('a');
    expect(useSubagentStore.getState().stops['a']).toBeUndefined();
    expect(useSubagentStore.getState().stops['b']).toBe(b);
  });

  it('attributes each run to the session that started it', () => {
    const st = useSubagentStore.getState();
    st.register('in-a', 'sess-a', vi.fn());
    st.register('in-b', 'sess-b', vi.fn());
    st.register('in-draft', null, vi.fn());
    const now = useSubagentStore.getState();
    expect(subagentsOf(now, 'sess-a')).toEqual(['in-a']);
    expect(subagentsOf(now, null)).toEqual(['in-draft']);
    expect(hasRunningSubagent(now, 'sess-c')).toBe(false);
  });

  it('unregistering an unknown id is a no-op', () => {
    const before = useSubagentStore.getState().stops;
    useSubagentStore.getState().unregister('nope');
    expect(useSubagentStore.getState().stops).toBe(before);
  });
});
