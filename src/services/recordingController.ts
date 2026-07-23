/**
 * Recording controller — the meeting-recorder engine, extracted from
 * Header.tsx and decoupled from the viewed session.
 *
 * The recording binds to its OWNING session at start(): the websocket
 * carries that session id, every transcript/interim/speaker_update
 * lands in that session's transcription store, and stop() saves that
 * session — regardless of which session the user is currently viewing.
 * Switching sessions while recording is therefore free; the header
 * shows a jump chip back to the owner.
 *
 * One recording at a time (one microphone). Module-singleton state, no
 * React: Header renders buttons off recordingStore and calls start/stop.
 */
import { useSessionStore } from '@/stores/sessionStore';
import { useRecordingStore } from '@/stores/recordingStore';
import { useSettingsStore } from '@/stores/settingsStore';
import { useUIStore } from '@/stores/uiStore';
import { countActiveSessions, getTranscriptionStore } from '@/stores/sessionRuntimes';
import { MAX_ACTIVE_SESSIONS } from '@/hooks/useChatStream';

const SAMPLE_RATE = 16000;
// How much audio we buffer client-side before flushing a chunk to the server.
// Backend-dependent and the single biggest lever on perceived latency:
//   * Whisper (utterance/VAD path): 1s — the server only transcribes whole
//     utterances at pauses, so chunk size just controls VAD feed granularity.
//   * Streaming (Parakeet): 320ms — Parakeet emits a growing transcript per
//     chunk, so chunk size sets the word-by-word update cadence (~3×/s) while
//     staying comfortably faster than real time (RTF ~0.7).
const WHISPER_CHUNK_SAMPLES = SAMPLE_RATE; // 1s
const STREAMING_CHUNK_SAMPLES = Math.round(0.32 * SAMPLE_RATE); // 320ms
const chunkSamplesForBackend = (backend: string): number =>
  backend === 'streaming' ? STREAMING_CHUNK_SAMPLES : WHISPER_CHUNK_SAMPLES;
const WS_PING_INTERVAL = 15000;
const WATCHDOG_INTERVAL = 3000;

// ── Engine state (the old Header refs, now module lets) ────────────────
let ws: WebSocket | null = null;
let workletNode: AudioWorkletNode | null = null;
/** Web Audio source for the optional Chrome-tab audio, summed into the
 *  worklet alongside the mic. Held so stop() (and the track's own "ended"
 *  event) can detach it from the graph. */
let tabSourceNode: MediaStreamAudioSourceNode | null = null;
let pcmBuffer: Float32Array[] = [];
let pcmBufferLen = 0;
let chunkSamples = WHISPER_CHUNK_SAMPLES;
let speakerCount = 0;
let watchdogTimer: ReturnType<typeof setInterval> | null = null;
let pingTimer: ReturnType<typeof setInterval> | null = null;
/** The session that owns the live recording. Single source of truth is
 *  recordingStore.recordingSessionId; this mirror avoids store reads in
 *  the per-chunk hot path. */
let owningSessionId: string | null = null;

const ownerStore = () => getTranscriptionStore(owningSessionId).getState();

function flushPcmBuffer(): void {
  if (pcmBufferLen === 0) return;
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  const combined = new Float32Array(pcmBufferLen);
  let offset = 0;
  for (const chunk of pcmBuffer) {
    combined.set(chunk, offset);
    offset += chunk.length;
  }
  pcmBuffer = [];
  pcmBufferLen = 0;
  const pcm16 = new Int16Array(combined.length);
  for (let i = 0; i < combined.length; i++) {
    const s = Math.max(-1, Math.min(1, combined[i]));
    pcm16[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
  }
  ws.send(pcm16.buffer);
}

function onTranscriptResult(text: string, speaker: string, chunkId?: number): void {
  if (!text) return;
  const store = ownerStore();
  const segs = store.segments;
  const lastSeg = segs.length > 0 ? segs[segs.length - 1] : null;
  if (lastSeg && lastSeg.speaker === speaker) {
    // Same speaker still talking: grow the existing segment. The chunk id
    // is recorded so diarization corrections can split this merge later.
    store.appendSegmentText(lastSeg.id, text, chunkId);
  } else {
    const now = Date.now();
    store.addSegment({
      id: crypto.randomUUID(),
      text,
      speaker,
      chunks: chunkId !== undefined ? [{ id: chunkId, start: 0 }] : undefined,
      timestamp: now,
      edited: false,
      receivedAt: now,
      freshIndex: 0,
    });
  }
}

function stopPing(): void {
  if (pingTimer) { clearInterval(pingTimer); pingTimer = null; }
}

function startPing(): void {
  stopPing();
  pingTimer = setInterval(() => {
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'ping' }));
  }, WS_PING_INTERVAL);
}

