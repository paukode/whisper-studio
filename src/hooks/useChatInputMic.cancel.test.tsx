/**
 * Regression test for the mic "stuck state" bug in useChatInputMic.
 *
 * `start()` is async: it awaits the WebSocket open, then acquireMic()
 * (getUserMedia), then the worklet module. If the user CANCELS (stop()) while
 * start() is still connecting, start() used to resume after the teardown ran
 * and still set isRecording=true on an already-closed socket, while the
 * getUserMedia track it acquired was never stopped — leaving the mic device
 * open with no way to reach it.
 *
 * These tests drive the hook with a deferred getUserMedia so the cancel lands
 * mid-connect, then assert the in-flight start() bails cleanly: isRecording
 * stays false, the acquired mic track is stopped, and the socket is closed.
 */
import { renderHook, act } from '@testing-library/react';
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { WebSocketMock } from '@/test/mocks/webSocket';
import { useRecordingStore } from '@/stores/recordingStore';
import { useUIStore } from '@/stores/uiStore';
import { __resetNativeMicForTests } from '@/services/nativeMicSource';
import { useChatInputMic } from './useChatInputMic';

// --- Minimal Web Audio mocks (jsdom has none) --------------------------------
class AudioContextMock {
  state: 'running' | 'closed' = 'running';
  destination = {};
  audioWorklet = { addModule: vi.fn(() => Promise.resolve()) };
  close = vi.fn(() => {
    this.state = 'closed';
    return Promise.resolve();
  });
  createMediaStreamSource = vi.fn(() => ({ connect: vi.fn() }));
}

class AudioWorkletNodeMock {
  static instances: AudioWorkletNodeMock[] = [];
  constructor() {
    AudioWorkletNodeMock.instances.push(this);
  }
  port: { onmessage: ((e: MessageEvent) => void) | null; postMessage: (m: unknown) => void } = {
    onmessage: null,
    postMessage: vi.fn(),
  };
  connect = vi.fn();
  disconnect = vi.fn();
}

/** Build a MediaStream stand-in whose single track records stop() calls. */
function makeStream() {
  const track = { stop: vi.fn(), kind: 'audio' };
  const stream = { getTracks: () => [track] } as unknown as MediaStream;
  return { stream, track };
}

