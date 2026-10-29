import { afterEach, describe, expect, it } from 'vitest';
import { render } from '@testing-library/react';
import { TurnBubble } from './TurnBubble';
import { renderEventCards } from '@/hooks/chatStream/sseEventCards';
import { dropRuntime, getChatStore } from '@/stores/sessionRuntimes';
import { SSEEventDataSchema } from '@/types/schemas';
import type { SSEEventData } from '@/types/chat';

/**
 * A Stop hook that refuses to end the turn leaves a stop_hook_block card in
 * the transcript: the generic hook label, with the reason as its detail line
 * (a card can share a group with the calls it judged). The card is built
 * along the path a live frame takes: the schema, then the event-card
 * renderer, then the turn's bubble. The server sends the model back over a
 * claimed delivery or a requested file with no card at all.
 */
describe('a stop_hook_block card', () => {
  const sid = 'sess-stop-hook';
  afterEach(() => dropRuntime(sid));

  function renderCard(frame: unknown) {
    const parsed: SSEEventData = SSEEventDataSchema.parse(frame);
    const store = () => getChatStore(sid).getState();
    renderEventCards(parsed, store, sid, () => {});
    const { messages } = store();
    const card = messages[messages.length - 1];
    return render(<TurnBubble rows={[card]} indices={[messages.length - 1]} />).container;
  }

  it('shows the generic label with the reason as its detail', () => {
    const card = renderCard({ stop_hook_block: { reason: 'tests are red', attempt: 1 } });
    expect(card.querySelector('.activity-label')?.textContent).toBe('Continuing (Stop hook)');
    expect(card.querySelector('.activity-call-subject')?.textContent).toBe('Continuing (Stop hook): tests are red');
  });
});
