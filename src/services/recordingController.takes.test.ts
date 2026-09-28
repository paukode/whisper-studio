/**
 * Recording takes: every frame of a take lands in the store of the session
 * that started it, under chunk ids no other take in that store uses.
 *
 * The server numbers chunks from 0 again after every explicit stop, and a
 * late result (an Apple translation waiting on the OS) can settle after the
 * next take started, possibly in another session. These tests drive the real
 * controller through a native-only recording against a fake socket that
 * answers `stop` the way the server does (session_ended, then close).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/services/recordingModels', () => ({
  ensureRecordingModels: vi.fn(async () => 'ready'),
}));
const translation = vi.hoisted(() => ({
  available: false,
  translateNative: vi.fn(async (_text: string, _source: string, _target?: string) => ''),
}));
vi.mock('@/services/nativeTranslation', () => ({
  isNativeTranslationAvailable: () => translation.available,
  translateNative: translation.translateNative,
}));

import { recordingController, STOP_SETTLE_DEADLINE_MS } from './recordingController';
import { __resetNativeAudioForTests } from './nativeAudioSource';
import { useRecordingStore } from '@/stores/recordingStore';
import { useSessionStore } from '@/stores/sessionStore';
import { getTranscriptionStore } from '@/stores/sessionRuntimes';
import { useUIStore } from '@/stores/uiStore';
import { WebSocketMock } from '@/test/mocks/webSocket';
import { answerStopLikeServer } from '@/test/mocks/asrServer';

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));
const native = () => window.__whisperNativeAudio!;

/** The recording socket as the server drives it. With `serverAnswersStop`
 *  off, a test plays the server's stop sequence itself. */
class ServerSocket extends WebSocketMock {
  constructor(url: string) {
    super(url);
    sockets.push(this);
  }

  send(data: string | ArrayBuffer): void {
    super.send(data);
    if (serverAnswersStop) answerStopLikeServer(this, data);
  }

  sent(): string[] {
    return this._getSentMessages().filter((m): m is string => typeof m === 'string');
  }
}

let sockets: ServerSocket[] = [];
let serverAnswersStop = true;
let RealWS: typeof WebSocket;
const lastSocket = () => sockets[sockets.length - 1];

/** The server's side of stop: session_ended, then it closes the socket. */
function endSession(sock: ServerSocket): void {
  sock._receiveMessage({ type: 'session_ended' });
  sock.close();
}

async function startTake(sessionId: string): Promise<ServerSocket> {
  await recordingController.start(sessionId);
  await sleep(5); // the mock socket opens on the next tick
  return lastSocket();
}

async function stopTake(): Promise<void> {
  recordingController.stop();
  await sleep(20);
  expect(useRecordingStore.getState().recordingSessionId).toBeNull();
}

const final = (chunk: number, text: string, speaker: string, extra: object = {}) => ({
  type: 'transcript', text, speaker, chunk_id: chunk, ...extra,
});

const segmentsOf = (sid: string) => getTranscriptionStore(sid).getState().segments;
const toastMessages = () =>
  vi.mocked(useUIStore.getState().addToast).mock.calls.map(([t]) => t.message);

beforeEach(() => {
  sockets = [];
  serverAnswersStop = true;
  translation.available = false;
  translation.translateNative.mockReset();
  translation.translateNative.mockResolvedValue('');
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
  vi.useRealTimers();
  if (useRecordingStore.getState().recordingSessionId) {
    recordingController.stop();
    const sock = lastSocket();
    if (sock?.readyState === WebSocketMock.OPEN) endSession(sock);
    await sleep(20);
  }
  globalThis.WebSocket = RealWS;
  __resetNativeAudioForTests();
  delete window.__WHISPER_NATIVE_AUDIO;
  delete (window as { webkit?: unknown }).webkit;
  vi.clearAllMocks();
});

