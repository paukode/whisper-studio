/**
 * The offer to run the code checks after a turn that changed code files.
 * A clean run ends at the banner and sends nothing to the assistant; findings
 * are sent as the user's next message; Skip runs nothing.
 */
import { render, fireEvent, screen, waitFor } from '@testing-library/react';
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { dropRuntime, getChatStore, useRuntimeIndex } from '@/stores/sessionRuntimes';
import { useSessionStore } from '@/stores/sessionStore';

const api = vi.hoisted(() => ({ get: vi.fn(), put: vi.fn(), post: vi.fn(), del: vi.fn() }));
vi.mock('@/api/client', () => api);
const sendChatTurn = vi.hoisted(() => vi.fn());
vi.mock('@/hooks/useChatStream', () => ({ sendChatTurn }));

import { CodeChecksBanner } from './CodeChecksBanner';

const SID = 'sess-checks';
const FILES = ['/ws/a.py', '/ws/b.ts'];
const offer = () => getChatStore(SID).getState().codeChecksOffer;

describe('CodeChecksBanner', () => {
  beforeEach(() => {
    for (const id of useRuntimeIndex.getState().liveIds) dropRuntime(id);
    useSessionStore.setState({ currentSessionId: SID, liveSessions: {}, sessions: [] });
    vi.clearAllMocks();
    getChatStore(SID).getState().setCodeChecksOffer({ files: FILES, sessionId: SID });
  });

  it('shows nothing when the turn left no offer', () => {
    getChatStore(SID).getState().setCodeChecksOffer(null);
    render(<CodeChecksBanner />);
    expect(screen.queryByTestId('code-checks-banner')).toBeNull();
  });

  it('Skip clears the offer and runs nothing', () => {
    render(<CodeChecksBanner />);
    expect(screen.getByText(/changed 2 code files/)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Skip' }));
    expect(offer()).toBeNull();
    expect(api.post).not.toHaveBeenCalled();
    expect(sendChatTurn).not.toHaveBeenCalled();
  });

  it('a clean run sends nothing to the assistant', async () => {
    api.post.mockResolvedValueOnce({
      results: FILES.map((path) => ({ path, tool: 'ruff', issues: 0, text: 'no issues' })),
      issues: 0,
      report: '',
      enabled: true,
    });
    render(<CodeChecksBanner />);
    fireEvent.click(screen.getByRole('button', { name: 'Run checks' }));
    await waitFor(() => expect(offer()).toBeNull());
    expect(api.post.mock.calls[0].slice(0, 2)).toEqual(['/api/code-tools/check', { files: FILES }]);
    expect(sendChatTurn).not.toHaveBeenCalled();
  });

  it('findings go to the assistant as the next message', async () => {
    const report = 'I ran the code checks. They found 2 issues.';
    api.post.mockResolvedValueOnce({
      results: [{ path: FILES[0], tool: 'ruff', issues: 2, text: 'ruff: 2 issues' }],
      issues: 2,
      report,
      enabled: true,
    });
    render(<CodeChecksBanner />);
    fireEvent.click(screen.getByRole('button', { name: 'Run checks' }));
    await waitFor(() => expect(sendChatTurn).toHaveBeenCalledWith(report));
    expect(offer()).toBeNull();
  });

  it('findings for a session the user left are not sent into the open one', async () => {
    let finish: ((v: unknown) => void) | null = null;
    api.post.mockImplementationOnce(() => new Promise((r) => { finish = r; }));
    render(<CodeChecksBanner />);
    fireEvent.click(screen.getByRole('button', { name: 'Run checks' }));
    useSessionStore.setState({ currentSessionId: 'sess-other' });
    finish!({ results: [], issues: 1, report: 'one issue', enabled: true });
    await waitFor(() => expect(api.post).toHaveBeenCalled());
    await new Promise((r) => setTimeout(r, 0));
    expect(sendChatTurn).not.toHaveBeenCalled();
    expect(offer()).not.toBeNull();
  });
});