function connectWS(): void {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  // The OWNING session id rides the URL — the backend keys its speaker
  // profiles by it, and a watchdog reconnect must come back to the same
  // session even if the user is viewing a different one by then.
  const wsUrl = owningSessionId
    ? `${protocol}//${window.location.host}/ws?session_id=${encodeURIComponent(owningSessionId)}`
    : `${protocol}//${window.location.host}/ws`;
  const newWs = new WebSocket(wsUrl);
  ws = newWs;
  newWs.onopen = () => {
    useRecordingStore.getState().setConnected(true);
    startPing();
    // Apply a participant-count hint chosen before recording started.
    if (speakerCount > 0) {
      newWs.send(JSON.stringify({ type: 'set_speakers', count: speakerCount }));
    }
  };
  newWs.onmessage = (event: MessageEvent) => {
    try {
      const msg: Record<string, unknown> = JSON.parse(event.data as string);
      if (msg.type === 'transcript') {
        // A finalized sentence: commit it and clear the live draft.
        onTranscriptResult(
          String(msg.text ?? ''),
          String(msg.speaker ?? 'Speaker 1'),
          typeof msg.chunk_id === 'number' ? msg.chunk_id : undefined,
        );
        ownerStore().setInterimText('');
      } else if (msg.type === 'speaker_update') {
        // Diarization re-clustered and corrected some earlier labels.
        const updates = Array.isArray(msg.updates) ? msg.updates : [];
        ownerStore().applySpeakerUpdates(updates as { chunk_id: number; speaker: string }[]);
      } else if (msg.type === 'interim') {
        // Parakeet's growing word-by-word draft of the utterance in progress.
        ownerStore().setInterimText(String(msg.text ?? ''));
      } else if (msg.type === 'model_loading') {
        // Local mode: the selected engine is loading into memory. Drive the
        // banner; 'ready' auto-hides after a brief beat.
        const stage = String(msg.stage ?? 'loading') as 'start' | 'loading' | 'ready';
        useUIStore.getState().setModelLoading({
          label: String(msg.label ?? 'Model'),
          progress: typeof msg.progress === 'number' ? msg.progress : 0,
          stage,
        });
        if (stage === 'ready') {
          setTimeout(() => {
            const cur = useUIStore.getState().modelLoading;
            if (cur && cur.stage === 'ready') useUIStore.getState().setModelLoading(null);
          }, 700);
        }
      } else if (msg.type === 'model_unloaded') {
        // The outgoing engine was freed on switch — clear any stale banner.
        useUIStore.getState().setModelLoading(null);
      }
    } catch { /* ignore */ }
  };
  newWs.onclose = () => {
    useRecordingStore.getState().setConnected(false);
    if (ws === newWs) ws = null;
    stopPing();
  };
  newWs.onerror = () => { newWs.close(); };
}

function stopWatchdog(): void {
  if (watchdogTimer) { clearInterval(watchdogTimer); watchdogTimer = null; }
}

function startWatchdog(): void {
  stopWatchdog();
  watchdogTimer = setInterval(() => {
    if (!useRecordingStore.getState().isRecording) return;
    if (!ws || ws.readyState === WebSocket.CLOSED || ws.readyState === WebSocket.CLOSING) connectWS();
  }, WATCHDOG_INTERVAL);
}