/** A promise plus its resolver, for deferring getUserMedia until we choose. */
function deferred<T>() {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

const tick = () => act(async () => {
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
});

const setGetUserMedia = (impl: () => Promise<MediaStream>) => {
  Object.defineProperty(navigator, 'mediaDevices', {
    value: { getUserMedia: vi.fn(impl) },
    configurable: true,
    writable: true,
  });
};

describe('useChatInputMic — cancel during connect', () => {
  let closeSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    (globalThis as unknown as { AudioContext: unknown }).AudioContext = AudioContextMock;
    (globalThis as unknown as { AudioWorkletNode: unknown }).AudioWorkletNode = AudioWorkletNodeMock;
    closeSpy = vi.spyOn(WebSocketMock.prototype, 'close');
  });

  afterEach(() => {
    closeSpy.mockRestore();
    // Reset the module-level refcount/singleton state in the recording store.
    useRecordingStore.getState().cleanup();
  });

  it('cancel while acquiring the mic: never records, stops the track, closes the socket', async () => {
    const gum = deferred<MediaStream>();
    const { stream, track } = makeStream();
    setGetUserMedia(() => gum.promise);

    const { result } = renderHook(() => useChatInputMic({ onTranscript: vi.fn() }));

    // Kick off start(); let the socket open (mock opens on a 0ms timer) so
    // start() advances to the (still-pending) getUserMedia await.
    await act(async () => {
      void result.current.start();
      await new Promise((r) => setTimeout(r, 0));
      await Promise.resolve();
      await Promise.resolve();
    });

    // We are mid-connect: getUserMedia was reached but has not resolved.
    expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(1);
    expect(result.current.isRecording).toBe(false);
    expect(result.current.isConnecting).toBe(true);

    // Cancel while the acquire is in flight.
    act(() => {
      result.current.stop();
    });
    expect(result.current.isRecording).toBe(false);
    expect(closeSpy).toHaveBeenCalled(); // socket torn down by the cancel

    // Now the deferred getUserMedia resolves — start() must bail, NOT activate.
    await act(async () => {
      gum.resolve(stream);
    });
    await tick();

    expect(result.current.isRecording).toBe(false);
    expect(result.current.isConnecting).toBe(false);
    // The mic device acquired during connect is released, not left open.
    expect(track.stop).toHaveBeenCalled();
  });

  it('cancel while the socket is opening: mic is never acquired', async () => {
    const { stream } = makeStream();
    setGetUserMedia(() => Promise.resolve(stream));

    const { result } = renderHook(() => useChatInputMic({ onTranscript: vi.fn() }));

    // Begin start() and cancel synchronously, before the open timer fires.
    act(() => {
      void result.current.start();
      result.current.stop();
    });

    // Let the socket-open timer fire; start() resumes and must bail.
    await act(async () => {
      await new Promise((r) => setTimeout(r, 0));
    });
    await tick();

    expect(result.current.isRecording).toBe(false);
    // Bailed before touching the mic — getUserMedia never called.
    expect(navigator.mediaDevices.getUserMedia).not.toHaveBeenCalled();
    expect(closeSpy).toHaveBeenCalled();
  });

  it('a cancelled start that resumes late leaves the next start running', async () => {
    const gum = deferred<MediaStream>();
    const { stream, track } = makeStream();
    setGetUserMedia(() => gum.promise);

    const { result } = renderHook(() => useChatInputMic({ onTranscript: vi.fn() }));

    // Start, let the socket open, then cancel while the mic is opening.
    await act(async () => {
      void result.current.start();
      await new Promise((r) => setTimeout(r, 0));
    });
    act(() => result.current.stop());

    // Start again before the first mic open lands; it joins the same open.
    await act(async () => {
      void result.current.start();
      await new Promise((r) => setTimeout(r, 0));
    });
    const closesBefore = closeSpy.mock.calls.length;

    await act(async () => {
      gum.resolve(stream);
    });
    await tick();

    // The first start unwound without touching the second one's socket or mic.
    expect(result.current.isRecording).toBe(true);
    expect(closeSpy.mock.calls.length).toBe(closesBefore);
    expect(track.stop).not.toHaveBeenCalled();

    act(() => result.current.stop());
    await tick();
    expect(track.stop).toHaveBeenCalled();
  });

  it('happy path still works: start records, stop tears down and releases the mic', async () => {
    const { stream, track } = makeStream();
    setGetUserMedia(() => Promise.resolve(stream));

    const { result } = renderHook(() => useChatInputMic({ onTranscript: vi.fn() }));

    await act(async () => {
      await result.current.start();
    });

    expect(result.current.isRecording).toBe(true);
    expect(track.stop).not.toHaveBeenCalled();

    act(() => {
      result.current.stop();
    });
    await tick();

    expect(result.current.isRecording).toBe(false);
    expect(track.stop).toHaveBeenCalled();
  });

  it('a Stop while the model loads takes its loading banner down', async () => {
    // Nothing on the closed socket clears the banner after a Stop, so the
    // teardown ends dictation's run on it.
    const { stream } = makeStream();
    setGetUserMedia(() => Promise.resolve(stream));
    const listen = vi.spyOn(WebSocketMock.prototype, 'addEventListener');
    const { result } = renderHook(() => useChatInputMic({ onTranscript: vi.fn() }));
    await act(async () => {
      await result.current.start();
    });
    const socket = listen.mock.contexts[0] as WebSocketMock;

    act(() => socket._receiveMessage({ type: 'model_loading', label: 'Parakeet', progress: 0.4, stage: 'loading' }));
    expect(useUIStore.getState().modelLoading?.producer).toBe('dictation');

    act(() => {
      result.current.stop();
    });
    await tick();

    expect(useUIStore.getState().modelLoading).toBeNull();
    listen.mockRestore();
  });
});

