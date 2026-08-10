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
});
