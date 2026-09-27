import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { renderEventCards } from './sseEventCards';
import { dropRuntime, getChatStore } from '@/stores/sessionRuntimes';
import { useGoalStore } from '@/stores/goalStore';
import type { SSEEventData } from '@/types/chat';

/**
 * The completion gate's frames become transcript cards. A verdict the judge
 * could not reach ('not_checked') is terminal but not a failure, keeps the
 * goal active, and carries its full reason; and a gate card ends the "Checking
 * the goal on ..." status, which must not label the next round's wait.
 */
describe('renderEventCards: completion-gate frames', () => {
  const sid = 'sess-gate-cards';
  const store = () => getChatStore(sid).getState();
  const lastCard = () => store().messages[store().messages.length - 1].toolUse?.[0];
  const render = (parsed: SSEEventData) => renderEventCards(parsed, store, sid, () => {});

  beforeEach(() => {
    useGoalStore.getState().setGoal(sid, 'write the summary', true);
  });

  afterEach(() => {
    useGoalStore.getState().clearGoal(sid);
    dropRuntime(sid);
  });

  it('a not_checked verdict is a neutral card with the full reason and keeps the goal', () => {
    const reason = `Gemma could not check the goal: ${'the answer was empty. '.repeat(8)}`;
    render({ goal_eval: { verdict: 'not_checked', feedback: reason, confidence: 0, attempt: 0, cap: 8 } });
    const card = lastCard();
    expect(card?.toolName).toBe('goal_eval');
    expect(card?.status).toBe('complete');
    expect(card?.result).toBe(reason);
    const goal = useGoalStore.getState().byId[sid];
    expect(goal.active).toBe(true);
    expect(goal.lastVerdict).toBe('not_checked');
    expect(goal.lastFeedback).toBe(reason);
  });

  it('blocked stays an error card and keeps the goal; achieved ends it', () => {
    render({ goal_eval: { verdict: 'blocked', feedback: 'needs a key', confidence: 0.9 } });
    expect(lastCard()?.status).toBe('error');
    expect(useGoalStore.getState().byId[sid].active).toBe(true);
    render({ goal_eval: { verdict: 'achieved', feedback: 'done', confidence: 0.9 } });
    expect(lastCard()?.status).toBe('complete');
    expect(useGoalStore.getState().byId[sid].active).toBe(false);
  });

  it.each([
    { goal_eval: { verdict: 'not_achieved', feedback: 'keep going' } },
    { stop_hook_block: { reason: 'tests are red', attempt: 1 } },
    { goal_cap_reached: { attempt: 8, cap: 8, source: 'evaluator' } },
  ] as SSEEventData[])('a gate card clears the goal-check status (%o)', (frame) => {
    store().setStreamStatus('Checking the goal on Gemma 4 12B (Local)...');
    render(frame);
    expect(store().streamStatus).toBeNull();
  });
});
