/**
 * Render contract for SessionMessageCard: sender attribution, markdown body,
 * timestamp, and the "Jump to session" action gated on the sender still
 * being a known session (it may have been deleted since the message arrived).
 */
import { render, fireEvent } from '@testing-library/react';
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { SessionMessageCard } from './SessionMessageCard';
import { useSessionStore } from '@/stores/sessionStore';
import type { SessionMessagePayload } from '@/types/chat';

function msg(over: Partial<SessionMessagePayload> = {}): SessionMessagePayload {
  return {
    from_session_id: 's1',
    from_title: 'Payments API',
    content: 'the migration is done',
    timestamp: '2026-08-08T05:00:00Z',
    ...over,
  };
}

describe('SessionMessageCard', () => {
  beforeEach(() => {
    useSessionStore.setState({ sessions: [] });
  });

  it('renders the sender and message body', () => {
    const { getByText } = render(<SessionMessageCard message={msg()} />);
    expect(getByText('Payments API')).toBeTruthy();
    expect(getByText(/the migration is done/)).toBeTruthy();
  });

  it('shows no jump action when the sender session is unknown', () => {
    const { queryByTitle } = render(<SessionMessageCard message={msg()} />);
    expect(queryByTitle('Open that session')).toBeNull();
  });

  it('shows a jump action that switches to the sender session when it exists', () => {
    useSessionStore.setState({
      sessions: [{ id: 's1', title: 'Payments API', date: '', segmentCount: 0, chatCount: 0 } as never],
    });
    const switchSession = vi.fn().mockResolvedValue(undefined);
    useSessionStore.setState({ switchSession } as never);

    const { getByTitle } = render(<SessionMessageCard message={msg()} />);
    fireEvent.click(getByTitle('Open that session'));
    expect(switchSession).toHaveBeenCalledWith('s1');
  });
});