describe('recordingController: takes', () => {
  it("a later take's translations and speaker corrections never touch an earlier take", async () => {
    const sid = 'two-takes-session';
    let sock = await startTake(sid);
    sock._receiveMessage(final(0, 'Dzien dobry.', 'Speaker 1', { translating: true }));
    sock._receiveMessage({ type: 'translation', chunk_id: 0, text: 'Good morning.', target: 'en' });
    sock._receiveMessage(final(1, 'Czesc.', 'Speaker 2', { translating: true }));
    sock._receiveMessage({ type: 'translation', chunk_id: 1, text: 'Hi.', target: 'en' });
    sock._receiveMessage(final(2, 'Jak sie masz?', 'Speaker 1'));
    await stopTake();
    const firstTake = segmentsOf(sid);

    // The server restarted its counter: take 2 is numbered from 0 again,
    // and its first voice is "Speaker 1" again.
    sock = await startTake(sid);
    sock._receiveMessage(final(0, 'Nie wiem.', 'Speaker 1', { translating: true }));
    sock._receiveMessage(final(1, 'Moze jutro.', 'Speaker 1', { translating: true }));
    sock._receiveMessage(final(2, 'Dobrze.', 'Speaker 2', { translating: true }));
    sock._receiveMessage({ type: 'translation', chunk_id: 0, text: "I don't know.", target: 'en' });
    sock._receiveMessage({ type: 'translation', chunk_id: 1, text: 'Maybe tomorrow.', target: 'en' });
    sock._receiveMessage({ type: 'translation', chunk_id: 2, text: 'Fine.', target: 'en' });
    sock._receiveMessage({ type: 'speaker_update', updates: [{ chunk_id: 1, speaker: 'Speaker 2' }] });
    await stopTake();

    const all = segmentsOf(sid);
    // Take 1 is exactly as it was when it stopped.
    expect(all.slice(0, firstTake.length)).toEqual(firstTake);
    // Take 2 opened its own segments, and each line sits under its own text.
    const secondTake = all.slice(firstTake.length);
    expect(secondTake.map((s) => [s.speaker, s.text, s.translations?.map((t) => t.text)])).toEqual([
      ['Speaker 1', 'Nie wiem.', ["I don't know."]],
      ['Speaker 2', 'Moze jutro.', ['Maybe tomorrow.']],
      ['Speaker 2', 'Dobrze.', ['Fine.']],
    ]);
  });

  it('an Apple translation that settles after the next take started lands on its own session', async () => {
    translation.available = true;
    let resolveLate: (text: string) => void = () => {};
    translation.translateNative.mockImplementationOnce(
      () => new Promise<string>((resolve) => { resolveLate = resolve; }),
    );

    let sock = await startTake('apple-session-a');
    sock._receiveMessage(
      final(0, 'Dzien dobry.', 'Speaker 1', {
        translating: true, translate_via: 'apple', translate_target: 'en', language: 'pl',
      }),
    );
    await stopTake();

    sock = await startTake('apple-session-b');
    sock._receiveMessage(final(0, 'Hello there.', 'Speaker 1', { translating: true }));

    resolveLate('Good morning.');
    await sleep(0);

    const [a] = segmentsOf('apple-session-a');
    expect(a.translations?.map((t) => t.text)).toEqual(['Good morning.']);
    expect(a.pendingTranslations).toBeUndefined();
    const [b] = segmentsOf('apple-session-b');
    expect(b.translations).toBeUndefined();
    expect(b.pendingTranslations?.length).toBe(1);
    // The take was saved at stop, before the line existed: it saves again.
    expect(useSessionStore.getState().debouncedSave).toHaveBeenCalledWith('apple-session-a');
  });

  const rows = (segs: ReturnType<typeof segmentsOf>) =>
    segs.map((s) => [s.speaker, s.text, s.translations?.map((t) => t.text)]);

  it("a restarted server's chunk ids never land on the take's earlier segments", async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
    const sid = 'restarted-server-session';
    let sock = await startTake(sid);
    sock._receiveMessage(final(0, 'Raz.', 'Speaker 1', { translating: true }));
    sock._receiveMessage({ type: 'translation', chunk_id: 0, text: 'One.', target: 'en' });
    sock._receiveMessage(final(1, 'Dwa.', 'Speaker 2'));
    sock._receiveMessage(final(2, 'Trzy.', 'Speaker 1'));
    const before = segmentsOf(sid);

    // The backend restarts: the watchdog reconnects to a server that numbers
    // from 0 again and labels its first voice "Speaker 1" again.
    sock.close();
    vi.advanceTimersByTime(3000);
    await sleep(5);
    expect(sockets).toHaveLength(2);
    sock = lastSocket();
    sock._receiveMessage(final(0, 'Cztery.', 'Speaker 1', { translating: true }));
    sock._receiveMessage({ type: 'translation', chunk_id: 0, text: 'Four.', target: 'en' });
    sock._receiveMessage({ type: 'speaker_update', updates: [{ chunk_id: 0, speaker: 'Speaker 2' }] });

    const all = segmentsOf(sid);
    expect(all.slice(0, before.length)).toEqual(before);
    expect(rows(all.slice(before.length))).toEqual([['Speaker 2', 'Cztery.', ['Four.']]]);
  });

  it('a reconnect to a server that kept counting still corrects the earlier chunks', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
    const sid = 'resumed-server-session';
    let sock = await startTake(sid);
    sock._receiveMessage(final(0, 'Raz.', 'Speaker 1'));
    sock._receiveMessage(final(1, 'Dwa.', 'Speaker 2'));
    sock.close();
    vi.advanceTimersByTime(3000);
    await sleep(5);
    sock = lastSocket();
    sock._receiveMessage(final(2, 'Trzy.', 'Speaker 2', { translating: true }));
    sock._receiveMessage({ type: 'translation', chunk_id: 2, text: 'Three.', target: 'en' });
    sock._receiveMessage({ type: 'speaker_update', updates: [{ chunk_id: 0, speaker: 'Speaker 3' }] });

    expect(rows(segmentsOf(sid))).toEqual([
      ['Speaker 3', 'Raz.', undefined],
      ['Speaker 2', 'Dwa. Trzy.', ['Three.']],
    ]);
  });

  it("a take whose stop never reached the server keeps its chunks correctable", async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
    const sid = 'unstopped-server-session';
    let sock = await startTake(sid);
    sock._receiveMessage(final(0, 'Raz.', 'Speaker 1'));
    sock._receiveMessage(final(1, 'Dwa.', 'Speaker 2'));
    // The socket is down when the user stops: `stop` cannot be sent, so the
    // server keeps this session's speakers and its chunk counter.
    sock.close();
    await stopTake();

    sock = await startTake(sid);
    sock._receiveMessage(final(2, 'Trzy.', 'Speaker 1', { translating: true }));
    sock._receiveMessage({ type: 'translation', chunk_id: 2, text: 'Three.', target: 'en' });
    sock._receiveMessage({ type: 'speaker_update', updates: [{ chunk_id: 1, speaker: 'Speaker 1' }] });

    expect(rows(segmentsOf(sid))).toEqual([
      ['Speaker 1', 'Raz.', undefined],
      ['Speaker 1', 'Dwa.', undefined],
      ['Speaker 1', 'Trzy.', ['Three.']],
    ]);
  });

  it("a new take's lines land even under the ids an edit superseded", async () => {
    const sid = 'edited-history-session';
    const transcript = getTranscriptionStore(sid);
    // A transcript saved in an earlier run: chunk ids are gone, lines stay.
    transcript.getState().loadSegments(
      [{
        id: 'old', speaker: 'Speaker 1', text: 'Czesc wszystkim.', timestamp: 1, edited: false,
        translations: [{ chunkId: 3, text: 'Hi all.', target: 'en' }],
      }],
      {},
    );
    // The user corrects it; the fresh line replaces the chunk-3 one.
    transcript.getState().editSegmentText('old', 'Cześć wszystkim.');
    transcript.getState().beginSegmentRetranslation('old');
    transcript.getState().completeSegmentRetranslation('old', 'Hi everyone.', 'en');

    const sock = await startTake(sid);
    const texts = ['Raz.', 'Dwa.', 'Trzy.', 'Cztery.'];
    const lines = ['One.', 'Two.', 'Three.', 'Four.'];
    texts.forEach((text, i) => {
      sock._receiveMessage(final(i, text, i % 2 ? 'Speaker 2' : 'Speaker 1', { translating: true }));
    });
    lines.forEach((text, i) => {
      sock._receiveMessage({ type: 'translation', chunk_id: i, text, target: 'en' });
    });

    const [old, ...take] = segmentsOf(sid);
    expect(old.translations?.map((t) => t.text)).toEqual(['Hi everyone.']);
    expect(take.map((s) => [s.text, s.translations?.map((t) => t.text)])).toEqual(
      texts.map((text, i) => [text, [lines[i]]]),
    );
  });

  it('a transcript cleared mid-session cannot make a new take reuse its chunk ids', async () => {
    translation.available = true;
    let resolveLate: (text: string) => void = () => {};
    translation.translateNative.mockImplementationOnce(
      () => new Promise<string>((resolve) => { resolveLate = resolve; }),
    );
    const sid = 'cleared-session';
    let sock = await startTake(sid);
    sock._receiveMessage(
      final(0, 'Stare zdanie.', 'Speaker 1', { translating: true, translate_via: 'apple' }),
    );
    await stopTake();
    getTranscriptionStore(sid).getState().clearSegments();

    sock = await startTake(sid);
    sock._receiveMessage(final(0, 'Nowe zdanie.', 'Speaker 1', { translating: true }));
    resolveLate('Old sentence.');
    await sleep(0);

    const [seg] = segmentsOf(sid);
    expect(seg.text).toBe('Nowe zdanie.');
    expect(seg.translations).toBeUndefined();
  });
});

