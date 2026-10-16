/**
 * WorkspacePromptCard's resume mechanism must dispatch the `answers` array
 * shape ({tool_use_id, content}), not a bare `answer` string — only the
 * former is routed by useComposerChatEvents through
 * chatStream.send('', {approvedToolResult}), the sole path that reaches
 * server/chat/routes.py's approved_tool_result branch and restores the
 * stashed paused-turn state. A bare string is treated as a brand new chat
 * message and never resumes anything (see sseStream.ts / tool_executor.py).
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

function captureResumeEvents(): Array<{ answer?: string; answers?: Array<{ tool_use_id: string; content: string }> }> {
  const captured: Array<{ answer?: string; answers?: Array<{ tool_use_id: string; content: string }> }> = [];
  const handler = (e: Event) => {
    captured.push((e as CustomEvent).detail);
  };
  window.addEventListener('whisper-submit-answer', handler);
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
      json: async () => ({ writable: true }),
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
});
