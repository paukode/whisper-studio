/**
 * Stop while a recording is still starting. start() awaits each source in
 * turn (the shell's mic, the app's pid, the shell's capture, the browser
 * mic, the worklet module), and the header shows Stop from the first one.
 * Whichever await the Stop lands in, the recording must end with no live
 * source: every source that opens after the Stop is closed again, and the
 * start never records into the stopped take.
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

function deferred<T>() {
  let resolve!: (v: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

class ServerSocket extends WebSocketMock {
  send(data: string | ArrayBuffer): void {
    super.send(data);
    answerStopLikeServer(this, data);
  }
}

class FakeWorkletNode {
  port: { onmessage: unknown } = { onmessage: null };
  connected = false;
  constructor() {
    workletNodes.push(this);
  }
  connect(): void {
    this.connected = true;
  }
  disconnect(): void {
    this.connected = false;
  }
}

let workletNodes: FakeWorkletNode[] = [];
/** Every message the page posted to either shell bridge, in order. */
let posted: string[] = [];
let tracks: { stop: ReturnType<typeof vi.fn> }[] = [];
let gum: ReturnType<typeof deferred<MediaStream>>;
let addModule: ReturnType<typeof deferred<void>>;
let RealWS: typeof WebSocket;

function makeStream(): MediaStream {
  const track = { stop: vi.fn(), label: 'Built-in Microphone' };
  tracks.push(track);
  return { getTracks: () => [track], getAudioTracks: () => [track] } as unknown as MediaStream;
}

/** Shell bridges that confirm stops at once but leave each start (and the
 *  source list) pending until the test answers, so a Stop can land inside. */
function installShell(opts: { mic: boolean; audio: boolean }): void {
  if (opts.mic) window.__WHISPER_NATIVE_MIC = { available: true };
  if (opts.audio) window.__WHISPER_NATIVE_AUDIO = { available: true };
  (window as unknown as { webkit: unknown }).webkit = {
    messageHandlers: {
      nativeAudio: {
        postMessage: (msg: { cmd: string }) => {
          posted.push(`audio:${msg.cmd}`);
          if (msg.cmd === 'stop') queueMicrotask(() => window.__whisperNativeAudio!.onStopped());
        },
      },
      nativeMic: {
        postMessage: (msg: { cmd: string }) => {
          posted.push(`mic:${msg.cmd}`);
          if (msg.cmd === 'stop') queueMicrotask(() => window.__whisperNativeMic!.onStopped());
        },
      },
    },
  };
}

/** Start recording and let it run until it waits on `pending`. The start
 *  comes back wrapped: an async function would flatten a bare promise and
 *  wait for the start itself. */
async function startUntil(pending: () => boolean): Promise<{ run: Promise<void> }> {
  const run = recordingController.start('cancel-session');
  for (let i = 0; i < 50 && !pending(); i++) await sleep(1);
  expect(pending()).toBe(true);
  expect(useRecordingStore.getState().isRecording).toBe(true);
  return { run };
}

/** Nothing the recording opened is still live, and the UI shows no recording. */
function expectNoLiveSource(): void {
  // Each shell bridge's last word is a stop whenever it was ever started.
  for (const bridge of ['mic', 'audio']) {
    const cmds = posted.filter((p) => p === `${bridge}:start` || p === `${bridge}:stop`);
    expect(cmds[cmds.length - 1] ?? `${bridge}:stop`).toBe(`${bridge}:stop`);
  }
  for (const track of tracks) expect(track.stop).toHaveBeenCalled();
  expect(workletNodes.filter((n) => n.connected)).toEqual([]);
  const rec = useRecordingStore.getState();
  expect(rec.micStream).toBeNull();
  expect(rec.isRecording).toBe(false);
  expect(rec.recordingSessionId).toBeNull();
  expect(rec.activeSourceLabel).toBeNull();
}