describe('useChatInputMic: the server closes the socket', () => {
  const RealWebSocket = globalThis.WebSocket;
  let sockets: WebSocketMock[] = [];

  beforeEach(() => {
    (globalThis as unknown as { AudioContext: unknown }).AudioContext = AudioContextMock;
    (globalThis as unknown as { AudioWorkletNode: unknown }).AudioWorkletNode = AudioWorkletNodeMock;
    sockets = [];
    class Capturing extends WebSocketMock {
      constructor(url: string) {
        super(url);
        sockets.push(this);
      }
    }
    globalThis.WebSocket = Capturing as unknown as typeof WebSocket;
  });

  afterEach(() => {
    globalThis.WebSocket = RealWebSocket;
    useRecordingStore.getState().cleanup();
  });

  /** What a server-side close looks like to the page: the socket is closed
   *  and its close event fires, with no stop() from the user. */
  const serverClose = (ws: WebSocketMock) => {
    ws.readyState = WebSocketMock.CLOSED;
    ws.onclose?.(new CloseEvent('close', { code: 1011 }));
  };

  it('ends dictation the way Stop does: not recording, mic released, no audio piling up', async () => {
    const { stream, track } = makeStream();
    setGetUserMedia(() => Promise.resolve(stream));
    const { result } = renderHook(() => useChatInputMic({ onTranscript: vi.fn() }));
    await act(async () => {
      await result.current.start();
    });
    expect(result.current.isRecording).toBe(true);
    const node = AudioWorkletNodeMock.instances[AudioWorkletNodeMock.instances.length - 1];
    expect(node.port.onmessage).not.toBeNull();

    act(() => serverClose(sockets[0]));
    await tick();

    expect(result.current.isRecording).toBe(false);
    expect(result.current.isConnecting).toBe(false);
    expect(track.stop).toHaveBeenCalled();
    // The worklet no longer feeds a buffer nobody will send.
    expect(node.port.onmessage).toBeNull();
    expect(node.disconnect).toHaveBeenCalled();
  });

  it('a close while the mic is still opening cancels the start', async () => {
    const gum = deferred<MediaStream>();
    const { stream, track } = makeStream();
    setGetUserMedia(() => gum.promise);
    const { result } = renderHook(() => useChatInputMic({ onTranscript: vi.fn() }));
    await act(async () => {
      void result.current.start();
      await new Promise((r) => setTimeout(r, 0));
    });
    expect(result.current.isConnecting).toBe(true);

    act(() => serverClose(sockets[0]));
    await act(async () => {
      gum.resolve(stream);
    });
    await tick();

    expect(result.current.isRecording).toBe(false);
    expect(result.current.isConnecting).toBe(false);
    expect(track.stop).toHaveBeenCalled();
  });
});

describe('useChatInputMic in the Mac app', () => {
  const sockets: WebSocketMock[] = [];
  let RealWS: typeof WebSocket;
  class TrackedSocket extends WebSocketMock {
    constructor(url: string) {
      super(url);
      sockets.push(this);
    }
  }

  beforeEach(() => {
    sockets.length = 0;
    RealWS = globalThis.WebSocket;
    globalThis.WebSocket = TrackedSocket as unknown as typeof WebSocket;
    __resetNativeMicForTests();
    window.__WHISPER_NATIVE_MIC = { available: true };
    (window as unknown as { webkit: unknown }).webkit = {
      messageHandlers: {
        nativeMic: {
          postMessage: (msg: { cmd: string }) => {
            if (msg.cmd === 'start') queueMicrotask(() => window.__whisperNativeMic!.onStarted('Mic'));
            if (msg.cmd === 'stop') queueMicrotask(() => window.__whisperNativeMic!.onStopped());
          },
        },
      },
    };
    setGetUserMedia(() => Promise.reject(new Error('getUserMedia must not be used')));
  });

  afterEach(() => {
    globalThis.WebSocket = RealWS;
    __resetNativeMicForTests();
    delete window.__WHISPER_NATIVE_MIC;
  });

  it('dictates from the shell mic, never the web view mic, and sends its audio', async () => {
    const { result } = renderHook(() => useChatInputMic({ onTranscript: vi.fn() }));
    await act(async () => {
      void result.current.start();
      await new Promise((r) => setTimeout(r, 0));
    });
    await tick();
    expect(result.current.isRecording).toBe(true);
    expect(navigator.mediaDevices.getUserMedia).not.toHaveBeenCalled();

    // Half a second of 16 kHz audio fills one chunk.
    const pcm = new Int16Array(8000).fill(1000);
    let binary = '';
    for (const byte of new Uint8Array(pcm.buffer)) binary += String.fromCharCode(byte);
    act(() => window.__whisperNativeMic!.onAudio(btoa(binary), 8000, 0.1));

    const ws = sockets[sockets.length - 1];
    const audio = ws._getSentMessages().filter((m): m is ArrayBuffer => typeof m !== 'string');
    expect(audio).toHaveLength(1);
    expect(audio[0].byteLength).toBe(16000);

    act(() => result.current.stop());
    expect(result.current.isRecording).toBe(false);
  });
});
