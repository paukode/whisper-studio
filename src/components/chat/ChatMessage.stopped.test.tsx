import { render } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { TurnBubble } from './TurnBubble';
import type { ChatMessage as ChatMessageType, ToolUseEvent } from '@/types/chat';

/**
 * A turn the user cut short commits its tool activity with the running steps
 * frozen as 'stopped'. Every renderer of that activity must show a terminal
 * mark for such a step and never a spinner: a committed message has no
 * stream left to wait for.
 */
describe('a stopped turn renders its stopped tool step without a spinner', () => {
  const stoppedGrep: ToolUseEvent = {
    toolId: 'ws_grep',
    toolName: 'ws_grep',
    input: { pattern: 'TODO' },
    result: '[Stopped by user]',
    status: 'stopped',
  };
  const doneRead: ToolUseEvent = {
    toolId: 'ws_read_file',
    toolName: 'ws_read_file',
    input: { path: '/repo/a.ts' },
    result: 'const a = 1;',
    status: 'complete',
  };
  const message = (toolUse: ToolUseEvent[]): ChatMessageType => ({
    role: 'assistant',
    content: '*(Stopped)*',
    timestamp: '2026-09-08T10:00:00.000Z',
    stopped: true,
    toolUse,
  });

  it('single step: the row shows the stop mark (ACTIVITY_MIN_RUN is 1, so it is an Activity row)', () => {
    const { container } = render(<TurnBubble rows={[message([stoppedGrep])]} indices={[1]} />);
    expect(container.querySelector('.trace-spinner')).toBeNull();
    expect(container.querySelector('.activity-spinner')).toBeNull();
    const row = container.querySelector('.activity-row.activity-stopped');
    expect(row).not.toBeNull();
    // The stop mark is a stroke icon with its own class, not a text glyph.
    expect(row?.querySelector('.activity-head svg.activity-mark-stopped')).not.toBeNull();
    expect(row?.querySelector('.activity-chip.muted')?.textContent).toBe('1 not run');
  });

  it('activity bundle: no spinner, the stop mark, what landed and what did not', () => {
    const { container } = render(<TurnBubble rows={[message([doneRead, stoppedGrep])]} indices={[1]} />);
    expect(container.querySelector('.trace-spinner')).toBeNull();
    expect(container.querySelector('.activity-spinner')).toBeNull();
    expect(container.querySelector('.activity-chip.live')).toBeNull();
    expect(container.querySelector('.activity-row.activity-stopped')).not.toBeNull();
    expect(container.querySelector('.activity-head svg.activity-mark-stopped')).not.toBeNull();
    expect(container.querySelector('.activity-label')?.textContent).toBe('Read a.ts, stopped');
    expect(container.querySelector('.activity-tail')?.textContent).toBe('Stopped by you');
  });

  // Rows keyed by the provider's call ids, with the verdict and exit code the
  // result brought: the errored step keeps its mark next to the stopped one.
  it('steps keyed by call ids: the errored one and the stopped one each show their mark, no spinner', () => {
    const failedRun: ToolUseEvent = {
      toolId: 'tu_run',
      toolName: 'ws_run_command',
      input: { command: 'npm test' },
      result: '2 failed\n(exit code 1)',
      status: 'error',
      isError: true,
      exitCode: 1,
    };
    const stoppedRead: ToolUseEvent = { ...stoppedGrep, toolId: 'tu_grep' };
    const { container } = render(<TurnBubble rows={[message([failedRun, stoppedRead])]} indices={[1]} />);
    expect(container.querySelector('.trace-spinner')).toBeNull();
    expect(container.querySelector('.activity-spinner')).toBeNull();
    expect(container.querySelector('.activity-chip.live')).toBeNull();
    // An error outranks the stop on the row, and opens it.
    expect(container.querySelector('.activity-row.activity-error.open')).not.toBeNull();
    expect(container.querySelector('.activity-head svg.activity-mark-error')).not.toBeNull();
    expect(container.querySelector('.activity-chip.err')?.textContent).toBe('exit 1');
    expect(container.querySelector('.activity-call.status-error')).not.toBeNull();
    const stoppedLine = container.querySelector('.activity-call.status-stopped');
    expect(stoppedLine?.querySelector('svg.activity-mark-stopped')).not.toBeNull();
    expect(stoppedLine?.querySelector('.activity-call-out')?.textContent).toBe('not run');
  });
});
