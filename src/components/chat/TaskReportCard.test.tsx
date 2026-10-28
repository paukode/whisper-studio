/**
 * A finished background command reads like an agent report: one collapsed
 * line with the command and its outcome, opening to the end of its output.
 */
import { fireEvent, render } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { TaskReportCard, durationText } from './TaskReportCard';
import { ChatMessage } from './ChatMessage';
import type { TaskReportPayload } from '@/types/chat';

const report = (over: Partial<TaskReportPayload> = {}): TaskReportPayload => ({
  task_id: 't1',
  command: 'make release',
  status: 'completed',
  exit_code: 0,
  duration_ms: (1 * 3600 + 43 * 60) * 1000,
  output_tail: 'built dist/App.dmg',
  timestamp: '2026-10-28T17:03:22Z',
  ...over,
});

describe('TaskReportCard', () => {
  it('shows the command, the outcome and the run time, collapsed', () => {
    const view = render(<TaskReportCard report={report()} />);
    expect(view.getByText('make release')).toBeTruthy();
    expect(view.getByText('finished')).toBeTruthy();
    expect(view.getByText('1h 43m')).toBeTruthy();
    expect(view.queryByText(/built dist/)).toBeNull();
    fireEvent.click(view.getByRole('button'));
    expect(view.getByText(/built dist\/App\.dmg/)).toBeTruthy();
  });

  it('says a failure with its exit code', () => {
    const view = render(<TaskReportCard report={report({ status: 'failed', exit_code: 2 })} />);
    expect(view.getByText('failed · exit 2')).toBeTruthy();
  });

  it('is what the chat draws for a task_report row', () => {
    const view = render(
      <ChatMessage message={{ role: 'task_report', content: '', timestamp: 't', taskReport: report() }} index={3} />,
    );
    expect(view.getByTestId('task-report-card')).toBeTruthy();
  });

  it('formats run times', () => {
    expect(durationText(40_000)).toBe('40s');
    expect(durationText(125_000)).toBe('2m 5s');
    expect(durationText(null)).toBe('');
  });
});
