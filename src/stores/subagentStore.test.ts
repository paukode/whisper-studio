import { beforeEach, describe, expect, it, vi } from 'vitest';
import { useSubagentStore } from './subagentStore';

describe('subagentStore', () => {
  beforeEach(() => {
    useSubagentStore.setState({ stops: {} });
  });

  it('registers and exposes a stop handler by team id', () => {
    const stop = vi.fn();
    useSubagentStore.getState().register('subagent-1', stop);
    expect(useSubagentStore.getState().stops['subagent-1']).toBe(stop);
  });

  it('unregisters a stop handler', () => {
    const stop = vi.fn();
    useSubagentStore.getState().register('subagent-1', stop);
    useSubagentStore.getState().unregister('subagent-1');
    expect(useSubagentStore.getState().stops['subagent-1']).toBeUndefined();
  });

  it('keeps multiple agents independent', () => {
    const a = vi.fn();
    const b = vi.fn();
    useSubagentStore.getState().register('a', a);
    useSubagentStore.getState().register('b', b);
    useSubagentStore.getState().unregister('a');
    expect(useSubagentStore.getState().stops['a']).toBeUndefined();
    expect(useSubagentStore.getState().stops['b']).toBe(b);
  });

  it('unregistering an unknown id is a no-op', () => {
    const before = useSubagentStore.getState().stops;
    useSubagentStore.getState().unregister('nope');
    expect(useSubagentStore.getState().stops).toBe(before);
  });
});
