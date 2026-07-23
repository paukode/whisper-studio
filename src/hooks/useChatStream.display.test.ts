import { describe, expect, it } from 'vitest';
import { buildDisplayQuestion } from './useChatStream';
import { SKILL_MENTION_PLACEHOLDER } from '@/components/chat/chatInputConstants';

// Regression: a bare `@skill` send carries a synthetic anchor sentence as its
// payload (so the API message is non-empty and the forced tool call has
// something to anchor on), but the bubble must show only what the user typed.
describe('buildDisplayQuestion', () => {
  it('renders a bare skill mention as just @skill, hiding the placeholder payload', () => {
    expect(
      buildDisplayQuestion(SKILL_MENTION_PLACEHOLDER, {
        forceSkill: 'summarize',
        displayText: '',
      }),
    ).toBe('@summarize');
  });

  it('renders mention plus the user text when they typed some', () => {
    expect(
      buildDisplayQuestion('with action items', {
        forceSkill: 'meeting_notes',
        displayText: 'with action items',
      }),
    ).toBe('@meeting_notes with action items');
  });

  it('falls back to the payload question when no displayText is given', () => {
    // Callers that never split payload from display (e.g. programmatic sends)
    // keep today's behavior.
    expect(buildDisplayQuestion('catch me up', { forceSkill: 'catch_up' })).toBe(
      '@catch_up catch me up',
    );
  });

  it('leaves plain messages untouched', () => {
    expect(buildDisplayQuestion('hello')).toBe('hello');
  });
});
