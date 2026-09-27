/**
 * The recorder socket names its take. The server hands a dropped socket's
 * in-flight sentence only to a reconnect of the same take, so a watchdog
 * reconnect must repeat the take id and a new recording must not. Runs in
 * native-only mode against a fake shell bridge (no mic, no worklet).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/services/recordingModels', () => ({
  ensureRecordingModels: vi.fn(async () => 'ready'),
}));

import { recordingController } from './recordingController';
import { __resetNativeAudioForTests } from './nativeAudioSource';
import { useRecordingStore } from '@/stores/recordingStore';
import { useSessionStore } from '@/stores/sessionStore';

const native = () => window.__whisperNativeAudio!;
const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

interface TrackedWS {
  url: string;
  close(): void;
}

let sockets: TrackedWS[] = [];
let RealWS: typeof WebSocket;

beforeEach(() => {
  sockets = [];
  __resetNativeAudioForTests();
  window.__WHISPER_NATIVE_AUDIO = { platform: 'macos', available: true };
  window.webkit = {
    messageHandlers: {
      nativeAudio: {
        postMessage: (msg: unknown) => {
          const cmd = (msg as { cmd?: string }).cmd;
          if (cmd === 'start') queueMicrotask(() => native().onStarted());
          if (cmd === 'stop') queueMicrotask(() => native().onStopped());
        },
      },
    },
  };
  RealWS = globalThis.WebSocket;
  class TrackingWS extends (RealWS as unknown as { new (url: string): object }) {
    constructor(url: string) {
      super(url);
      sockets.push(this as unknown as TrackedWS);
    }
  }
  globalThis.WebSocket = TrackingWS as unknown as typeof WebSocket;
  useSessionStore.setState({ saveSession: vi.fn() });
  useRecordingStore.setState({
    isRecording: false,
    isConnected: false,
    recordingSessionId: null,
    nativeSource: { pid: -1, name: 'System audio' },
    micEnabled: false,
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
  delete window.__WHISPER_NATIVE_AUDIO;
  delete (window as { webkit?: unknown }).webkit;
  vi.restoreAllMocks();
});

const takeOf = (socket: TrackedWS) => new URL(socket.url).searchParams.get('take');

describe('recordingController take id', () => {
  it('is repeated by a watchdog reconnect and renewed by a new recording', async () => {
    const intervals = vi.spyOn(globalThis, 'setInterval');
    await recordingController.start('take-session');
    await sleep(10);
    expect(sockets).toHaveLength(1);
    expect(new URL(sockets[0].url).searchParams.get('session_id')).toBe('take-session');
    const take = takeOf(sockets[0]);
    expect(take).toBeTruthy();

    // The socket dies mid-recording; the watchdog opens a replacement.
    sockets[0].close();
    const watchdog = intervals.mock.calls.find(([, ms]) => ms === 3000)?.[0] as () => void;
    watchdog();
    expect(sockets).toHaveLength(2);
    expect(takeOf(sockets[1])).toBe(take);

    recordingController.stop();
    await sleep(300);
    await recordingController.start('take-session');
    await sleep(10);
    expect(sockets).toHaveLength(3);
    expect(takeOf(sockets[2])).toBeTruthy();
    expect(takeOf(sockets[2])).not.toBe(take);
  });
});