async function start(sessionId: string): Promise<void> {
  const rec = useRecordingStore.getState();
  if (rec.recordingSessionId) return; // one mic, one recording

  // The 3-active-sessions ceiling counts recording as an activity.
  if (countActiveSessions(sessionId) >= MAX_ACTIVE_SESSIONS) {
    useUIStore.getState().addToast({
      type: 'error',
      message: `${MAX_ACTIVE_SESSIONS} sessions are already active. Stop one before starting a recording.`,
      duration: 5000,
      key: 'parallel-cap',
    });
    return;
  }

  owningSessionId = sessionId;
  rec.setRecordingSession(sessionId);

  pcmBuffer = [];
  pcmBufferLen = 0;
  // Size chunks for whichever backend is active at start; a live engine
  // switch updates this via the whisper-set-backend event.
  chunkSamples = chunkSamplesForBackend(useSettingsStore.getState().config.transcriptionBackend);

  useUIStore.getState().showTranscript();
  rec.setRecording(true);
  connectWS();

  try {
    // Refcounted acquisition — if the chat-input mic is already running,
    // this returns the same MediaStream + AudioContext so both pipelines
    // tap one device instead of fighting for it.
    const { stream: micStream, context } = await rec.acquireMic('header-recorder');

    // Vite BASE_URL so the worklet resolves in dev ('/') and prod
    // ('/static/dist/') — hardcoding '/pcm-processor.js' hits the SPA
    // fallback in prod and gets index.html back.
    await context.audioWorklet.addModule(`${import.meta.env.BASE_URL}pcm-processor.js`);
    // Explicit mono: any stereo source (e.g. tab audio) is downmixed
    // (L+R) into channel 0 rather than having its right channel dropped,
    // since the worklet only forwards channel 0.
    workletNode = new AudioWorkletNode(context, 'pcm-processor', {
      channelCount: 1,
      channelCountMode: 'explicit',
      channelInterpretation: 'speakers',
    });

    workletNode.port.onmessage = (e: MessageEvent<Float32Array>) => {
      if (!useRecordingStore.getState().isRecording) return;
      pcmBuffer.push(e.data);
      pcmBufferLen += e.data.length;
      if (pcmBufferLen >= chunkSamples) flushPcmBuffer();
    };

    // Mic source — if the user set up an OS-level aggregate device
    // (BlackHole + mic), this stream already mixes system audio in.
    context.createMediaStreamSource(micStream).connect(workletNode);

    // Optional Chrome-tab audio, armed from the source picker before
    // recording. Summed into the SAME worklet input as the mic, so the
    // websocket / VAD / ASR pipeline downstream is unchanged (one mono
    // stream) and speaker diarization separates the voices. The user's
    // headphones keep the tab audio from looping back through the mic.
    const tabStream = useRecordingStore.getState().tabStream;
    const tabTrack = tabStream?.getAudioTracks()[0] ?? null;
    if (tabStream && tabTrack && tabTrack.readyState === 'live') {
      tabSourceNode = context.createMediaStreamSource(tabStream);
      tabSourceNode.connect(workletNode);
      // "Stop sharing" ends the track — detach the dead source so it
      // doesn't linger in the graph for the rest of the recording.
      tabTrack.addEventListener('ended', () => {
        if (tabSourceNode) {
          tabSourceNode.disconnect();
          tabSourceNode = null;
        }
      });
    }

    workletNode.connect(context.destination);

    startWatchdog();
  } catch (err) {
    // Audio setup failed — getUserMedia denied, device gone, autoplay
    // policy, worklet load failure. Tear everything down so the next
    // click starts clean, and tell the user what actually happened.
    const detail = err instanceof Error ? err.message : String(err);
    console.error('[record] audio setup failed:', err);
    const errName = err instanceof DOMException ? err.name : '';
    const message =
      errName === 'NotAllowedError'
        ? 'Microphone access is blocked. Click the mic/lock icon in your ' +
          'browser address bar and allow the microphone for this site, then ' +
          'try again. (Embedded previews cannot access the microphone. ' +
          'open the app in your regular browser.)'
        : errName === 'NotFoundError'
          ? 'No microphone found. Connect or select an input device in your system sound settings.'
          : `Recording failed to start: ${detail}`;
    useUIStore.getState().addToast({ type: 'error', message, duration: 8000 });
    stopWatchdog();
    stopPing();
    if (ws) {
      try { ws.close(); } catch { /* ignore */ }
      ws = null;
    }
    if (tabSourceNode) {
      tabSourceNode.disconnect();
      tabSourceNode = null;
    }
    const store = useRecordingStore.getState();
    store.setConnected(false);
    store.setRecording(false);
    store.setRecordingSession(null);
    owningSessionId = null;
    void store.releaseMic('header-recorder');
    store.releaseTabAudio();
  }
}

