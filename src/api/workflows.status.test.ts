import { describe, expect, it } from 'vitest';
import { endedEarly, isTerminalRun } from './workflows';

/** One list of the statuses a workflow run does not leave, for every card. */
describe('workflow run status', () => {
  it('a running run, or a status it does not know, is not over', () => {
    expect(isTerminalRun('running')).toBe(false);
    expect(isTerminalRun(undefined)).toBe(false);
    expect(isTerminalRun('starting')).toBe(false);
  });

  it('done ends with its result; failed, stopped and stale end early', () => {
    expect(isTerminalRun('done')).toBe(true);
    expect(endedEarly('done')).toBe(false);
    for (const s of ['failed', 'stopped', 'stale']) {
      expect(isTerminalRun(s)).toBe(true);
      expect(endedEarly(s)).toBe(true);
    }
  });
});
