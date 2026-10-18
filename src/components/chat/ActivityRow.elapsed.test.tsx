/**
 * The running group's elapsed chip counts from the arrival of its calls,
 * which the live row keeps (ToolUseEvent.startedAt). It used to keep its
 * start in the row's own state, so every remount (the live tail moving to a
 * bubble of its own after a mid-turn message, a session switch) restarted a
 * 45 s command at 0 s.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, render } from '@testing-library/react';
import { ActivityRow } from './ActivityRow';
import type { ToolUseEvent } from '@/types/chat';

const chip = (container: HTMLElement) => container.querySelector('.activity-chip.live')?.textContent;

const running = (startedAt?: number): ToolUseEvent => ({
  toolId: 'tu_run',
  toolName: 'ws_run_command',
  input: { command: 'venv/bin/python -m pytest tests/' },
  status: 'running',
  ...(startedAt !== undefined ? { startedAt } : {}),
});

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date('2026-10-18T12:00:00Z'));
});

afterEach(() => {
  vi.useRealTimers();
});

describe('ActivityRow elapsed chip', () => {
  it('counts from the arrival the live row keeps, across a remount', () => {
    const tools = [running(Date.now())];
    act(() => { vi.advanceTimersByTime(45_000); });

    const first = render(<ActivityRow tools={tools} />);
    act(() => { vi.advanceTimersByTime(10); });
    expect(chip(first.container)).toBe('45 s');
    first.unmount();

    act(() => { vi.advanceTimersByTime(2_000); });
    const again = render(<ActivityRow tools={tools} />);
    act(() => { vi.advanceTimersByTime(10); });
    expect(chip(again.container)).toBe('47 s');
  });

  it('counts from when the group started running here when no call carries an arrival', () => {
    const { container } = render(<ActivityRow tools={[running()]} />);
    act(() => { vi.advanceTimersByTime(3_000); });
    expect(chip(container)).toBe('3 s');
  });
});
