/**
 * WorkspacePromptCard's resume mechanism must dispatch the `answers` array
 * shape ({tool_use_id, content}), not a bare `answer` string — only the
 * former is routed by useComposerChatEvents through
 * chatStream.send('', {approvedToolResult}), the sole path that reaches
 * server/chat/routes.py's approved_tool_result branch and restores the
 * stashed paused-turn state. A bare string is treated as a brand new chat
 * message and never resumes anything (see sseStream.ts / tool_executor.py).
 *
 * Covers both branches that share this component:
 *  - the classic workspace-connect flow (reason='no_workspace' etc.)
 *  - the new one-shot save_location flow (reason='save_location'), which
 *    must resume WITHOUT ever calling /api/workspace/connect.
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

  it('save_location flow: quick-pick resumes with an absolute destination path and never calls /connect', async () => {
    const captured = captureResumeEvents();

    const { getByText } = render(
      <WorkspacePromptCard
        reason="save_location"
        suggested=""
        recent={[]}
        toolUseId="tu_save_456"
        toolName="save_file"
        suggestedName="report.pdf"
        documentsDir="/Users/me/Documents"
        downloadsDir="/Users/me/Downloads"
      />,
    );

    fireEvent.click(getByText('/Users/me/Documents/report.pdf'));

    await waitFor(() => expect(captured.length).toBe(1));
    expect(captured[0].answers).toEqual([
      {
        tool_use_id: 'tu_save_456',
        content: expect.stringContaining('/Users/me/Documents/report.pdf'),
      },
    ]);
    expect(captured[0].answers?.[0].content).toContain('save_file');
    // Never touches the persistent-workspace endpoint.
    expect(globalThis.fetch).not.toHaveBeenCalledWith(
      '/api/workspace/connect',
      expect.anything(),
    );
  });

  it('save_location flow: Browse button calls pick-save-target (not pick-folder) and resumes with the picked path', async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true,
      json: async () => ({ path: '/Users/me/Desktop/custom-name.pdf' }),
    });
    const captured = captureResumeEvents();

    const { getByText } = render(
      <WorkspacePromptCard
        reason="save_location"
        suggested=""
        recent={[]}
        toolUseId="tu_save_789"
        suggestedName="report.pdf"
      />,
    );

    fireEvent.click(getByText('Browse…'));

    await waitFor(() => expect(captured.length).toBe(1));
    expect(globalThis.fetch).toHaveBeenCalledWith(
      expect.stringContaining('/api/workspace/pick-save-target?filename='),
    );
    expect(captured[0].answers).toEqual([
      {
        tool_use_id: 'tu_save_789',
        content: expect.stringContaining('/Users/me/Desktop/custom-name.pdf'),
      },
    ]);
  });
});
