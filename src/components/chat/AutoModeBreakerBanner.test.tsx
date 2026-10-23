/**
 * One banner draws the notice of whichever session is open, so a Resume in
 * flight in one session must not disable the button in another.
 */
import { act, render, fireEvent, screen, waitFor } from '@testing-library/react';
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { dropRuntime, getChatStore, useRuntimeIndex } from '@/stores/sessionRuntimes';
import { useSessionStore } from '@/stores/sessionStore';
import { AutoModeBreakerBanner } from './AutoModeBreakerBanner';

const resumeAutoMode = vi.fn();

vi.mock('@/api/settings', () => ({
  resumeAutoMode: (...args: unknown[]) => resumeAutoMode(...args),
}));

const SID = 'sess-breaker';
const OTHER = 'sess-breaker-other';

describe('AutoModeBreakerBanner', () => {
  beforeEach(() => {
    for (const id of useRuntimeIndex.getState().liveIds) dropRuntime(id);
    useSessionStore.setState({ currentSessionId: SID, liveSessions: {}, sessions: [] });
    resumeAutoMode.mockReset();
  });

  it('a Resume in flight in one session leaves the other session\'s button live', async () => {
    let finish: ((v: unknown) => void) | null = null;
    resumeAutoMode.mockImplementationOnce(() => new Promise((r) => { finish = r; }));
    getChatStore(SID).getState().setAutoModeBreaker({ reason: 'Paused in A', sessionId: SID });
    getChatStore(OTHER).getState().setAutoModeBreaker({ reason: 'Paused in B', sessionId: OTHER });

    render(<AutoModeBreakerBanner />);
    fireEvent.click(screen.getByRole('button', { name: 'Resume auto mode' }));
    expect(screen.getByRole('button', { name: 'Resuming…' })).toBeTruthy();

    act(() => useSessionStore.setState({ currentSessionId: OTHER }));
    const resume = screen.getByRole('button', { name: 'Resume auto mode' });
    expect((resume as HTMLButtonElement).disabled).toBe(false);

    finish!({ resumed: true });
    await waitFor(() => expect(getChatStore(SID).getState().autoModeBreaker).toBeNull());
    expect(getChatStore(OTHER).getState().autoModeBreaker).not.toBeNull();
  });
});
