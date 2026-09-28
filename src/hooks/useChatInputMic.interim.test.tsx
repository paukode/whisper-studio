/**
 * The dictation socket forwards every interim draft, the empty one included:
 * an empty interim is how the server withdraws a draft whose utterance
 * closed without a final, and dropping it left the phantom words in the
 * composer (see useDictationInput).
 */
import { act, renderHook } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { WebSocketMock } from '@/test/mocks/webSocket';
import { useRecordingStore } from '@/stores/recordingStore';
import { useChatInputMic } from './useChatInputMic';

describe('useChatInputMic interim drafts', () => {
  const RealWebSocket = globalThis.WebSocket;
  const sockets: WebSocketMock[] = [];

  afterEach(() => {
    globalThis.WebSocket = RealWebSocket;
    sockets.length = 0;
    useRecordingStore.getState().cleanup();
  });

  it('forwards an empty interim so the composer can withdraw the draft', async () => {
    class Capturing extends WebSocketMock {
      constructor(url: string) {
        super(url);
        sockets.push(this);
      }
    }
    globalThis.WebSocket = Capturing as unknown as typeof WebSocket;
    // The mic is never granted: the socket's message handler is what is under test.
    Object.defineProperty(navigator, 'mediaDevices', {
      value: { getUserMedia: vi.fn(() => new Promise(() => {})) },
      configurable: true,
      writable: true,
    });
    const onTranscript = vi.fn();
    const { result } = renderHook(() => useChatInputMic({ onTranscript }));
    await act(async () => {
      void result.current.start();
      await new Promise((r) => setTimeout(r, 0));
    });
    const ws = sockets[0];
    act(() => {
      ws.onmessage?.(new MessageEvent('message', { data: JSON.stringify({ type: 'interim', text: 'um' }) }));
      ws.onmessage?.(new MessageEvent('message', { data: JSON.stringify({ type: 'interim', text: '' }) }));
    });
    expect(onTranscript.mock.calls).toEqual([
      ['um', false],
      ['', false],
    ]);
    act(() => result.current.stop());
  });
});
