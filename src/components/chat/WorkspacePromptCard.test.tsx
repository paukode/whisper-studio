/**
 * WorkspacePromptCard's resume must send the `answers` array shape
 * ({tool_use_id, content}), not a bare `answer` string: only the former goes
 * through sendChatTurn('', {approvedToolResult}) (hooks/chatStream/pausedAnswer),
 * the sole path that reaches server/chat/routes.py's approved_tool_result
 * branch and restores the stashed paused-turn state. A bare string is treated
 * as a brand new chat message and never resumes anything (see sseStream.ts /
 * tool_executor.py).
 *
 * This card only handles the workspace-connect flow for a modify or delete
 * with no workspace connected. One-off file saves resolve their destination
 * in the tool executor (user's words or a Documents default) and go straight
 * to the approval card, and a path outside a connected workspace is answered
 * by the backend (a save approval or a plain refusal), never here.
 */
import { render, fireEvent, waitFor } from '@testing-library/react';
import { describe, it, expect, beforeEach, vi, afterEach } from 'vitest';
import { WorkspacePromptCard } from './WorkspacePromptCard';
import { useUIStore } from '@/stores/uiStore';

type Answers = Array<{ tool_use_id: string; content: string }>;

// Patched where pausedAnswer reads it: the card resumes through sendChatTurn.
const sendChatTurn = vi.hoisted(() => vi.fn());
vi.mock('@/hooks/useChatStream', () => ({ sendChatTurn }));

/** What each resume sent; `resumed` is what sendChatTurn answers. */
function captureResumeEvents(resumed = true): Array<{ answer?: string; answers?: Answers }> {
  const captured: Array<{ answer?: string; answers?: Answers }> = [];
  sendChatTurn.mockReset();
  sendChatTurn.mockImplementation(async (_q: string, opts: { approvedToolResult: Answers }) => {
    captured.push({ answers: opts.approvedToolResult });
    return resumed;
  });
  return captured;
}

describe('WorkspacePromptCard', () => {
  const originalFetch = globalThis.fetch;

  beforeEach(() => {
    globalThis.fetch = vi.fn();
  });

  afterEach(() => {
    globalThis.fetch = originalFetch;
    vi.restoreAllMocks();
  });

  it('workspace-connect flow: dispatches an answers-array with the real tool_use_id, not a bare answer string', async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true,
      json: async () => ({ path: '/Users/me/Projects/site', writable: true }),
    });
    const captured = captureResumeEvents();

    const { getByText } = render(
      <WorkspacePromptCard
        suggested="/Users/me/Projects/site"
        recent={[]}
        toolUseId="tu_real_123"
      />,
    );

    fireEvent.click(getByText('/Users/me/Projects/site'));

    await waitFor(() => expect(captured.length).toBe(1));
    const detail = captured[0];
    // Must NOT be the old broken shape (bare answer string is a new message).
    expect(detail.answer).toBeUndefined();
    expect(detail.answers).toEqual([
      {
        tool_use_id: 'tu_real_123',
        content: expect.stringContaining('/Users/me/Projects/site'),
      },
    ]);
  });

  it('stores and names the canonical root the server resolved, not the picked path', async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true,
      json: async () => ({ path: '/Users/me/Projects/site', writable: true }),
    });
    const captured = captureResumeEvents();

    const { getByText } = render(
      <WorkspacePromptCard suggested="~/Projects/site" recent={[]} toolUseId="tu_canon" />,
    );
    fireEvent.click(getByText('~/Projects/site'));

    await waitFor(() => expect(captured.length).toBe(1));
    expect(useUIStore.getState().wsPath).toBe('/Users/me/Projects/site');
    expect(captured[0].answers![0].content).toContain('Workspace connected to /Users/me/Projects/site.');
  });

  it('no_workspace: "Not now" resolves the pause instead of stranding the turn', async () => {
    const captured = captureResumeEvents();

    const { getByText } = render(
      <WorkspacePromptCard
        suggested="/Users/me/Documents"
        recent={[]}
        toolUseId="tu_decline"
      />,
    );

    fireEvent.click(getByText('Not now'));

    await waitFor(() => expect(captured.length).toBe(1));
    expect(captured[0].answers![0].content).toContain('declined to connect a workspace');
  });

  it('surfaces a failed connect instead of silently un-busying the button', async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockImplementation((url: string) =>
      String(url).includes('/api/workspace/connect')
        ? Promise.resolve({ ok: false, json: async () => ({ error: 'Directory not found: /gone' }) })
        : Promise.resolve({ ok: true, json: async () => ({ connected: false }) }),
    );

    const { getByText, findByText } = render(
      <WorkspacePromptCard
        suggested="/gone"
        recent={[]}
        toolUseId="tu_err"
      />,
    );

    fireEvent.click(getByText('/gone'));

    expect(await findByText('Directory not found: /gone')).toBeTruthy();
  });

  it('a refused resume gives the card back so the user can answer again', async () => {
    // Refused before the server ran it (the parallel ceiling, declined
    // consent): a card stuck on "connected" would strand the paused turn.
    const captured = captureResumeEvents(false);
    const { getByText, getByRole } = render(
      <WorkspacePromptCard suggested="/Users/me/Projects/site" recent={[]} toolUseId="tu_x" />,
    );

    fireEvent.click(getByRole('button', { name: /not now/i }));

    await waitFor(() => expect(captured).toHaveLength(1));
    await waitFor(() => expect(getByText('/Users/me/Projects/site').closest('button')).toBeEnabled());
  });
});
