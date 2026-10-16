/**
 * Inside the Mac app the recorder reads the microphone through the shell
 * (nativeMicSource.ts), never getUserMedia: WebKit's voice-processing mic
 * opened late, came in quiet for its first seconds, and fought Teams or Zoom
 * for the audio route. Drives the real controller against a fake shell.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/services/recordingModels', () => ({
  ensureRecordingModels: vi.fn(async () => 'ready'),
}));

import { recordingController } from './recordingController';
import { __resetNativeAudioForTests } from './nativeAudioSource';
import { __resetNativeMicForTests } from './nativeMicSource';
import { useRecordingStore } from '@/stores/recordingStore';
import { useSessionStore } from '@/stores/sessionStore';
import { useUIStore } from '@/stores/uiStore';
import { WebSocketMock } from '@/test/mocks/webSocket';
import { answerStopLikeServer } from '@/test/mocks/asrServer';

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

let sockets: ServerSocket[] = [];
class ServerSocket extends WebSocketMock {
  constructor(url: string) {
    super(url);
    sockets.push(this);
  }
  send(data: string | ArrayBuffer): void {
    super.send(data);
    answerStopLikeServer(this, data);
  }
  audio(): ArrayBuffer[] {
    return this._getSentMessages().filter((m): m is ArrayBuffer => typeof m !== 'string');
  }
  marks(): Record<string, unknown>[] {
    return this._getSentMessages()
      .filter((m): m is string => typeof m === 'string')
      .map((m) => JSON.parse(m) as Record<string, unknown>)
      .filter((m) => m.type === 'client_mark');
  }
}
let RealWS: typeof WebSocket;
/** Every message the page posted to either shell bridge, in order. */
let posted: string[] = [];
let getUserMedia: ReturnType<typeof vi.fn>;

function chunk(samples: number, value: number): string {
  const pcm = new Int16Array(samples).fill(value);
  let binary = '';
  for (const byte of new Uint8Array(pcm.buffer)) binary += String.fromCharCode(byte);
  return btoa(binary);
}

beforeEach(() => {
  sockets = [];
  posted = [];
  __resetNativeAudioForTests();
  __resetNativeMicForTests();
  window.__WHISPER_NATIVE_AUDIO = { platform: 'macos', available: true };
  window.__WHISPER_NATIVE_MIC = { platform: 'macos', available: true };
  (window as unknown as { webkit: unknown }).webkit = {
    messageHandlers: {
      nativeAudio: {
        postMessage: (msg: { cmd: string }) => {
          posted.push(`audio:${msg.cmd}`);
          if (msg.cmd === 'start') queueMicrotask(() => window.__whisperNativeAudio!.onStarted());
          if (msg.cmd === 'stop') queueMicrotask(() => window.__whisperNativeAudio!.onStopped());
        },
      },
      nativeMic: {
        postMessage: (msg: { cmd: string }) => {
          posted.push(`mic:${msg.cmd}`);
          if (msg.cmd === 'start')
            queueMicrotask(() => window.__whisperNativeMic!.onStarted('MacBook Pro Microphone'));
          if (msg.cmd === 'stop') queueMicrotask(() => window.__whisperNativeMic!.onStopped());
        },
      },
    },
  };
  getUserMedia = vi.fn();
  Object.defineProperty(navigator, 'mediaDevices', {
    configurable: true,
    value: { getUserMedia },
  });
  RealWS = globalThis.WebSocket;
  globalThis.WebSocket = ServerSocket as unknown as typeof WebSocket;
  useSessionStore.setState({ saveSession: vi.fn(), debouncedSave: vi.fn() });
  useUIStore.setState({ addToast: vi.fn(() => 'toast') });
  useRecordingStore.setState({
    isRecording: false,
    isConnected: false,
    recordingSessionId: null,
    nativeSource: null,
    micEnabled: true,
    activeSourceLabel: null,
  });
});

