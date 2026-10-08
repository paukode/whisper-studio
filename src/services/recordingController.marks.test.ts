/**
 * Start-up marks: the recorder tells the server's log (recording_timeline.py)
 * when each start-up step happened, counted from the press of Record. Marks
 * made before the socket opens wait for it, every mark reaches the server
 * before the audio it describes, and the "first" marks fire once per take.
 * Runs in native-only mode against a fake shell bridge (no mic, no worklet).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/services/recordingModels', () => ({
  ensureRecordingModels: vi.fn(async () => 'ready'),
}));

import { recordingController } from './recordingController';
import { __resetNativeAudioForTests } from './nativeAudioSource';
import { useRecordingStore } from '@/stores/recordingStore';
import { useSessionStore } from '@/stores/sessionStore';
import { useUIStore } from '@/stores/uiStore';
import { WebSocketMock } from '@/test/mocks/webSocket';
import { answerStopLikeServer } from '@/test/mocks/asrServer';

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));
const native = () => window.__whisperNativeAudio!;

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

  /** Everything sent, in order: marks by name, audio as 'audio'. */
  wire(): string[] {
    return this._getSentMessages().map((m) => {
      if (typeof m !== 'string') return 'audio';
      const msg = JSON.parse(m) as { type: string; mark?: string };
      return msg.type === 'client_mark' ? `mark:${msg.mark}` : msg.type;
    });
  }

  marks(): Record<string, unknown>[] {
    return this._getSentMessages()
      .filter((m): m is string => typeof m === 'string')
      .map((m) => JSON.parse(m) as Record<string, unknown>)
      .filter((m) => m.type === 'client_mark');
  }
}
let RealWS: typeof WebSocket;

/** One shell chunk of `samples` Int16 samples at `value`, base64-encoded. */
function chunk(samples: number, value: number): string {
  const pcm = new Int16Array(samples).fill(value);
  let binary = '';
  for (const byte of new Uint8Array(pcm.buffer)) binary += String.fromCharCode(byte);
  return btoa(binary);
}

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
  globalThis.WebSocket = ServerSocket as unknown as typeof WebSocket;
  useSessionStore.setState({ saveSession: vi.fn(), debouncedSave: vi.fn() });
  useUIStore.setState({ addToast: vi.fn(() => 'toast') });
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

describe('recording start-up marks', () => {
  it('reach the server in order, before the audio they describe, once per take', async () => {
    await recordingController.start('marks-session');
    await sleep(10); // the mock socket opens on the next tick
    const sock = sockets[0];

    // Marks made while the socket was still connecting were held for it.
    expect(sock.wire()).toEqual(
      expect.arrayContaining(['mark:models_ready', 'mark:native_started', 'mark:socket_open']),
    );
    expect(sock.wire().indexOf('mark:models_ready')).toBe(0);
    expect(sock.wire()).not.toContain('audio');

    // 4 x 1600 samples of 0.25 fill one 0.4 s send.
    for (let i = 0; i < 4; i++) native().onAudio(chunk(1600, 0x2000), 1600);
    const wire = sock.wire();
    expect(wire.indexOf('mark:first_native_audio')).toBeLessThan(wire.indexOf('audio'));
    expect(wire.indexOf('mark:first_audio_sent')).toBeLessThan(wire.indexOf('audio'));

    const byName = Object.fromEntries(sock.marks().map((m) => [m.mark, m]));
    expect(byName.native_started.source).toBe('System audio');
    expect(byName.first_native_audio.dbfs).toBe(-12); // 0.25 full scale
    expect(byName.first_audio_sent.seconds).toBe(0.4);
    for (const m of sock.marks()) {
      expect(typeof m.ms).toBe('number');
      expect(m.ms as number).toBeGreaterThanOrEqual(0);
    }

    // Later audio adds no more "first" marks.
    const marksBefore = sock.marks().length;
    for (let i = 0; i < 4; i++) native().onAudio(chunk(1600, 0), 1600);
    expect(sock.marks()).toHaveLength(marksBefore);
  });

  it('a second press while recording leaves the running take alone', async () => {
    await recordingController.start('marks-session');
    await sleep(10);
    const sent = sockets[0].marks().length;
    await recordingController.start('marks-session'); // ignored: one mic, one recording
    await sleep(10);
    expect(sockets).toHaveLength(1);
    expect(sockets[0].marks()).toHaveLength(sent);
  });
});
