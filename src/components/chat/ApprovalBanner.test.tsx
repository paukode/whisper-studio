/**
 * The approval card's FAILURE path, which used to strand the paused turn.
 *
 * `executeApproval` throws an ApiError on any non-2xx / network failure. When
 * that rejection escaped the click handler, three things happened at once and
 * none of them were visible: the continuation was never sent (so the model was
 * never told the user approved), `isProcessing` stayed true (so the next card
 * rendered with every button disabled as "Running…"), and queued approvals were
 * never surfaced. The user's only clue was a chat that had gone quiet.
 */
import { render, fireEvent, waitFor } from '@testing-library/react';
import { describe, it, expect, beforeEach, vi } from 'vitest';
import type { PendingApproval } from '@/stores/chatStore';
import { dropRuntime, getChatStore, useRuntimeIndex } from '@/stores/sessionRuntimes';
import { useSessionStore } from '@/stores/sessionStore';
import { ApprovalBanner } from './ApprovalBanner';

const executeApproval = vi.fn();
const sendApprovalContinuation = vi.fn();

vi.mock('@/api/approval', () => ({
  executeApproval: (...args: unknown[]) => executeApproval(...args),
}));
vi.mock('@/hooks/useChatStream', () => ({
  sendApprovalContinuation: (...args: unknown[]) => sendApprovalContinuation(...args),
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
  };
}

describe('ApprovalBanner', () => {
  beforeEach(() => {
    for (const id of useRuntimeIndex.getState().liveIds) dropRuntime(id);
    useSessionStore.setState({ currentSessionId: SID, liveSessions: {}, sessions: [] });
    executeApproval.mockReset();
    sendApprovalContinuation.mockReset().mockResolvedValue(undefined);
  });

  it('still resumes the turn when executing the approved action throws', async () => {
    executeApproval.mockRejectedValue(new Error('HTTP 500: executor unreachable'));
    getChatStore(SID).getState().enqueueApproval(approval('tu_1'));

    const { getByText } = render(<ApprovalBanner />);
    fireEvent.click(getByText('✓ Yes'));

    await waitFor(() => expect(sendApprovalContinuation).toHaveBeenCalledTimes(1));
    const [appr, sid, accepted, , outcome] = sendApprovalContinuation.mock.calls[0];
    expect((appr as PendingApproval).toolUseId).toBe('tu_1');
    expect(sid).toBe(SID);
    expect(accepted).toBe(true);
    // Truthful outcome: approved, but the operation did NOT happen.
    expect(outcome).toMatchObject({ ok: false });
    expect(String((outcome as { error: string }).error)).toContain('executor unreachable');
  });

  it('recovers the card UI after a failure so a queued approval stays actionable', async () => {
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
    executeApproval.mockResolvedValue({ ok: true, output: 'Created branch docs/x' });
    getChatStore(SID).getState().enqueueApproval(approval('tu_ok'));

    const { getByText } = render(<ApprovalBanner />);
    fireEvent.click(getByText('✓ Yes'));

    await waitFor(() => expect(sendApprovalContinuation).toHaveBeenCalledTimes(1));
    const [, , accepted, , outcome] = sendApprovalContinuation.mock.calls[0];
    expect(accepted).toBe(true);
    expect(outcome).toMatchObject({ ok: true, output: 'Created branch docs/x' });
    expect(getChatStore(SID).getState().currentApproval).toBeNull();
  });
});
