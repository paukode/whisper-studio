import { render } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { ChatMessage } from './ChatMessage';
import type { ChatMessage as ChatMessageType, ToolUseEvent } from '@/types/chat';

/**
 * A turn the user cut short commits its tool activity with the running steps
 * frozen as 'stopped'. Every renderer of that activity must show a terminal
 * glyph for such a step and never a spinner: a committed message has no
 * stream left to wait for.
 */
describe('ChatMessage renders a stopped tool step without a spinner', () => {
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

  it('single step: the row shows the stopped glyph (ACTIVITY_MIN_RUN is 1, so it is an Activity row)', () => {
    const { container } = render(<ChatMessage message={message([stoppedGrep])} index={1} />);
    expect(container.querySelector('.trace-spinner')).toBeNull();
    expect(container.querySelector('.activity-spinner')).toBeNull();
    expect(container.querySelector('.activity-stopped, .trace-stopped, .activity-badge.stopped')).not.toBeNull();
  });

  it('activity bundle: no spinner, a stopped badge, and the step marked stopped', () => {
    const { container } = render(<ChatMessage message={message([doneRead, stoppedGrep])} index={1} />);
    expect(container.querySelector('.trace-spinner')).toBeNull();
    expect(container.querySelector('.activity-spinner')).toBeNull();
    expect(container.querySelector('.activity-badge.running')).toBeNull();
    expect(container.querySelector('.activity-badge.stopped')).not.toBeNull();
    expect(container.querySelector('.activity-row.activity-stopped')).not.toBeNull();
  });

  // Rows keyed by the provider's call ids, with the verdict and exit code the
  // result brought: the errored step keeps its glyph next to the stopped one.
  it('steps keyed by call ids: the errored one and the stopped one each show their glyph, no spinner', () => {
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
    const { container } = render(<ChatMessage message={message([failedRun, stoppedRead])} index={1} />);
    expect(container.querySelector('.trace-spinner')).toBeNull();
    expect(container.querySelector('.activity-spinner')).toBeNull();
    expect(container.querySelector('.activity-badge.running')).toBeNull();
    expect(container.querySelector('.activity-badge.error')).not.toBeNull();
    expect(container.querySelector('.activity-badge.stopped')).not.toBeNull();
    expect(container.querySelector('.activity-step.status-error')).not.toBeNull();
    expect(container.querySelector('.activity-step.status-stopped')).not.toBeNull();
  });
});
