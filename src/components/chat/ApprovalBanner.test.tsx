/**
 * The approval card's FAILURE path, which used to strand the paused turn.
 *
 * `executeApproval` throws an ApiError on any non-2xx / network failure. When
 * that rejection escaped the click handler, three things happened at once and
 * none of them were visible: the continuation was never sent (so the model was
 * never told the user approved), `isProcessing` stayed true (so the next card
 * rendered with every button disabled as "Running…"), and queued approvals were
 * never surfaced. The user's only clue was a chat that had gone quiet.
 *
 * Only the action executor and the network are faked: the card drives the
 * real approval leg and the real continuation.
 */
import { render, fireEvent, waitFor } from '@testing-library/react';
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import type { PendingApproval } from '@/stores/chatStore';
import { dropRuntime, getChatStore, useRuntimeIndex } from '@/stores/sessionRuntimes';
import { useSessionStore } from '@/stores/sessionStore';
import { killSessionStream } from '@/hooks/chatStream/streamControl';
import { ApprovalBanner } from './ApprovalBanner';

const executeApproval = vi.fn();

vi.mock('@/api/approval', () => ({
  executeApproval: (...args: unknown[]) => executeApproval(...args),
}));

const SID = 'sess-approval';

function approval(toolUseId: string): PendingApproval {
  return {
    toolUseId,
    action: 'git_create_branch',
    category: 'cli',
    preview: 'command',
    summary: `Create branch ${toolUseId} from main`,
    payload: { command: `git checkout -b ${toolUseId}` },
    riskHint: 'low',
    explanation: null,
    sessionId: SID,
    alwaysAsks: false,
    turnSettings: { model: 'm', effort_level: 'normal', verbosity: 'medium', brief_mode: false },
  };
}

/** Every /api/chat continuation answers with one short reply. */
function continuationBodies(): Array<{ approved_tool_result: { tool_use_id: string; content: string } }> {
  const bodies: Array<{ approved_tool_result: { tool_use_id: string; content: string } }> = [];
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    if (!String(input).includes('/api/chat')) return new Response('{}', { status: 200 });
    bodies.push(JSON.parse(String(init?.body)));
    const stream = new ReadableStream<Uint8Array>({
      start(c) {
        const enc = new TextEncoder();
        c.enqueue(enc.encode('data: {"text": "resumed"}\n\n'));
        c.enqueue(enc.encode('data: [DONE]\n\n'));
        c.close();
      },
    });
    return new Response(stream, { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
  });
  return bodies;
}