afterEach(async () => {
  if (useRecordingStore.getState().isRecording) {
    recordingController.stop();
    await sleep(300);
  }
  globalThis.WebSocket = RealWS;
  __resetNativeAudioForTests();
  __resetNativeMicForTests();
  delete window.__WHISPER_NATIVE_AUDIO;
  delete window.__WHISPER_NATIVE_MIC;
  delete (window as { webkit?: unknown }).webkit;
  vi.restoreAllMocks();
});

describe('native microphone in the Mac app', () => {
  it('records the shell mic, never getUserMedia, and closes it on stop', async () => {
    await recordingController.start('mic-session');
    await sleep(10);
    expect(getUserMedia).not.toHaveBeenCalled();
    expect(posted).toEqual(['mic:start']);
    const sock = sockets[0];
    const ready = sock.marks().find((m) => m.mark === 'mic_ready');
    expect(ready).toMatchObject({ device: 'MacBook Pro Microphone', source: 'native mic' });

    // 0.4 s of mic audio fills one streaming chunk (0.32 s) and reaches the
    // server at full level.
    for (let i = 0; i < 4; i++) window.__whisperNativeMic!.onAudio(chunk(1600, 0x2000), 1600, 0.25);
    expect(sock.audio()).toHaveLength(1);
    expect(new Int16Array(sock.audio()[0])[0]).toBeGreaterThan(0x1ffe);
    expect(sock.marks().find((m) => m.mark === 'first_mic_frame')?.dbfs).toBe(-12);

    recordingController.stop();
    await sleep(300);
    expect(posted).toEqual(['mic:start', 'mic:stop']);
  });

  it('opens the mic before system audio, so the mixer never drops a start-up backlog', async () => {
    useRecordingStore.setState({ nativeSource: { pid: -1, name: 'System audio' }, micEnabled: true });
    await recordingController.start('mixed-session');
    await sleep(10);
    expect(posted.slice(0, 2)).toEqual(['mic:start', 'audio:start']);
    expect(getUserMedia).not.toHaveBeenCalled();
    expect(useRecordingStore.getState().activeSourceLabel).toBe('Mic + System audio');
  });

  it('a mic that fails mid-recording leaves system audio carrying the take', async () => {
    useRecordingStore.setState({ nativeSource: { pid: -1, name: 'System audio' }, micEnabled: true });
    await recordingController.start('mixed-session');
    await sleep(10);
    window.__whisperNativeMic!.onError('The input device disappeared.');
    expect(useRecordingStore.getState().isRecording).toBe(true);
    expect(useRecordingStore.getState().activeSourceLabel).toBe('System audio');
    // System audio now drives the chunker on its own clock.
    for (let i = 0; i < 4; i++) window.__whisperNativeAudio!.onAudio(chunk(1600, 0x1000), 1600, 0.1);
    expect(sockets[0].audio()).toHaveLength(1);
  });

  it('a mic that fails with no other source stops the recording', async () => {
    await recordingController.start('mic-session');
    await sleep(10);
    window.__whisperNativeMic!.onError('The input device disappeared.');
    await sleep(300);
    expect(useRecordingStore.getState().isRecording).toBe(false);
  });

  it('a blocked microphone fails the start with the shell’s reason', async () => {
    (window as unknown as { webkit: { messageHandlers: Record<string, unknown> } }).webkit.messageHandlers.nativeMic = {
      postMessage: () =>
        queueMicrotask(() =>
          window.__whisperNativeMic!.onError('Microphone access is blocked. Allow Whisper Studio.'),
        ),
    };
    await recordingController.start('mic-session');
    await sleep(10);
    expect(useRecordingStore.getState().isRecording).toBe(false);
    const toasts = vi.mocked(useUIStore.getState().addToast).mock.calls.map(([t]) => t.message);
    expect(toasts.some((m) => m.includes('Microphone access is blocked'))).toBe(true);
  });
});