describe('recordingController: stop settles on the server, not on quiet', () => {
  beforeEach(() => {
    serverAnswersStop = false;
  });

  /** What the owning session held each time it was saved: per segment its
   *  text, its translation lines and a PENDING per spinning marker. */
  function recordSaves(sid: string): string[][] {
    const saved: string[][] = [];
    const summary = (s: ReturnType<typeof segmentsOf>[number]) =>
      [
        s.text,
        ...(s.translations ?? []).map((t) => t.text),
        ...(s.pendingTranslations ?? []).map(() => 'PENDING'),
      ].join(' | ');
    useSessionStore.setState({
      saveSession: vi.fn(() => {
        saved.push(segmentsOf(sid).map(summary));
      }),
    });
    return saved;
  }

  it('a final that lands well after stop replaces the live draft', async () => {
    const sid = 'slow-final-session';
    const saved = recordSaves(sid);
    const sock = await startTake(sid);
    sock._receiveMessage({ type: 'interim', text: 'How are you' });

    recordingController.stop();
    expect(sock.sent().some((m) => m.includes('"stop"'))).toBe(true);
    // The server is still decoding the tail: seconds of quiet change nothing.
    await sleep(400);
    expect(useRecordingStore.getState().recordingSessionId).toBe(sid);
    expect(saved).toEqual([]);

    sock._receiveMessage(final(0, 'Jak się masz?', 'Speaker 1'));
    endSession(sock);

    expect(segmentsOf(sid).map((s) => s.text)).toEqual(['Jak się masz?']);
    expect(getTranscriptionStore(sid).getState().interimText).toBe('');
    expect(saved).toEqual([['Jak się masz?']]);
    expect(useRecordingStore.getState().recordingSessionId).toBeNull();
    expect(useUIStore.getState().addToast).not.toHaveBeenCalled();
  });

  it('the last translation, drained after the final, is saved instead of left Translating', async () => {
    const sid = 'drained-translation-session';
    const saved = recordSaves(sid);
    const sock = await startTake(sid);
    sock._receiveMessage(final(0, 'Dzień dobry.', 'Speaker 1', { translating: true }));

    recordingController.stop();
    await sleep(400);
    sock._receiveMessage({ type: 'translation', chunk_id: 0, text: 'Good morning.', target: 'en' });
    endSession(sock);

    expect(saved).toEqual([['Dzień dobry. | Good morning.']]);
    expect(useUIStore.getState().addToast).not.toHaveBeenCalled();
  });

  it('a draft the server withdrew before answering is not saved as a segment', async () => {
    const sid = 'withdrawn-draft-session';
    const sock = await startTake(sid);
    sock._receiveMessage(final(0, 'Dziękuję.', 'Speaker 1'));
    sock._receiveMessage({ type: 'interim', text: 'Thank you for watching' });

    recordingController.stop();
    // The tail produced no final (junk-filtered): the server withdraws it.
    sock._receiveMessage({ type: 'interim', text: '' });
    endSession(sock);

    expect(segmentsOf(sid).map((s) => s.text)).toEqual(['Dziękuję.']);
    expect(getTranscriptionStore(sid).getState().interimText).toBe('');
    expect(useUIStore.getState().addToast).not.toHaveBeenCalled();
  });

  it('a draft still showing when the server answers is kept and flagged, not lost', async () => {
    const sid = 'failed-finish-session';
    const saved = recordSaves(sid);
    const sock = await startTake(sid);
    sock._receiveMessage(final(0, 'Dziękuję.', 'Speaker 1'));
    sock._receiveMessage({ type: 'interim', text: 'Do zobaczenia' });

    recordingController.stop();
    // The final decode failed server-side, which still ends the session.
    endSession(sock);

    expect(saved).toEqual([['Dziękuję.', 'Do zobaczenia']]);
    expect(getTranscriptionStore(sid).getState().interimText).toBe('');
    expect(useUIStore.getState().addToast).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'warning', source: 'recording' }),
    );
  });

  it('a server that never answers: the deadline keeps the draft, says so, and stops the spinners', async () => {
    const sid = 'silent-server-session';
    const saved = recordSaves(sid);
    const sock = await startTake(sid);
    sock._receiveMessage(final(0, 'Dzień dobry.', 'Speaker 1', { translating: true }));
    sock._receiveMessage({ type: 'interim', text: 'Jak się' });

    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
    recordingController.stop();
    vi.advanceTimersByTime(STOP_SETTLE_DEADLINE_MS - 1);
    expect(useRecordingStore.getState().recordingSessionId).toBe(sid);
    vi.advanceTimersByTime(1);

    expect(saved).toEqual([['Dzień dobry.', 'Jak się']]);
    expect(sock.readyState).toBe(WebSocketMock.CLOSED);
    expect(useRecordingStore.getState().recordingSessionId).toBeNull();
    // Both losses are said out loud: the unfinished sentence and the line.
    expect(toastMessages()).toHaveLength(2);
    expect(toastMessages()).toEqual(
      expect.arrayContaining([
        expect.stringContaining('A translation did not arrive'),
        expect.stringContaining('live draft was saved'),
      ]),
    );
  });

  it('a socket that drops mid-take stops the spinners of the translations it owed', async () => {
    translation.available = true;
    translation.translateNative.mockImplementationOnce(() => new Promise<string>(() => {}));
    const sid = 'dropped-socket-session';
    const sock = await startTake(sid);
    sock._receiveMessage(final(0, 'Dzień dobry.', 'Speaker 1', { translating: true }));
    sock._receiveMessage(
      final(1, 'Cześć.', 'Speaker 2', { translating: true, translate_via: 'apple' }),
    );
    sock.close(); // network blip: the server cancels its in-flight decodes

    const [server, apple] = segmentsOf(sid);
    expect(server.pendingTranslations).toBeUndefined();
    expect(toastMessages()).toEqual([expect.stringContaining('A translation did not arrive')]);
    // The Apple line is translated here, not owed by the socket.
    expect(apple.pendingTranslations?.length).toBe(1);
  });

  it('a record click while the previous take settles starts right after it', async () => {
    const sock = await startTake('settling-session-a');
    recordingController.stop();

    const next = recordingController.start('settling-session-b');
    await sleep(20);
    expect(useRecordingStore.getState().recordingSessionId).toBe('settling-session-a');

    endSession(sock);
    await next;
    expect(useRecordingStore.getState().recordingSessionId).toBe('settling-session-b');
  });
});