describe('ApprovalBanner', () => {
  beforeEach(() => {
    for (const id of useRuntimeIndex.getState().liveIds) dropRuntime(id);
    useSessionStore.setState({ currentSessionId: SID, liveSessions: {}, sessions: [] });
    executeApproval.mockReset();
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('still resumes the turn when executing the approved action throws', async () => {
    const bodies = continuationBodies();
    executeApproval.mockRejectedValue(new Error('HTTP 500: executor unreachable'));
    getChatStore(SID).getState().enqueueApproval(approval('tu_1'));

    const { getByText } = render(<ApprovalBanner />);
    fireEvent.click(getByText('✓ Yes'));

    await waitFor(() => expect(bodies).toHaveLength(1));
    const result = bodies[0].approved_tool_result;
    expect(result.tool_use_id).toBe('tu_1');
    // Truthful outcome: approved, but the operation did NOT happen.
    expect(result.content).toContain('FAILED');
    expect(result.content).toContain('executor unreachable');
  });

  it('recovers the card UI after a failure so a queued approval stays actionable', async () => {
    continuationBodies();
    executeApproval.mockRejectedValue(new Error('Network error'));
    const chat = getChatStore(SID).getState();
    chat.enqueueApproval(approval('tu_1'));
    chat.enqueueApproval(approval('tu_2')); // queued behind the first

    const { getByText, findByText } = render(<ApprovalBanner />);
    fireEvent.click(getByText('✓ Yes'));

    await waitFor(() =>
      expect(getChatStore(SID).getState().currentApproval?.toolUseId).toBe('tu_2'),
    );
    // Buttons live again — not frozen at "Running…" until a page reload.
    const yes = await findByText('✓ Yes');
    expect((yes as HTMLButtonElement).disabled).toBe(false);
  });

  it('sends the continuation with the real outcome on the happy path', async () => {
    const bodies = continuationBodies();
    executeApproval.mockResolvedValue({ ok: true, output: 'Created branch docs/x' });
    getChatStore(SID).getState().enqueueApproval(approval('tu_ok'));

    const { getByText } = render(<ApprovalBanner />);
    fireEvent.click(getByText('✓ Yes'));

    await waitFor(() => expect(bodies).toHaveLength(1));
    expect(bodies[0].approved_tool_result.content).toContain('The action succeeded');
    expect(bodies[0].approved_tool_result.content).toContain('Created branch docs/x');
    expect(getChatStore(SID).getState().currentApproval).toBeNull();
    await waitFor(() => expect(getChatStore(SID).getState().isStreaming).toBe(false));
  });

  it('while the approved action runs the session is busy, and Stop keeps it from resuming', async () => {
    const bodies = continuationBodies();
    let finish: ((v: unknown) => void) | null = null;
    executeApproval.mockImplementation(() => new Promise((r) => { finish = r; }));
    getChatStore(SID).getState().enqueueApproval(approval('tu_slow'));

    const { getByText } = render(<ApprovalBanner />);
    fireEvent.click(getByText('✓ Yes'));

    // The card went away, but the session reads as working: the composer
    // shows Stop and steers instead of starting a second turn.
    await waitFor(() => expect(executeApproval).toHaveBeenCalled());
    expect(getChatStore(SID).getState().currentApproval).toBeNull();
    expect(getChatStore(SID).getState().isStreaming).toBe(true);

    killSessionStream(SID);
    finish!({ ok: true, output: 'done' });
    await waitFor(() =>
      expect(getChatStore(SID).getState().messages.some((m) => m.stopped)).toBe(true),
    );
    expect(bodies).toHaveLength(0);
    expect(getChatStore(SID).getState().isStreaming).toBe(false);
  });

  it('No resumes the turn with the denial and runs nothing', async () => {
    const bodies = continuationBodies();
    getChatStore(SID).getState().enqueueApproval(approval('tu_no'));

    const { getByText } = render(<ApprovalBanner />);
    fireEvent.click(getByText('✕ No'));

    await waitFor(() => expect(bodies).toHaveLength(1));
    expect(bodies[0].approved_tool_result.content).toContain('[User denied]');
    expect(executeApproval).not.toHaveBeenCalled();
  });

  // The server checks a hard floor before the session's remembered choice, so
  // "Yes, all" and "Block" would do nothing for it. The card must not offer
  // them, and must say why only Yes and No are there.
  it('a card the server always asks for offers no remember-for-session choice', () => {
    getChatStore(SID).getState().enqueueApproval({ ...approval('tu_rm'), alwaysAsks: true });

    const { queryByText, getByText } = render(<ApprovalBanner />);

    expect(getByText('✓ Yes')).toBeTruthy();
    expect(getByText('✕ No')).toBeTruthy();
    expect(queryByText(/Yes, all/)).toBeNull();
    expect(queryByText(/Block/)).toBeNull();
    expect(getByText('This action always asks.')).toBeTruthy();
  });

  it('an ordinary card still offers the session choice', () => {
    getChatStore(SID).getState().enqueueApproval(approval('tu_plain'));

    const { getByText, queryByText } = render(<ApprovalBanner />);

    expect(getByText('✓ Yes, all cli')).toBeTruthy();
    expect(getByText('✕ Block cli')).toBeTruthy();
    expect(queryByText('This action always asks.')).toBeNull();
  });
});
