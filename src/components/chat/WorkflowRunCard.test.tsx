/** The run card lists every agent of a finished run (from the journal the
 *  snapshot carries) and reads how many ran at once and per phase. */
import { render } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { WorkflowRunCard, phaseBreakdown } from './WorkflowRunCard';
import { useWorkflowStore } from '@/stores/workflowStore';

vi.mock('@/api/workflows', () => ({
  getRun: vi.fn(),
  stopRun: vi.fn(),
  subscribeRun: vi.fn(() => () => {}),
}));

describe('WorkflowRunCard', () => {
  beforeEach(() => {
    useWorkflowStore.setState({ runs: {}, activity: {} });
  });

  it('keeps finished agents listed with the peak and the phase counts', () => {
    useWorkflowStore.getState().upsertRun({
      run_id: 'r1', name: 'Audit', status: 'done', agents_spawned: 3, tokens_in: 0, tokens_out: 0, cost_usd: 0, cap_reached: false, error: '',
      agent_calls: [
        { seq: 0, phase: 'review', label: 'one', status: 'completed', agent_id: 'a0', started_at: 0, finished_at: 10 },
        { seq: 1, phase: 'review', label: 'two', status: 'completed', agent_id: 'a1', started_at: 1, finished_at: 9 },
        { seq: 2, phase: 'fix', label: 'three', status: 'completed', agent_id: 'a2', started_at: 10, finished_at: 12 },
      ],
    });
    const { getAllByTestId, getByText } = render(<WorkflowRunCard runId="r1" />);
    expect(getAllByTestId('agent-row')).toHaveLength(3);
    expect(getByText(/at most 2 at once/)).toBeTruthy();
    expect(getByText('review 2 · fix 1')).toBeTruthy();
  });

  it('phaseBreakdown is empty when the script names no phases', () => {
    expect(phaseBreakdown([''], { '': 4 })).toBe('');
    expect(phaseBreakdown(['', 'fix'], { '': 1, fix: 2 })).toBe('no phase 1 · fix 2');
  });
});
