import { describe, it, expect, vi, beforeEach } from 'vitest';

const { postMock } = vi.hoisted(() => ({ postMock: vi.fn() }));
vi.mock('@/api/client', () => ({ post: postMock }));

import { runCompact } from './compactCommand';
import { getChatStore } from '@/stores/sessionRuntimes';
import type { ChatMessage } from '@/types/chat';

const SID = 'sess-compact';
const row = (role: ChatMessage['role'], content: string): ChatMessage => ({
  role, content, timestamp: `2026-11-04T10:00:0${content.length % 10}Z`,
});

describe('/compact', () => {
  const addToast = vi.fn();
  beforeEach(() => {
    postMock.mockReset();
    addToast.mockReset();
    getChatStore(SID).setState({ messages: [row('user', 'q'), row('assistant', 'a')], isStreaming: false });
  });

  it('sends every row and the note, then shows the summary row', async () => {
    const summary: ChatMessage = { ...row('compact_summary', 'summary'), compactSummary: { note: 'keep x', messages: 2, before_chars: 100, after_chars: 10 } };
    postMock.mockResolvedValue(summary);
    await runCompact(SID, 'keep x', 'opus', addToast);
    expect(postMock).toHaveBeenCalledWith(`/api/sessions/${SID}/compact`, {
      history: [{ role: 'user', content: 'q' }, { role: 'assistant', content: 'a' }],
      note: 'keep x',
      model: 'opus',
    }, { longLived: true });
    const { messages } = getChatStore(SID).getState();
    expect(messages[messages.length - 1]).toEqual(summary);
  });

  it('runs one compaction at a time per session', async () => {
    let finish!: (v: ChatMessage) => void;
    postMock.mockReturnValueOnce(new Promise((r) => { finish = r; }));
    const first = runCompact(SID, '', 'opus', addToast);
    await runCompact(SID, '', 'opus', addToast);
    expect(postMock).toHaveBeenCalledTimes(1);
    expect(addToast).toHaveBeenLastCalledWith(expect.objectContaining({ message: expect.stringContaining('already') }));

    finish(row('compact_summary', 'summary'));
    await first;
    // Once it ended, /compact runs again (on a conversation that went on).
    getChatStore(SID).getState().addMessage(row('user', 'more'));
    postMock.mockResolvedValueOnce(row('compact_summary', 'again'));
    await runCompact(SID, '', 'opus', addToast);
    expect(postMock).toHaveBeenCalledTimes(2);
  });

  it('waits while a reply is streaming and changes nothing on failure', async () => {
    getChatStore(SID).setState({ isStreaming: true });
    await runCompact(SID, '', 'opus', addToast);
    expect(postMock).not.toHaveBeenCalled();

    getChatStore(SID).setState({ isStreaming: false });
    postMock.mockRejectedValue(new Error('Context compaction failed'));
    await runCompact(SID, '', 'opus', addToast);
    expect(getChatStore(SID).getState().messages).toHaveLength(2);
    expect(addToast).toHaveBeenLastCalledWith(expect.objectContaining({ type: 'error' }));
  });
});