function stop(): void {
  const rec = useRecordingStore.getState();
  const owner = owningSessionId;
  rec.setRecording(false);
  stopWatchdog();
  stopPing();

  if (pcmBufferLen > 0) flushPcmBuffer();

  // Detach our own worklet node — the only genuinely per-consumer piece.
  // The MediaStream/AudioContext are shared with dictation; releaseMic
  // only tears the device down when the LAST consumer lets go.
  if (workletNode) {
    workletNode.port.onmessage = null;
    workletNode.disconnect();
    workletNode = null;
  }
  if (tabSourceNode) {
    tabSourceNode.disconnect();
    tabSourceNode = null;
  }
  void rec.releaseMic('header-recorder');
  // The tab stream belongs to this recording only (not shared like the
  // mic), so tear it down here — the next recording re-arms it.
  rec.releaseTabAudio();

  // Settle the transcript before closing the socket: Parakeet's stop
  // handler emits one last transcript for the in-flight sentence, and
  // Whisper may drain its VAD buffer. Wait for session_ended OR 200ms of
  // quiet (1.5s hard deadline), then commit any leftover interim text and
  // save the OWNING session.
  const sock = ws;
  const finishUp = () => {
    const tStore = getTranscriptionStore(owner).getState();
    const pending = (tStore.interimText || '').trim();
    if (pending) {
      const lastSeg = tStore.segments[tStore.segments.length - 1];
      const speaker = lastSeg?.speaker ?? 'Speaker 1';
      const now = Date.now();
      tStore.addSegment({
        id: crypto.randomUUID(),
        text: pending,
        speaker,
        timestamp: now,
        edited: false,
      });
    }
    tStore.setInterimText('');

    if (sock) {
      try { sock.close(); } catch { /* ignore */ }
    }
    if (ws === sock) ws = null;
    const store = useRecordingStore.getState();
    store.setConnected(false);
    store.setRecordingSession(null);
    owningSessionId = null;

    // Synchronous save of the session that OWNS this recording — durable
    // the moment the mic icon goes off, wherever the user is looking.
    try {
      if (owner) useSessionStore.getState().saveSession(owner);
    } catch { /* best-effort */ }
  };

  if (sock && sock.readyState === WebSocket.OPEN) {
    let done = false;
    let quietTimer = 0;
    const settle = () => {
      if (done) return;
      done = true;
      clearTimeout(hardDeadline);
      clearTimeout(quietTimer);
      sock.removeEventListener('message', onMsg);
      finishUp();
    };
    const onMsg = (ev: MessageEvent) => {
      // Reset the quiet timer on every frame so a burst of late
      // transcripts all land before we close.
      clearTimeout(quietTimer);
      quietTimer = window.setTimeout(settle, 200);
      try {
        const msg = JSON.parse(ev.data as string) as Record<string, unknown>;
        if (msg.type === 'session_ended') settle();
      } catch { /* not JSON — ignore */ }
    };
    quietTimer = window.setTimeout(settle, 200);
    const hardDeadline = window.setTimeout(settle, 1500);
    sock.addEventListener('message', onMsg);
    try {
      sock.send(JSON.stringify({ type: 'stop' }));
    } catch {
      settle();
    }
  } else {
    finishUp();
  }
}

export const recordingController = { start, stop };

let eventsInitialized = false;

/** Window-event wiring (start/stop from welcome cards, live backend
 *  switch, participant hint). Called once from AppShell. */
export function initRecordingControllerEvents(): void {
  if (eventsInitialized) return;
  eventsInitialized = true;

  // Start from elsewhere (welcome card). Must be dispatched synchronously
  // inside a user gesture so mic acquisition keeps the user activation.
  window.addEventListener('whisper-start-recording', () => {
    if (useRecordingStore.getState().isRecording) return;
    let sid = useSessionStore.getState().currentSessionId;
    if (!sid) sid = useSessionStore.getState().createSession();
    void start(sid);
  });

  // Live ASR-engine switch from the transcript panel: re-size the client
  // chunk cadence and tell the recording socket (wherever its session is).
  window.addEventListener('whisper-set-backend', (e: Event) => {
    const backend = (e as CustomEvent<{ backend?: string }>).detail?.backend ?? 'whisper';
    chunkSamples = chunkSamplesForBackend(backend);
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'set_backend', backend }));
    }
  });

  // Participant-count hint: kept for sockets opened later, relayed live
  // to an open one.
  window.addEventListener('whisper-set-speakers', (e: Event) => {
    const count = (e as CustomEvent<{ count?: number }>).detail?.count ?? 0;
    speakerCount = count;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'set_speakers', count }));
    }
  });
}
