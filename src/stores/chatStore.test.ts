/**
 * Cover the parts of chatStore that drive the most user-visible behaviour:
 * message append, approval queue ordering, and finishStream atomicity.
 *
 * Each test resets the store so cases stay isolated.
 */
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { createChatStore, type PendingApproval } from './chatStore';
import type { ChatMessage } from '@/types/chat';

let store: ReturnType<typeof createChatStore>;

function reset() {
  // A fresh store per test — exactly how the runtime registry hands one
  // to each session.
  store = createChatStore();
}

const userMessage = (text: string): ChatMessage => ({
  role: 'user',
  content: text,
  timestamp: new Date().toISOString(),
});

beforeEach(reset);
afterEach(reset);

describe('chatStore — messages', () => {
  it('addMessage appends to the messages list', () => {
    store.getState().addMessage(userMessage('hello'));
    store.getState().addMessage(userMessage('world'));
    expect(store.getState().messages.map((m) => m.content)).toEqual(['hello', 'world']);
  });

  it('clearMessages wipes messages and stream state', () => {
    store.getState().addMessage(userMessage('hello'));
    store.setState({
      isStreaming: true,
      currentStreamContent: 'partial',
      currentThinkingContent: 'partial-thinking',
    });
    store.getState().clearMessages();
    const s = store.getState();
    expect(s.messages).toEqual([]);
    expect(s.isStreaming).toBe(false);
    expect(s.currentStreamContent).toBe('');
    expect(s.currentThinkingContent).toBe('');
  });
});

describe('chatStore — finishStream atomicity', () => {
  it('clears stream state and appends the final message in one update', () => {
    store.setState({
      isStreaming: true,
      currentStreamContent: 'streaming...',
      currentThinkingContent: 'thinking...',
    });
    const final: ChatMessage = {
      role: 'assistant',
      content: 'final answer',
      timestamp: new Date().toISOString(),
    };
    store.getState().finishStream(final);
    const s = store.getState();
    expect(s.isStreaming).toBe(false);
    expect(s.currentStreamContent).toBe('');
    expect(s.currentThinkingContent).toBe('');
    expect(s.messages).toHaveLength(1);
    expect(s.messages[0]).toBe(final);
  });

  it('without a message argument it just clears stream state', () => {
    store.setState({ isStreaming: true, currentStreamContent: 'x' });
    store.getState().finishStream();
    expect(store.getState().isStreaming).toBe(false);
    expect(store.getState().messages).toEqual([]);
  });
});

describe('chatStore — approval queue', () => {
  const make = (id: string, action = 'write'): PendingApproval => ({
    toolUseId: id,
    action,
    category: action === 'cli' || action === 'command' ? 'cli' : action === 'delete' ? 'delete' : 'write',
    preview: 'diff',
    summary: `Write /tmp/${id}`,
    payload: { path: '/tmp/' + id },
    sessionId: 'sess1',
  });

  it('first enqueued approval becomes the current; siblings queue', () => {
    store.getState().enqueueApproval(make('a'));
    store.getState().enqueueApproval(make('b'));
    store.getState().enqueueApproval(make('c'));
    const s = store.getState();
    expect(s.currentApproval?.toolUseId).toBe('a');
    expect(s.approvalQueue.map((a) => a.toolUseId)).toEqual(['b', 'c']);
  });

  it('showNextApproval drains the queue in FIFO order', () => {
    store.getState().enqueueApproval(make('a'));
    store.getState().enqueueApproval(make('b'));
    store.getState().enqueueApproval(make('c'));

    store.getState().showNextApproval();
    expect(store.getState().currentApproval?.toolUseId).toBe('b');
    expect(store.getState().approvalQueue.map((a) => a.toolUseId)).toEqual(['c']);

    store.getState().showNextApproval();
    expect(store.getState().currentApproval?.toolUseId).toBe('c');
    expect(store.getState().approvalQueue).toEqual([]);

    store.getState().showNextApproval();
    expect(store.getState().currentApproval).toBeNull();
  });

  // Routing on session memory (allow → execute, deny → refuse) is the CALLER's
  // job — sseStream decides before it ever enqueues. enqueueApproval used to
  // re-check `sessionApprovals` and return early for those two modes, so
  // anything that reached it with a pre-approved (or pre-blocked) category
  // vanished: no card, no queue entry, and a paused turn with nothing left to
  // resume it. Showing a card the user may not have needed is the safe failure.
  it('shows an approval whose category is already allowed rather than dropping it', () => {
    store.setState({
      sessionApprovals: { write: 'allow', delete: 'ask', cli: 'ask' },
    });
    store.getState().enqueueApproval(make('write-1', 'write'));
    expect(store.getState().currentApproval?.toolUseId).toBe('write-1');
  });

  it('shows an approval whose category is blocked rather than dropping it', () => {
    store.setState({
      sessionApprovals: { write: 'deny', delete: 'ask', cli: 'ask' },
    });
    store.getState().enqueueApproval(make('write-2', 'write'));
    expect(store.getState().currentApproval?.toolUseId).toBe('write-2');
  });
});

describe('chatStore — session usage totals', () => {
  it('accumulates each turn into the session totals', () => {
    // Turn 1, streamed as two usage frames (running totals within the turn).
    store.getState().setUsage(40_000, 500, 0.2, 40_000, 200_000);
    store.getState().setUsage(60_000, 2_000, 0.3, 60_000, 200_000);
    expect(store.getState().sessionInputTokens).toBe(60_000);
    expect(store.getState().sessionOutputTokens).toBe(2_000);
    expect(store.getState().sessionCost).toBeCloseTo(0.3, 6);

    // Turn 2 — setStreaming clears the per-turn fields, so the next frame's
    // totals must add to the session rather than replace it.
    store.getState().setStreaming(true);
    store.getState().setUsage(58_400, 2_012, 0.1679, 58_400, 200_000);
    expect(store.getState().inputTokens).toBe(58_400);
    expect(store.getState().sessionInputTokens).toBe(118_400);
    expect(store.getState().sessionOutputTokens).toBe(4_012);
    expect(store.getState().sessionCost).toBeCloseTo(0.4679, 6);
  });

  it('never subtracts when a turn restarts mid-flight', () => {
    store.getState().setUsage(50_000, 1_000, 0.25);
    // An approval resume re-enters the loop and starts its own count lower;
    // the session total holds and grows from there.
    store.getState().setUsage(10_000, 100, 0.05);
    expect(store.getState().sessionInputTokens).toBe(50_000);
    expect(store.getState().sessionCost).toBeCloseTo(0.25, 6);
  });

  it('hydrateSessionUsage seeds totals without lowering a live count', () => {
    store.getState().hydrateSessionUsage(500_000, 20_000, 3.5);
    expect(store.getState().sessionInputTokens).toBe(500_000);
    expect(store.getState().sessionOutputTokens).toBe(20_000);
    expect(store.getState().sessionCost).toBeCloseTo(3.5, 6);

    // A turn that streamed while the fetch was in flight stays counted.
    store.getState().hydrateSessionUsage(400_000, 10_000, 2.0);
    expect(store.getState().sessionInputTokens).toBe(500_000);
    expect(store.getState().sessionCost).toBeCloseTo(3.5, 6);
  });
});
