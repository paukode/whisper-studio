/**
 * WorkspacePromptCard's resume mechanism must dispatch the `answers` array
 * shape ({tool_use_id, content}), not a bare `answer` string — only the
 * former is routed by useComposerChatEvents through
 * chatStream.send('', {approvedToolResult}), the sole path that reaches
 * server/chat/routes.py's approved_tool_result branch and restores the
 * stashed paused-turn state. A bare string is treated as a brand new chat
 * message and never resumes anything (see sseStream.ts / tool_executor.py).
 *
 * This card only handles the workspace-connect flow now: one-off file saves
 * resolve their destination in the tool executor (user's words or a
 * Documents default) and go straight to the approval card, never here.
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
        reason="no_workspace"
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

  /**
   * The card used to render the raw `reason` slug as its entire explanation,
   * so a user whose workspace root had moved underneath them saw the word
   * "outside_workspace" and nothing else — not the root that was checked, not
   * the path that was rejected, i.e. nothing that could explain why a card
   * asking for a workspace appeared while a workspace was plainly open.
   */
  it('outside_workspace: names the connected root and the rejected path instead of the slug', () => {
    const { getByText, queryByText } = render(
      <WorkspacePromptCard
        reason="outside_workspace"
        suggested="/Users/me/Projects/other"
        recent={[]}
        workspace="/private/var/folders/xx/pytest-of-me/ws"
        attempted="/Users/me/Documents/whisper-studio/server/main.py"
        toolName="ws_write_file"
        toolUseId="tu_1"
      />,
    );

    expect(queryByText('outside_workspace')).toBeNull();
    expect(getByText('/private/var/folders/xx/pytest-of-me/ws')).toBeTruthy();
    expect(getByText('/Users/me/Documents/whisper-studio/server/main.py')).toBeTruthy();
  });

  it('outside_workspace: keeping the workspace resumes the turn without connecting anything', async () => {
    const captured = captureResumeEvents();

    const { getByText } = render(
      <WorkspacePromptCard
        reason="outside_workspace"
        suggested="/Users/me/Projects/other"
        recent={[]}
        workspace="/Users/me/Projects/site"
        attempted="/tmp/scratch.txt"
        toolName="ws_write_file"
        toolUseId="tu_keep"
      />,
    );

    fireEvent.click(getByText('Keep this workspace and retry inside it'));

    await waitFor(() => expect(captured.length).toBe(1));
    expect(captured[0].answers![0].tool_use_id).toBe('tu_keep');
    expect(captured[0].answers![0].content).toContain('/Users/me/Projects/site');
    expect(captured[0].answers![0].content).toContain('did NOT execute');
    // No workspace switch: /api/workspace/connect must never be called.
    const calls = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls;
    expect(calls.some((c) => String(c[0]).includes('/api/workspace/connect'))).toBe(false);
  });

  it('no_workspace: "Not now" resolves the pause instead of stranding the turn', async () => {
    const captured = captureResumeEvents();

    const { getByText } = render(
      <WorkspacePromptCard
        reason="no_workspace"
        suggested="/Users/me/Documents"
        recent={[]}
        workspace={null}
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
        reason="no_workspace"
        suggested="/gone"
        recent={[]}
        toolUseId="tu_err"
      />,
    );

    fireEvent.click(getByText('/gone'));

    expect(await findByText('Directory not found: /gone')).toBeTruthy();
  });
});
