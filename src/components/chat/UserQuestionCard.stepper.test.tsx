import { fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { UserQuestionGroupCard } from './UserQuestionCard';
import type { ChatMessage as ChatMessageType } from '@/types/chat';
import { getActiveChatStore } from '@/stores/sessionRuntimes';

/**
 * Two or more questions step through one card: a tab per question, Back and
 * Next between them, one Submit at the end. The regression this pins: picking
 * an answer on the first of two questions used to lock the whole card, so the
 * second question could never be answered.
 */
type Answer = { tool_use_id: string; content: string };

const twoQuestions = (): ChatMessageType => ({
  role: 'assistant',
  content: '',
  timestamp: '2026-10-16T19:40:00.000Z',
  userQuestions: [
    { question: 'What should the folder be called?', options: ['capacity-planner', 'util-forecast'], toolUseId: 'tu_1' },
    { question: 'Which frontend stack?', options: ['React', 'Cloudscape'], toolUseId: 'tu_2' },
  ],
}) as ChatMessageType;

const oneQuestion = (): ChatMessageType => ({
  role: 'assistant',
  content: '',
  timestamp: '2026-10-16T19:40:00.000Z',
  userQuestions: [{ question: 'Which DB?', options: ['sqlite', 'pg'], toolUseId: 'tu_solo' }],
}) as ChatMessageType;

describe('UserQuestionGroupCard stepper', () => {
  const submits: Answer[][] = [];
  const onSubmit = (e: Event) => {
    submits.push(((e as CustomEvent).detail as { answers: Answer[] }).answers);
  };

  beforeEach(() => {
    submits.length = 0;
    window.addEventListener('whisper-submit-answer', onSubmit);
  });

  afterEach(() => {
    window.removeEventListener('whisper-submit-answer', onSubmit);
  });

  it('shows one question at a time, with a tab per question', () => {
    render(<UserQuestionGroupCard message={twoQuestions()} />);

    expect(screen.getByText('What should the folder be called?')).toBeInTheDocument();
    expect(screen.queryByText('Which frontend stack?')).not.toBeInTheDocument();
    const tabs = screen.getAllByRole('tab');
    expect(tabs.map((t) => t.textContent)).toEqual(['Question 1', 'Question 2']);
    expect(tabs[0]).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByRole('button', { name: 'Back' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Next' })).toBeEnabled();
  });

  it('answering the first of two questions submits nothing and leaves the second answerable', () => {
    render(<UserQuestionGroupCard message={twoQuestions()} />);

    fireEvent.click(screen.getByRole('button', { name: 'capacity-planner' }));
    expect(submits).toHaveLength(0);
    // The second question stays open (this is the lock-up the field report showed).
    expect(screen.getByRole('button', { name: 'util-forecast' })).toBeEnabled();

    fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    expect(screen.getByText('Which frontend stack?')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'React' })).toBeEnabled();
    expect(screen.getByRole('textbox', { name: 'Type your own answer' })).toBeEnabled();
  });

  it('Back returns to an earlier question with its answer kept, and a tab jumps directly', () => {
    render(<UserQuestionGroupCard message={twoQuestions()} />);

    fireEvent.click(screen.getByRole('button', { name: 'capacity-planner' }));
    fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    fireEvent.click(screen.getByRole('button', { name: 'Back' }));
    expect(screen.getByRole('button', { name: 'capacity-planner' })).toHaveClass('selected');
    // The answered tab carries a check; the open one does not.
    const tabs = screen.getAllByRole('tab');
    expect(tabs[0]).toHaveClass('done');
    expect(tabs[1]).not.toHaveClass('done');

    fireEvent.click(tabs[1]);
    expect(screen.getByText('Which frontend stack?')).toBeInTheDocument();
    expect(tabs[1]).toHaveAttribute('aria-selected', 'true');
  });

  it('Submit waits for every answer, then sends them all in one continuation', () => {
    const message = twoQuestions();
    getActiveChatStore().getState().setMessages([message]);
    render(<UserQuestionGroupCard message={message} />);

    fireEvent.click(screen.getByRole('button', { name: 'capacity-planner' }));
    fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    // Only the last step carries Submit, and it waits for the open question.
    expect(screen.queryByRole('button', { name: 'Next' })).not.toBeInTheDocument();
    const submit = screen.getByRole('button', { name: 'Answer all 2 to submit' });
    expect(submit).toBeDisabled();

    fireEvent.click(screen.getByRole('button', { name: 'React' }));
    expect(submits).toHaveLength(0);
    const ready = screen.getByRole('button', { name: 'Submit 2 answers' });
    expect(ready).toBeEnabled();
    fireEvent.click(ready);

    expect(submits).toEqual([[
      { tool_use_id: 'tu_1', content: 'capacity-planner' },
      { tool_use_id: 'tu_2', content: 'React' },
    ]]);
    expect(screen.getByRole('button', { name: 'Submitted' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'React' })).toBeDisabled();
    // Both questions are marked answered in the store, so a reload keeps the card closed.
    const stored = getActiveChatStore().getState().messages[0];
    expect(stored.userQuestions?.map((q) => q.answered)).toEqual([true, true]);
  });

  it('a typed answer counts like a picked one', () => {
    render(<UserQuestionGroupCard message={twoQuestions()} />);

    fireEvent.click(screen.getByRole('button', { name: 'capacity-planner' }));
    fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    const input = screen.getByRole('textbox', { name: 'Type your own answer' });
    fireEvent.change(input, { target: { value: 'Svelte' } });
    fireEvent.keyDown(input, { key: 'Enter' });

    expect(screen.getByText('Answered: Svelte')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Submit 2 answers' }));
    expect(submits[0][1]).toEqual({ tool_use_id: 'tu_2', content: 'Svelte' });
  });

  it('a single question submits on the first pick, with no steps', () => {
    render(<UserQuestionGroupCard message={oneQuestion()} />);

    expect(screen.queryByRole('tab')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Next' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'pg' }));
    expect(submits).toEqual([[{ tool_use_id: 'tu_solo', content: 'pg' }]]);
  });
});
