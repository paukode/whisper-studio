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

  it('session-allow short-circuits enqueue (caller auto-applies)', () => {
    // Pre-set a category-level approval so the matching action gets dropped
    // from the queue (handled by the caller path instead of the modal).
    store.setState({
      sessionApprovals: { write: 'allow', delete: 'ask', cli: 'ask' },
    });
    store.getState().enqueueApproval(make('write-1', 'write'));
    expect(store.getState().currentApproval).toBeNull();
    expect(store.getState().approvalQueue).toEqual([]);
  });
});
