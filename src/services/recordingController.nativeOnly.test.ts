/**
 * Native-only recording mode: a shell audio source (system audio / one app)
 * drives the chunker and websocket directly — no getUserMedia, no
 * AudioContext, no worklet. These tests pin that mode end to end against a
 * fake bridge: model gate first, {cmd:"start"} to the shell, decoded chunks
 * accumulating to the backend chunk size, one mono PCM16 buffer on the
 * websocket, and a clean {cmd:"stop"} teardown.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/services/recordingModels', () => ({
  ensureRecordingModels: vi.fn(async () => 'ready'),
}));

import { recordingController } from './recordingController';
import { ensureRecordingModels } from '@/services/recordingModels';
import { __resetNativeAudioForTests } from './nativeAudioSource';
import { useRecordingStore } from '@/stores/recordingStore';
import { useSessionStore } from '@/stores/sessionStore';

/** Base64-encode Int16 samples little-endian, like the Swift shell does. */
function encodeInt16(samples: number[]): string {
  const bytes: number[] = [];
  for (const s of samples) {
    const v = s < 0 ? s + 0x10000 : s;
    bytes.push(v & 0xff, (v >> 8) & 0xff);
  }
  return btoa(String.fromCharCode(...bytes));
}

const native = () => window.__whisperNativeAudio!;
const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

// The default transcription backend is 'streaming' → 320 ms chunks
// (5120 samples), which the 4 × 1600-sample feeds below cross once.

interface TrackedWS {
  _getSentMessages(): (string | ArrayBuffer)[];
  readyState: number;
}

let wsInstances: TrackedWS[] = [];
let posted: unknown[] = [];
let getUserMedia: ReturnType<typeof vi.fn>;
let RealWS: typeof WebSocket;

beforeEach(() => {
  posted = [];
  wsInstances = [];
  __resetNativeAudioForTests();

  // Fake shell bridge: marker + message handler that auto-confirms start.
  window.__WHISPER_NATIVE_AUDIO = { platform: 'macos', available: true };
  window.webkit = {
    messageHandlers: {
      nativeAudio: {
        postMessage: (msg: unknown) => {
          posted.push(msg);
          const cmd = (msg as { cmd?: string }).cmd;
          if (cmd === 'start') queueMicrotask(() => native().onStarted());
          if (cmd === 'stop') queueMicrotask(() => native().onStopped());
        },
      },
    },
  };

  // Track websocket instances so the sent PCM can be inspected.
  RealWS = globalThis.WebSocket;
  class TrackingWS extends (RealWS as unknown as { new (url: string): object }) {
    constructor(url: string) {
      super(url);
      wsInstances.push(this as unknown as TrackedWS);
    }
  }
  globalThis.WebSocket = TrackingWS as unknown as typeof WebSocket;

  // getUserMedia must never be touched in native-only mode.
  getUserMedia = vi.fn(async () => {
    throw new Error('getUserMedia must not be called in native-only mode');
  });
  Object.defineProperty(navigator, 'mediaDevices', {
    value: { getUserMedia },
    configurable: true,
  });

  // No real backend in jsdom — stub out the save triggered by stop().
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
  vi.clearAllMocks();
});

describe('recordingController — native-only mode', () => {
  it('records from the native source without ever opening the mic', async () => {
    await recordingController.start('native-only-session');

    // Model gate ran first, then the shell was asked to start pid -1.
    expect(ensureRecordingModels).toHaveBeenCalledTimes(1);
    expect(posted).toContainEqual({ cmd: 'start', pid: -1 });
    expect(getUserMedia).not.toHaveBeenCalled();

    const rec = useRecordingStore.getState();
    expect(rec.isRecording).toBe(true);
    expect(rec.recordingSessionId).toBe('native-only-session');
    expect(rec.activeSourceLabel).toBe('System audio');

    // Let the mocked websocket auto-open.
    await sleep(10);
    expect(wsInstances).toHaveLength(1);

    // Feed shell chunks: 4 × 1600 samples of 0.25 = 6400 ≥ 5120 → one flush.
    const chunk = encodeInt16(new Array<number>(1600).fill(0x2000)); // 0.25
    for (let i = 0; i < 4; i++) native().onAudio(chunk, 1600);

    const binary = wsInstances[0]
      ._getSentMessages()
      .filter((m): m is ArrayBuffer => typeof m !== 'string');
    expect(binary).toHaveLength(1);
    expect(binary[0].byteLength).toBe(6400 * 2);
    const pcm = new Int16Array(binary[0]);
    // 0x2000 → 0.25 → back to ~0.25 * 0x7fff on the wire.
    expect(pcm[0]).toBeGreaterThan(0x1ffe);
    expect(pcm[0]).toBeLessThanOrEqual(0x2000);

    recordingController.stop();
    await sleep(300); // stop-drain quiet timer

    expect(posted).toContainEqual({ cmd: 'stop' });
    const after = useRecordingStore.getState();
    expect(after.isRecording).toBe(false);
    expect(after.recordingSessionId).toBe(null);
    expect(after.activeSourceLabel).toBe(null);
    expect(getUserMedia).not.toHaveBeenCalled();
  });

  it('fails start cleanly when the shell rejects (e.g. TCC denial)', async () => {
    // Replace the auto-confirming bridge with a denying one.
    window.webkit = {
      messageHandlers: {
        nativeAudio: {
          postMessage: (msg: unknown) => {
            posted.push(msg);
            if ((msg as { cmd?: string }).cmd === 'start') {
              queueMicrotask(() =>
                native().onError('Could not create the audio tap. Enable it in System Settings.'),
              );
            }
          },
        },
      },
    };

    await recordingController.start('native-denied-session');
    await sleep(10);

    const rec = useRecordingStore.getState();
    expect(rec.isRecording).toBe(false);
    expect(rec.recordingSessionId).toBe(null);
    expect(rec.activeSourceLabel).toBe(null);
    expect(getUserMedia).not.toHaveBeenCalled();
  });
});
