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
import { act, render, fireEvent, screen, waitFor } from '@testing-library/react';
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import type { PendingApproval } from '@/stores/chatStore';
import { dropRuntime, getChatStore, useRuntimeIndex } from '@/stores/sessionRuntimes';
import { useSessionStore } from '@/stores/sessionStore';
import { killSessionStream } from '@/hooks/chatStream/streamControl';
import { ApprovalBanner } from './ApprovalBanner';
import { MAX_DIFF_CELLS } from '@/utils/computeLineDiff';

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

    render(<ApprovalBanner />);
    fireEvent.click(screen.getByRole('button', { name: 'Yes' }));

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

    render(<ApprovalBanner />);
    fireEvent.click(screen.getByRole('button', { name: 'Yes' }));

    await waitFor(() =>
      expect(getChatStore(SID).getState().currentApproval?.toolUseId).toBe('tu_2'),
    );
    // Buttons live again, not frozen at "Running…" until a page reload.
    const yes = await screen.findByRole('button', { name: 'Yes' });
    expect((yes as HTMLButtonElement).disabled).toBe(false);
  });

  it('sends the continuation with the real outcome on the happy path', async () => {
    const bodies = continuationBodies();
    executeApproval.mockResolvedValue({ ok: true, output: 'Created branch docs/x' });
    getChatStore(SID).getState().enqueueApproval(approval('tu_ok'));

    render(<ApprovalBanner />);
    fireEvent.click(screen.getByRole('button', { name: 'Yes' }));

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

    render(<ApprovalBanner />);
    fireEvent.click(screen.getByRole('button', { name: 'Yes' }));

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

  // One banner draws the card of whichever session is open: ChatPanel is not
  // remounted on a session switch. A Yes in one session used to hold
  // "Running…" on the other session's card until the first session's
  // resumed turn ended.
  it('a card in another session stays answerable while one session runs its approved action', async () => {
    const bodies = continuationBodies();
    let finish: ((v: unknown) => void) | null = null;
    executeApproval.mockImplementationOnce(() => new Promise((r) => { finish = r; }));
    const OTHER = 'sess-approval-other';
    getChatStore(SID).getState().enqueueApproval(approval('tu_a'));
    getChatStore(OTHER).getState().enqueueApproval({ ...approval('tu_b'), sessionId: OTHER });

    render(<ApprovalBanner />);
    fireEvent.click(screen.getByRole('button', { name: 'Yes' }));
    await waitFor(() => expect(executeApproval).toHaveBeenCalledTimes(1));

    act(() => useSessionStore.setState({ currentSessionId: OTHER }));
    const yes = screen.getByRole('button', { name: 'Yes' });
    expect((yes as HTMLButtonElement).disabled).toBe(false);

    // Answering it resumes that session now, not after the first one's turn.
    executeApproval.mockResolvedValueOnce({ ok: true, output: 'b done' });
    fireEvent.click(yes);
    await waitFor(() => expect(bodies.map((b) => b.approved_tool_result.tool_use_id)).toEqual(['tu_b']));

    finish!({ ok: true, output: 'a done' });
    await waitFor(() => expect(bodies).toHaveLength(2));
    expect(bodies[1].approved_tool_result.tool_use_id).toBe('tu_a');
  });

  it('a card in the same session waits while that session runs its approved action', async () => {
    const bodies = continuationBodies();
    let finish: ((v: unknown) => void) | null = null;
    executeApproval.mockImplementationOnce(() => new Promise((r) => { finish = r; }));
    getChatStore(SID).getState().enqueueApproval(approval('tu_first'));

    render(<ApprovalBanner />);
    fireEvent.click(screen.getByRole('button', { name: 'Yes' }));
    await waitFor(() => expect(executeApproval).toHaveBeenCalledTimes(1));

    act(() => getChatStore(SID).getState().enqueueApproval(approval('tu_next')));
    const running = screen.getByRole('button', { name: 'Running…' });
    expect((running as HTMLButtonElement).disabled).toBe(true);

    finish!({ ok: true, output: 'done' });
    await waitFor(() => expect(bodies).toHaveLength(1));
    const yes = await screen.findByRole('button', { name: 'Yes' });
    expect((yes as HTMLButtonElement).disabled).toBe(false);
  });

  it('No resumes the turn with the denial and runs nothing', async () => {
    const bodies = continuationBodies();
    getChatStore(SID).getState().enqueueApproval(approval('tu_no'));

    render(<ApprovalBanner />);
    fireEvent.click(screen.getByRole('button', { name: 'No' }));

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

    expect(screen.getByRole('button', { name: 'Yes' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'No' })).toBeTruthy();
    expect(queryByText(/Yes, all/)).toBeNull();
    expect(queryByText(/Block/)).toBeNull();
    expect(getByText('This action always asks.')).toBeTruthy();
  });

  it('an ordinary card still offers the session choice', () => {
    getChatStore(SID).getState().enqueueApproval(approval('tu_plain'));

    const { queryByText } = render(<ApprovalBanner />);

    expect(screen.getByRole('button', { name: 'Yes, all cli' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Block cli' })).toBeTruthy();
    expect(queryByText('This action always asks.')).toBeNull();
  });
});

/** A write card: the diff preview draws the diff dialog's own grid. */
function writeApproval(toolUseId: string, payload: Record<string, unknown>): PendingApproval {
  return {
    ...approval(toolUseId),
    action: 'ws_write_file',
    category: 'write',
    preview: 'diff',
    summary: `Write ${String(payload.path)}`,
    payload,
  };
}

const cells = (container: HTMLElement, kind: 'added' | 'removed') =>
  Array.from(container.querySelectorAll(`.git-diff-code.${kind}`)).map((el) => el.textContent?.slice(1));

describe('ApprovalBanner diff preview', () => {
  beforeEach(() => {
    for (const id of useRuntimeIndex.getState().liveIds) dropRuntime(id);
    useSessionStore.setState({ currentSessionId: SID, liveSessions: {}, sessions: [] });
  });

  it('draws an edit in the shared unified grid under its path and counts', () => {
    getChatStore(SID).getState().enqueueApproval(
      writeApproval('tu_w', { path: 'a.txt', original: 'one\ntwo\nthree\n', content: 'one\nTWO\nthree\n' }),
    );

    const { container, getByText } = render(<ApprovalBanner />);

    expect(container.querySelector('.git-diff-preview .git-diff-grid.unified')).toBeTruthy();
    expect(cells(container, 'removed')).toEqual(['two']);
    expect(cells(container, 'added')).toEqual(['TWO']);
    expect(getByText('+1')).toBeTruthy();
    expect(getByText('-1')).toBeTruthy();
    // No dialog chrome inside the card.
    expect(container.querySelector('.git-diff-header')).toBeNull();
  });

  it('draws a created file as all additions', () => {
    getChatStore(SID).getState().enqueueApproval(
      writeApproval('tu_new', { path: 'new.txt', content: 'a\nb\n' }),
    );

    const { container, getByText } = render(<ApprovalBanner />);

    expect(cells(container, 'added')).toEqual(['a', 'b']);
    expect(cells(container, 'removed')).toEqual([]);
    expect(getByText('+2')).toBeTruthy();
  });

  it('says a write that changes nothing has no changes', () => {
    const text = Array.from({ length: 20 }, (_, i) => `line ${i + 1}`).join('\n');
    getChatStore(SID).getState().enqueueApproval(
      writeApproval('tu_same', { path: 'same.txt', original: text, content: text }),
    );

    const { container, getByText } = render(<ApprovalBanner />);

    expect(getByText('No changes')).toBeTruthy();
    expect(cells(container, 'added')).toEqual([]);
    expect(cells(container, 'removed')).toEqual([]);
  });

  it('keeps the card answerable when the diff is too large: the new text shows instead', () => {
    const n = Math.ceil(Math.sqrt(MAX_DIFF_CELLS)) + 10;
    const side = (p: string) => Array.from({ length: n }, (_, i) => `${p}${i}`).join('\n');
    getChatStore(SID).getState().enqueueApproval(
      writeApproval('tu_big', { path: 'big.txt', original: side('l'), content: side('r') }),
    );

    const { container } = render(<ApprovalBanner />);

    expect(container.querySelector('.git-diff-grid')).toBeNull();
    expect(container.querySelector('pre')?.textContent?.startsWith('r0\nr1\n')).toBe(true);
    expect(screen.getByRole('button', { name: 'Yes' })).toBeTruthy();
  });
});