beforeEach(() => {
  workletNodes = [];
  posted = [];
  tracks = [];
  gum = deferred<MediaStream>();
  addModule = deferred<void>();
  __resetNativeAudioForTests();
  __resetNativeMicForTests();
  Object.defineProperty(navigator, 'mediaDevices', {
    configurable: true,
    value: { getUserMedia: vi.fn(() => gum.promise) },
  });
  vi.stubGlobal(
    'AudioContext',
    class {
      state = 'running';
      destination = {};
      audioWorklet = { addModule: () => addModule.promise };
      createMediaStreamSource() {
        return { connect: vi.fn(), disconnect: vi.fn() };
      }
      async close() {
        this.state = 'closed';
      }
    },
  );
  vi.stubGlobal('AudioWorkletNode', FakeWorkletNode);
  RealWS = globalThis.WebSocket;
  globalThis.WebSocket = ServerSocket as unknown as typeof WebSocket;
  useSessionStore.setState({ saveSession: vi.fn(), debouncedSave: vi.fn() });
  useUIStore.setState({ addToast: vi.fn(() => 'toast') });
  useRecordingStore.setState({ nativeSource: null, micEnabled: true });
});

afterEach(async () => {
  if (useRecordingStore.getState().isRecording) recordingController.stop();
  await sleep(20);
  useRecordingStore.getState().cleanup();
  globalThis.WebSocket = RealWS;
  __resetNativeAudioForTests();
  __resetNativeMicForTests();
  delete window.__WHISPER_NATIVE_AUDIO;
  delete window.__WHISPER_NATIVE_MIC;
  delete (window as { webkit?: unknown }).webkit;
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('Stop while the recording is starting', () => {
  it('while the shell mic opens: the mic that opens after Stop is closed again', async () => {
    installShell({ mic: true, audio: false });
    const { run } = await startUntil(() => posted.includes('mic:start'));

    recordingController.stop();
    window.__whisperNativeMic!.onStarted('MacBook Pro Microphone');
    await run;
    await sleep(20);

    expect(posted).toEqual(['mic:start', 'mic:stop']);
    expectNoLiveSource();
  });

  it('while the app pid resolves: no capture and no mic open at all', async () => {
    installShell({ mic: false, audio: true });
    useRecordingStore.setState({ nativeSource: { pid: 501, name: 'Zoom', bundleID: 'us.zoom.xos' } });
    const { run } = await startUntil(() => posted.includes('audio:list'));

    recordingController.stop();
    window.__whisperNativeAudio!.onSources([{ pid: 777, name: 'Zoom', bundleID: 'us.zoom.xos' }]);
    await run;
    await sleep(20);

    expect(posted).toEqual(['audio:list']);
    expect(navigator.mediaDevices.getUserMedia).not.toHaveBeenCalled();
    expectNoLiveSource();
  });

  it('while the system audio tap starts: the tap that starts after Stop is stopped', async () => {
    installShell({ mic: false, audio: true });
    useRecordingStore.setState({ nativeSource: { pid: -1, name: 'System audio' } });
    const { run } = await startUntil(() => posted.includes('audio:start'));

    recordingController.stop();
    window.__whisperNativeAudio!.onStarted();
    await run;
    await sleep(20);

    expect(posted).toEqual(['audio:start', 'audio:stop']);
    expect(navigator.mediaDevices.getUserMedia).not.toHaveBeenCalled();
    expectNoLiveSource();
  });

  it('while the browser mic opens: the stream that lands after Stop is stopped', async () => {
    const { run } = await startUntil(() => vi.mocked(navigator.mediaDevices.getUserMedia).mock.calls.length > 0);

    recordingController.stop();
    gum.resolve(makeStream());
    await run;
    await sleep(20);

    expect(workletNodes).toEqual([]);
    expectNoLiveSource();
  });

  it('while the worklet module loads: no worklet starts and the mic is closed', async () => {
    gum.resolve(makeStream());
    const { run } = await startUntil(() => tracks.length > 0 && useRecordingStore.getState().micStream !== null);

    recordingController.stop();
    addModule.resolve();
    await run;
    await sleep(20);

    expect(workletNodes).toEqual([]);
    expectNoLiveSource();
  });

  it('a start that resumes after Stop leaves the next recording its mic', async () => {
    const { run: first } = await startUntil(() => vi.mocked(navigator.mediaDevices.getUserMedia).mock.calls.length > 0);
    recordingController.stop();
    await sleep(20);

    // Record again while the first start still waits on the same device open.
    addModule.resolve();
    const second = recordingController.start('cancel-session');
    await sleep(5);
    gum.resolve(makeStream());
    await Promise.all([first, second]);
    await sleep(5);

    const rec = useRecordingStore.getState();
    expect(rec.isRecording).toBe(true);
    expect(rec.micStream).not.toBeNull();
    expect(tracks[0].stop).not.toHaveBeenCalled();
    expect(workletNodes.filter((n) => n.connected)).toHaveLength(1);
  });
});
