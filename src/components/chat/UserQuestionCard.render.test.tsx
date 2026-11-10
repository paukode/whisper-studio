import { render, screen } from '@testing-library/react';
import { describe, it, expect } from 'vitest';
import { UserQuestionGroupCard } from './UserQuestionCard';
import type { ChatMessage as ChatMessageType } from '@/types/chat';

/**
 * Regression for the "Something went wrong" chat crash: a model asked
 * ask_user_question with `options` missing (not an array), which threw
 * `options.some is not a function` inside withBrowseOption and took down the
 * whole conversation via the error boundary. The card must render regardless.
 */
describe('UserQuestionGroupCard — malformed options render, never crash', () => {
  const makeMessage = (options: unknown): ChatMessageType =>
    ({
      role: 'assistant',
      content: '',
      userQuestions: [
        { question: 'Which README should I read?', options, toolUseId: 't1' },
      ],
    } as unknown as ChatMessageType);

  it('renders the question when options is undefined', () => {
    render(<UserQuestionGroupCard message={makeMessage(undefined)} />);
    expect(screen.getByText('Which README should I read?')).toBeTruthy();
  });

  it('renders when options is a string (model sent the wrong shape)', () => {
    render(<UserQuestionGroupCard message={makeMessage('a, b, c')} />);
    expect(screen.getByText('Which README should I read?')).toBeTruthy();
  });

  it('still offers a way to answer (free-text input) with no options', () => {
    const { container } = render(<UserQuestionGroupCard message={makeMessage(undefined)} />);
    expect(container.querySelector('input')).toBeTruthy();
  });
});

describe('UserQuestionGroupCard: closed by a message the user sent instead', () => {
  it('says it was not answered and takes no answer', () => {
    const message = {
      role: 'assistant',
      content: '',
      userQuestions: [
        { question: 'Which fruit?', options: ['Apple', 'Banana', 'Cherry'], toolUseId: 't1', skipped: true },
      ],
    } as unknown as ChatMessageType;
    const { container } = render(<UserQuestionGroupCard message={message} />);

    expect(screen.getByText('Not answered: you sent a new message instead.')).toBeTruthy();
    expect((screen.getByText('Apple').closest('button') as HTMLButtonElement).disabled).toBe(true);
    expect((container.querySelector('input') as HTMLInputElement).disabled).toBe(true);
  });
});
