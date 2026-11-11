import { describe, expect, it } from 'vitest';
import { agentStatusOf, outcomeOf, settledStatus, stopReasonOf } from './agentOutcome';

/**
 * One vocabulary for how an agent run is doing or ended: every row, card,
 * feed line and record readout reads it here, so the same run never shows
 * "done" in one place and "stopped at the turn limit" in another.
 */
describe('agentOutcome', () => {
  it('a limit stop reads as stopped, with the limit named, wherever it is shown', () => {
    expect(settledStatus('stopped', 'turn_limit')).toBe('stopped');
    const row = outcomeOf('stopped', 'turn_limit');
    expect(row.line).toBe('stopped at the turn limit');
    expect(row.chip).toBe('turn limit');
    expect(row.tone).toBe('neutral');
  });

  it('reads records written before a limit stop became stopped the same way', () => {
    // A turn_limit phase, a "completed" status with a limit reason, and a
    // workflow call's "incomplete" all mean the run stopped at a limit.
    expect(agentStatusOf('turn_limit')).toBe('stopped');
    expect(agentStatusOf('incomplete')).toBe('stopped');
    expect(settledStatus('completed', 'deadline')).toBe('stopped');
    expect(stopReasonOf('turn_limit')).toBe('turn_limit');
    expect(outcomeOf('turn_limit').line).toBe(outcomeOf('stopped', 'turn_limit').line);
  });

  it('names the reason a record left out from how the run ended', () => {
    expect(stopReasonOf('completed')).toBe('completed');
    expect(stopReasonOf('failed')).toBe('error');
    expect(stopReasonOf('stopped')).toBe('cancelled');
    expect(stopReasonOf('stopped', 'cost_cap')).toBe('cost_cap');
  });

  it('a running agent shows its status, not a reason left from an earlier run', () => {
    expect(outcomeOf('running', 'turn_limit').line).toBe('running');
    expect(outcomeOf('tool_call').line).toBe('running');
    expect(outcomeOf('pending').line).toBe('starting');
  });

  it('a finished run takes the done tone and a failure the error tone', () => {
    expect(outcomeOf('completed', 'completed')).toEqual({ chip: 'finished', line: 'done', tone: 'done' });
    expect(outcomeOf('failed', 'error').tone).toBe('err');
  });
});
