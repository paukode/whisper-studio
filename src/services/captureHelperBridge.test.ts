import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { dispatchHelperMessage, installCaptureHelperBridge } from './captureHelperBridge';

type Win = Record<string, unknown> & {
  webkit?: { messageHandlers?: Record<string, { postMessage: (m: unknown) => void }> };
};
const win = window as unknown as Win;

class FakeSocket {
  static OPEN = 1;
  static last: FakeSocket | null = null;
  readyState = 0;
  sent: string[] = [];
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onmessage: ((e: MessageEvent) => void) | null = null;
  constructor(public url: string) {
    FakeSocket.last = this;
    queueMicrotask(() => {
      this.readyState = FakeSocket.OPEN;
      this.onopen?.();
    });
  }
  send(data: string) {
    this.sent.push(data);
  }
}

function mockStatus(available: boolean) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => new Response(JSON.stringify({ available }), { status: 200 })),
  );
}

beforeEach(() => {
  FakeSocket.last = null;
  vi.stubGlobal('WebSocket', FakeSocket);
  delete win.webkit;
  delete win.__WHISPER_NATIVE_AUDIO;
  delete win.__WHISPER_NATIVE_MIC;
  delete win.__whisperNativeAudio;
  delete win.__whisperNativeMic;
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('dispatchHelperMessage', () => {
  it('calls the callback of the message channel with its args', () => {
    const onAudio = vi.fn();
    const onStarted = vi.fn();
    win.__whisperNativeAudio = { onAudio };
    win.__whisperNativeMic = { onStarted };

    dispatchHelperMessage(
      JSON.stringify({ channel: 'audio', event: 'onAudio', args: ['AAA=', 2, 0.5] }),
    );
    dispatchHelperMessage(
      JSON.stringify({ channel: 'mic', event: 'onStarted', args: ['Built-in'] }),
    );

    expect(onAudio).toHaveBeenCalledWith('AAA=', 2, 0.5);
    expect(onStarted).toHaveBeenCalledWith('Built-in');
  });

  it('logs an unreadable line instead of throwing', () => {
    const error = vi.spyOn(console, 'error').mockImplementation(() => {});
    expect(() => dispatchHelperMessage('not json')).not.toThrow();
    expect(error).toHaveBeenCalled();
    error.mockRestore();
  });
});

describe('installCaptureHelperBridge', () => {
  it('installs the same bridge globals the app window injects', async () => {
    mockStatus(true);

    await installCaptureHelperBridge();

    expect(win.__WHISPER_NATIVE_AUDIO).toEqual({ available: true });
    expect(win.__WHISPER_NATIVE_MIC).toEqual({ available: true });
    expect(FakeSocket.last?.url).toMatch(/\/ws\/capture-helper$/);
    win.webkit?.messageHandlers?.nativeAudio.postMessage({ cmd: 'start', pid: -1 });
    win.webkit?.messageHandlers?.nativeMic.postMessage({ cmd: 'stop' });
    expect(FakeSocket.last?.sent.map((s) => JSON.parse(s))).toEqual([
      { cmd: 'start', pid: -1, channel: 'audio' },
      { cmd: 'stop', channel: 'mic' },
    ]);
  });

  it('marks the bridges unavailable when the helper connection closes', async () => {
    mockStatus(true);
    await installCaptureHelperBridge();

    FakeSocket.last?.onclose?.();

    expect(win.__WHISPER_NATIVE_AUDIO).toEqual({ available: false });
    expect(win.__WHISPER_NATIVE_MIC).toEqual({ available: false });
  });

  it('does nothing when the server has no helper', async () => {
    mockStatus(false);

    await installCaptureHelperBridge();

    expect(FakeSocket.last).toBeNull();
    expect(win.__WHISPER_NATIVE_AUDIO).toBeUndefined();
  });

  it('does nothing inside the app window, which has its own bridges', async () => {
    const fetchSpy = vi.fn();
    vi.stubGlobal('fetch', fetchSpy);
    win.webkit = { messageHandlers: { nativeAudio: { postMessage: () => {} } } };

    await installCaptureHelperBridge();

    expect(fetchSpy).not.toHaveBeenCalled();
    expect(FakeSocket.last).toBeNull();
  });
});
