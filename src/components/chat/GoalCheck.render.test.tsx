import { act, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ChatMessage } from './ChatMessage';
import { GoalBanner } from './GoalBanner';
import { StreamingMessage } from './StreamingMessage';
import { summariseTool } from './ActivityRow';
import { useGoalStore } from '@/stores/goalStore';
import { getChatStore } from '@/stores/sessionRuntimes';
import type { ChatMessage as ChatMessageType, ToolUseEvent } from '@/types/chat';

/**
 * 'not_checked' means the goal judge could not run or could not be
 * understood. It must read differently from 'blocked' (the agent is stuck):
 * neutral, labelled "not checked", with the reason in view. And the status
 * the server sends while the judge runs ("Checking the goal on ...") arrives
 * after the reply has streamed, so it must show then too.
 */

const REASON =
  'Gemma 4 12B (Local) could not check the goal: no answer within 300 s. The goal stays set and is checked again when the next turn ends.';

const card = (verdict: string, status: ToolUseEvent['status']): ToolUseEvent => ({
  toolId: 'goal_eval',
  toolName: 'goal_eval',
  input: { verdict, feedback: REASON, confidence: 0, attempt: 0, cap: 8 },
  result: REASON,
  status,
});

const message = (tool: ToolUseEvent): ChatMessageType => ({
  role: 'assistant',
  content: '',
  timestamp: '2026-09-23T10:00:00.000Z',
  toolUse: [tool],
});

describe('goal card', () => {
  it('not_checked is neutral, labelled "not checked", and open on its reason', () => {
    const { container } = render(<ChatMessage message={message(card('not_checked', 'complete'))} index={1} />);
    const row = container.querySelector('.activity-row');
    expect(row?.classList.contains('activity-unchecked')).toBe(true);
    expect(row?.classList.contains('activity-error')).toBe(false);
    expect(container.querySelector('.activity-badge.unchecked')?.textContent).toBe('not checked');
    expect(container.querySelector('.activity-badge.ok')).toBeNull();
    expect(container.querySelector('.activity-badge.error')).toBeNull();
    expect(container.querySelector('.activity-check')).toBeNull();
    expect(container.querySelector('.activity-x')).toBeNull();
    // Open by default, with the reason in the step's summary.
    const detail = container.querySelector('.activity-step-detail')?.textContent ?? '';
    expect(detail.startsWith('not checked: Gemma 4 12B (Local) could not check the goal')).toBe(true);
    expect(container.querySelector('.activity-expand-btn')).not.toBeNull();
  });

  it('blocked still reads as an error', () => {
    const { container } = render(<ChatMessage message={message(card('blocked', 'error'))} index={1} />);
    expect(container.querySelector('.activity-row.activity-error')).not.toBeNull();
    expect(container.querySelector('.activity-badge.unchecked')).toBeNull();
  });

  it('names each verdict in words', () => {
    expect(summariseTool(card('not_checked', 'complete')).startsWith('not checked: ')).toBe(true);
    expect(summariseTool(card('not_achieved', 'error')).startsWith('not achieved: ')).toBe(true);
    expect(summariseTool(card('blocked', 'error')).startsWith('blocked: ')).toBe(true);
  });
});

describe('goal banner', () => {
  const sid = 'sess-goal-banner';

  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(new Response('null', { status: 404 }))));
    useGoalStore.getState().setGoal(sid, 'write the summary', true);
  });

  afterEach(() => {
    useGoalStore.getState().clearGoal(sid);
    vi.unstubAllGlobals();
  });

  it('shows not_checked as a neutral "not checked" chip with the reason on hover', () => {
    act(() => {
      useGoalStore.getState().applyEval(sid, { verdict: 'not_checked', feedback: REASON, attempt: 0, cap: 8 });
    });
    const { container } = render(<GoalBanner sessionId={sid} />);
    const chip = container.querySelector('.goal-banner-chip') as HTMLElement;
    expect(chip.textContent).toBe('not checked');
    expect(chip.getAttribute('title')).toBe(REASON);
    expect(chip.style.color).toBe('var(--text-muted)');
    // The goal is still active, so the banner stays.
    expect(screen.getByText('write the summary')).toBeInTheDocument();
  });

  it('keeps blocked in its own colour and label', () => {
    act(() => {
      useGoalStore.getState().applyEval(sid, { verdict: 'blocked', feedback: 'needs a key' });
    });
    const { container } = render(<GoalBanner sessionId={sid} />);
    const chip = container.querySelector('.goal-banner-chip') as HTMLElement;
    expect(chip.textContent).toBe('blocked');
    expect(chip.style.color).not.toBe('var(--text-muted)');
  });
});

describe('streaming status set after the reply', () => {
  const status = 'Checking the goal on Gemma 4 12B (Local)...';

  afterEach(() => {
    getChatStore(null).getState().setStreaming(false);
  });

  it('shows while the reply text is on screen', () => {
    act(() => {
      getChatStore(null).setState({ isStreaming: true, streamStatus: status, currentThinkingContent: '' });
    });
    render(<StreamingMessage content="Here is the summary." isStreaming />);
    expect(screen.getByText(status)).toBeInTheDocument();
    expect(screen.getByText('Here is the summary.')).toBeInTheDocument();
  });

  it('shows after a round that only thought, instead of "Thinking..."', () => {
    act(() => {
      getChatStore(null).setState({
        isStreaming: true,
        streamStatus: status,
        currentThinkingContent: 'I considered the goal.',
      });
    });
    render(<StreamingMessage content="" isStreaming />);
    expect(screen.getByText(status)).toBeInTheDocument();
    expect(screen.queryByText('Thinking…')).toBeNull();
  });
});
