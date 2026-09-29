import { afterEach, describe, expect, it } from 'vitest';
import { render } from '@testing-library/react';
import { ChatMessage } from './ChatMessage';
import { renderEventCards } from '@/hooks/chatStream/sseEventCards';
import { dropRuntime, getChatStore } from '@/stores/sessionRuntimes';
import { SSEEventDataSchema } from '@/types/schemas';
import type { SSEEventData } from '@/types/chat';

/**
 * When the server holds back a sentence claiming a delivery it could not
 * verify, it sends the model back with a stop_hook_block whose source is
 * 'deliverable'. The card that frame leaves in the transcript says what is
 * happening instead of the generic Stop-hook label, and shows the server's
 * reason as its detail the way every hook reason is shown. Each card is
 * built along the path a live frame takes: the schema, then the event-card
 * renderer, then the message view.
 */
describe('a stop_hook_block card', () => {
  const sid = 'sess-claim-check';
  afterEach(() => dropRuntime(sid));

  function renderCard(frame: unknown) {
    const parsed: SSEEventData = SSEEventDataSchema.parse(frame);
    const store = () => getChatStore(sid).getState();
    renderEventCards(parsed, store, sid, () => {});
    const { messages } = store();
    const card = messages[messages.length - 1];
    return render(<ChatMessage message={card} index={messages.length - 1} />).container;
  }

  it('names a held-back delivery claim and shows the reason as its detail', () => {
    const reason = 'A delivery the reply claimed could not be verified. Produce it, or say it was not done.';
    const card = renderCard({ stop_hook_block: { reason, attempt: 1, source: 'deliverable' } });
    expect(card.querySelector('.activity-step-name')?.textContent).toBe('Checking a claimed delivery');
    expect(card.querySelector('.activity-step-detail')?.textContent).toBe(reason);
  });

  it.each([['verify'], ['stop_hook'], [undefined]])('keeps the generic label for source %s', (source) => {
    const card = renderCard({
      stop_hook_block: { reason: 'tests are red', attempt: 1, ...(source ? { source } : {}) },
    });
    expect(card.querySelector('.activity-step-name')?.textContent).toBe('Continuing (Stop hook)');
    expect(card.querySelector('.activity-step-detail')?.textContent).toBe('tests are red');
  });
});
