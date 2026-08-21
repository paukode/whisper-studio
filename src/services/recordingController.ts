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
import type { NativeSourceSelection } from '@/stores/recordingStore';
import { useSettingsStore } from '@/stores/settingsStore';
import { useUIStore } from '@/stores/uiStore';
import { ensureRecordingModels } from '@/services/recordingModels';
import { put } from '@/api/client';
import {
  isNativeAudioAvailable,
  listNativeAudioSources,
  startNativeCapture,
  stopNativeCapture,
} from '@/services/nativeAudioSource';
import { NativeAudioMixer } from '@/services/nativeAudioMixer';
import { isNativeTranslationAvailable, translateNative } from '@/services/nativeTranslation';
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
/** True while the pre-recording model gate downloads weights. A second
 *  record click during the wait must not start a second gate — the
 *  banner's Cancel is the way out. */
let modelGateActive = false;
/** The ASR engine the recording websocket is actually running server-side.
 *  Distinct from config.transcriptionBackend, which the header select flips
 *  the instant the user picks a new engine — BEFORE its weights are on disk.
 *  Tracked so a live switch can gate the new engine's download first and, on
 *  cancel, put the header select back to the engine still in use. */
let activeBackend = 'streaming';
/** True while a live engine switch is gating the new engine's download, so a
 *  second switch can't race a set_backend relay past a half-finished one. */
let liveSwitchActive = false;
/** The user dismissed the server-driven "loading into memory" banner. A native
 *  MLX load can't be aborted mid-flight, so we simply stop re-showing its ramp
 *  until the next load starts or finishes. */
let loadModelBannerDismissed = false;

const engineOf = (backend: string): 'whisper' | 'parakeet' | 'canary' =>
  backend === 'whisper' ? 'whisper' : backend === 'canary' ? 'canary' : 'parakeet';
/** Buffers native (shell-captured) samples between mic worklet frames in
 *  mixed mode. Null in mic-only and native-only modes. */
let nativeMixer: NativeAudioMixer | null = null;
/** True while the shell's process-tap capture is live. */
let nativeCaptureRunning = false;

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

function onTranscriptResult(
  text: string,
  speaker: string,
  chunkId?: number,
  translating?: boolean,
): void {
  if (!text) return;
  const store = ownerStore();
  const segs = store.segments;
  const lastSeg = segs.length > 0 ? segs[segs.length - 1] : null;
  if (lastSeg && lastSeg.speaker === speaker) {
    // Same speaker still talking: grow the existing segment. The chunk id
    // is recorded so diarization corrections can split this merge later.
    store.appendSegmentText(lastSeg.id, text, chunkId, translating);
  } else {
    const now = Date.now();
    store.addSegment({
      id: crypto.randomUUID(),
      text,
      speaker,
      chunks: chunkId !== undefined ? [{ id: chunkId, start: 0 }] : undefined,
      pendingTranslations: translating && chunkId !== undefined ? [chunkId] : undefined,
      timestamp: now,
      edited: false,
      receivedAt: now,
      freshIndex: 0,
    });
  }
}

// ── Native (shell) audio source ─────────────────────────────────────────

/** App pids go stale across relaunches; re-resolve the armed selection by
 *  bundle id against a fresh source list, falling back to the stored pid.
 *  System audio (-1) passes straight through. */
async function resolveNativePid(sel: NativeSourceSelection): Promise<number> {
  if (sel.pid === -1) return -1;
  try {
    const sources = await listNativeAudioSources();
    const match =
      (sel.bundleID ? sources.find((s) => s.bundleID === sel.bundleID) : undefined) ??
      // Grouped rows carry ALL of an app's audio pids — the armed pid may
      // be any of them, not just the row's representative first pid.
      sources.find((s) => s.pid === sel.pid || (s.pids?.includes(sel.pid) ?? false));
    if (match) return match.pid;
  } catch {
    // Enumeration failed — the stored pid is still worth a try.
  }
  return sel.pid;
}

/** Fallback RMS for chunks from older shells that don't send one. */
function computeRms(samples: Float32Array): number {
  if (samples.length === 0) return 0;
  let sum = 0;
  for (let i = 0; i < samples.length; i++) sum += samples[i] * samples[i];
  return Math.sqrt(sum / samples.length);
}

/** One console line per recording when the mixer misbehaves — the mic path
 *  must stay alive regardless, so mixer failures are logged, not thrown. */
let mixerFailureLogged = false;

/** Decoded native chunk (16 kHz mono Float32). Mixed mode buffers it for the
 *  mic worklet (the mic is the clock master); native-only mode has no mic
 *  clock, so the chunks drive the chunker/websocket directly. */
function handleNativeAudio(samples: Float32Array, rms?: number): void {
  if (!useRecordingStore.getState().isRecording) return;
  useRecordingStore.getState().setNativeLevel(rms ?? computeRms(samples));
  if (nativeMixer) {
    // Fail-open: a mixer fault must never take the MIC down with it. Drop
    // this native chunk and keep recording; the worklet handler below keeps
    // forwarding mic frames whether or not the mixer has data.
    try {
      nativeMixer.push(samples);
    } catch (err) {
      if (!mixerFailureLogged) {
        mixerFailureLogged = true;
        console.error('[record] native mixer push failed — dropping native audio:', err);
      }
    }
    return;
  }
  pcmBuffer.push(samples);
  pcmBufferLen += samples.length;
  if (pcmBufferLen >= chunkSamples) flushPcmBuffer();
}

/** Silent-capture advisory from the shell (tap running, nothing but zeros).
 *  The capture keeps going; surface an actionable warning toast (persists
 *  to the notification bell via source: 'recording'). */
function handleNativeWarning(message: string): void {
  useRecordingStore.getState().setNativeLevel(0);
  useUIStore.getState().addToast({
    type: 'warning',
    message,
    duration: 10000,
    key: 'native-silent-capture',
    source: 'recording',
  });
}

/** Mid-recording native failure (the shell already stopped the tap).
 *  Mixed mode degrades to mic-only; native-only mode has nothing left to
 *  record, so the recording stops (and saves) cleanly. */
function handleNativeError(message: string): void {
  nativeCaptureRunning = false;
  const wasMixed = nativeMixer !== null;
  nativeMixer = null;
  useRecordingStore.getState().setNativeLevel(0);
  useUIStore.getState().addToast({
    type: 'error',
    message: `Native audio capture failed: ${message}`,
    duration: 8000,
    source: 'recording',
  });
  if (!useRecordingStore.getState().isRecording) return;
  if (wasMixed) {
    const rec = useRecordingStore.getState();
    const tabLive = !!rec.tabStream
      ?.getAudioTracks()
      .some((t) => t.readyState === 'live');
    rec.setActiveSourceLabel(tabLive ? 'Mic + Tab' : 'Mic');
  } else {
    stop();
  }
}

function buildSourceLabel(mic: boolean, nativeName: string | null, tab: boolean): string {
  const parts: string[] = [];
  if (mic) parts.push('Mic');
  if (nativeName) parts.push(nativeName);
  if (tab) parts.push('Tab');
  return parts.join(' + ') || 'Mic';
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
    // Tell the server the Translate dropdown state and whether this client
    // can run Apple's on-device translator — the server resolves who
    // translates each utterance from these.
    newWs.send(
      JSON.stringify({
        type: 'set_translate',
        mode: useSettingsStore.getState().config.translateMode,
        target: useSettingsStore.getState().config.translateTarget,
        apple_available: isNativeTranslationAvailable(),
      }),
    );
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
        const chunkId = typeof msg.chunk_id === 'number' ? msg.chunk_id : undefined;
        const language = typeof msg.language === 'string' ? msg.language : undefined;
        onTranscriptResult(
          String(msg.text ?? ''),
          String(msg.speaker ?? 'Speaker 1'),
          chunkId,
          msg.translating === true,
        );
        // The server resolved this chunk's translator; translate_via 'apple'
        // means this client runs it through the shell's on-device bridge and
        // fills the same per-chunk pending slot a model decode would. An
        // empty source language means "auto-detect" (Parakeet segments).
        if (msg.translate_via === 'apple' && chunkId !== undefined) {
          const target = typeof msg.translate_target === 'string' ? msg.translate_target : 'en';
          void translateNative(String(msg.text ?? ''), language ?? '', target).then((t) =>
            ownerStore().applyTranslation(chunkId, t, target),
          );
        }
        ownerStore().setInterimText('');
      } else if (msg.type === 'translation') {
        // Translation companion line for an earlier chunk (a model decode on
        // the server). Empty text still clears that chunk's pending marker.
        if (typeof msg.chunk_id === 'number') {
          ownerStore().applyTranslation(
            msg.chunk_id,
            String(msg.text ?? ''),
            typeof msg.target === 'string' ? msg.target : 'en',
          );
        }
      } else if (msg.type === 'speaker_update') {
        // Diarization re-clustered and corrected some earlier labels.
        const updates = Array.isArray(msg.updates) ? msg.updates : [];
        ownerStore().applySpeakerUpdates(updates as { chunk_id: number; speaker: string }[]);
      } else if (msg.type === 'interim') {
        // Parakeet's growing word-by-word draft of the utterance in progress.
        ownerStore().setInterimText(String(msg.text ?? ''));
      } else if (msg.type === 'model_loading') {
        // Local mode (or after a live switch's download): the selected engine
        // is loading into memory. Drive the banner; 'ready' auto-hides.
        const stage = String(msg.stage ?? 'loading') as 'start' | 'loading' | 'ready';
        // A fresh load re-arms the banner even if the last one was dismissed.
        if (stage === 'start') loadModelBannerDismissed = false;
        // Once dismissed, let the (uncancelable) native load finish silently
        // rather than letting the ramp re-open the banner on every tick.
        if (loadModelBannerDismissed && stage !== 'ready') return;
        useUIStore.getState().setModelLoading({
          label: String(msg.label ?? 'Model'),
          progress: typeof msg.progress === 'number' ? msg.progress : 0,
          stage,
          // A native model load (MLX from_pretrained / first decode) can't be
          // interrupted mid-flight, so Cancel here only hides the banner: the
          // load finishes in the background and transcripts start once it's
          // done. The long, genuinely-cancelable part — the multi-GB download —
          // is gated BEFORE this on the record-start / live-switch path.
          onCancel:
            stage === 'ready'
              ? undefined
              : () => {
                  loadModelBannerDismissed = true;
                  useUIStore.getState().setModelLoading(null);
                },
        });
        if (stage === 'ready') {
          loadModelBannerDismissed = false;
          setTimeout(() => {
            const cur = useUIStore.getState().modelLoading;
            if (cur && cur.stage === 'ready') useUIStore.getState().setModelLoading(null);
          }, 700);
        }
      } else if (msg.type === 'model_unloaded') {
        // The outgoing engine was freed on switch — clear any stale banner.
        loadModelBannerDismissed = false;
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
  if (modelGateActive) return; // already downloading the models for a start

  // The 3-active-sessions ceiling counts recording as an activity.
  if (countActiveSessions(sessionId) >= MAX_ACTIVE_SESSIONS) {
    useUIStore.getState().addToast({
      type: 'error',
      message: `${MAX_ACTIVE_SESSIONS} sessions are already active. Stop one before starting a recording.`,
      duration: 5000,
      key: 'parallel-cap',
      source: 'recording',
    });
    return;
  }

  // The speech models this session needs (active ASR engine + speaker
  // encoder) must be on disk BEFORE the mic and websocket open — otherwise
  // the websocket blocks silently while the server downloads gigabytes.
  // Installed: one cheap status GET and we fall straight through. Missing:
  // the gate downloads with a progress banner and resolves 'ready' so the
  // recording continues without a second click; on cancel or error nothing
  // was started yet, so the record button is simply idle again. The
  // server-side lazy download stays as the fallback.
  modelGateActive = true;
  try {
    if ((await ensureRecordingModels()) !== 'ready') return;
  } finally {
    modelGateActive = false;
  }
  // The wait can be minutes long — make sure nothing else grabbed the mic.
  if (useRecordingStore.getState().recordingSessionId) return;

  owningSessionId = sessionId;
  rec.setRecordingSession(sessionId);

  pcmBuffer = [];
  pcmBufferLen = 0;
  // Size chunks for whichever backend is active at start; a live engine
  // switch updates this via the whisper-set-model event. The gate above
  // already ensured this engine's weights are on disk, so it's the engine
  // the server will actually run — record it for live-switch bookkeeping.
  activeBackend = useSettingsStore.getState().config.transcriptionBackend;
  chunkSamples = chunkSamplesForBackend(activeBackend);

  useUIStore.getState().showTranscript();
  rec.setRecording(true);
  connectWS();

  try {
    // ── Source plan ──────────────────────────────────────────────────
    // Three modes: mic only (default), mic + native source (mixed), or
    // native source only. The native source is the shell's Core Audio
    // process tap (system audio or one app); the mic can only be off
    // when a native source stands in for it.
    const armed = useRecordingStore.getState();
    const nativeSel = armed.nativeSource;
    const nativeWanted = !!nativeSel && isNativeAudioAvailable();
    const micWanted = armed.micEnabled || !nativeWanted;

    // Native capture first: its failure modes (TCC denial, app quit) are
    // cheap to surface before the mic is opened. In mixed mode the samples
    // buffer in the mixer until the mic worklet starts pulling them.
    let nativeName: string | null = null;
    if (nativeWanted && nativeSel) {
      try {
        const pid = await resolveNativePid(nativeSel);
        nativeMixer = micWanted ? new NativeAudioMixer() : null;
        mixerFailureLogged = false;
        await startNativeCapture(
          pid,
          {
            onAudio: handleNativeAudio,
            onError: handleNativeError,
            onWarning: handleNativeWarning,
            onStopped: () => {
              nativeCaptureRunning = false;
              useRecordingStore.getState().setNativeLevel(0);
            },
          },
          // App captures re-resolve by bundle id in the shell too (the pid
          // may be stale, and one app can span several audio processes).
          pid >= 0 ? nativeSel.bundleID : undefined,
        );
        nativeCaptureRunning = true;
        nativeName = nativeSel.name;
      } catch (err) {
        nativeMixer = null;
        // Native-only: no other source can carry the recording.
        if (!micWanted) throw err;
        // Mixed: degrade to mic-only and say so — the message from the
        // shell is actionable (System Settings path on TCC denial).
        const detail = err instanceof Error ? err.message : String(err);
        useUIStore.getState().addToast({
          type: 'error',
          message: `Native audio source unavailable — recording with microphone only. ${detail}`,
          duration: 8000,
          source: 'recording',
        });
      }
    }

    let tabMixed = false;
    if (micWanted) {
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
        // Mic is the clock master: each frame pulls the same number of
        // buffered native samples (summed + clamped); underruns mix
        // silence, >500 ms backlogs drop their oldest samples.
        //
        // FAIL-OPEN INVARIANT: the mic frame reaches the chunker on every
        // path. If the mixer throws, mixing is disabled for the rest of the
        // recording and the raw mic frame is forwarded — a broken/silent
        // native source must never take the microphone down with it.
        let frame = e.data;
        if (nativeCaptureRunning && nativeMixer) {
          try {
            frame = nativeMixer.mixInto(e.data);
          } catch (err) {
            nativeMixer = null;
            frame = e.data;
            if (!mixerFailureLogged) {
              mixerFailureLogged = true;
              console.error(
                '[record] native mixer failed — continuing with the microphone only:',
                err,
              );
            }
          }
        }
        pcmBuffer.push(frame);
        pcmBufferLen += frame.length;
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
        tabMixed = true;
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
    }
    // Native-only mode: no getUserMedia, no AudioContext, no worklet — the
    // shell's chunks feed the chunker directly (see handleNativeAudio), so
    // recording needs no microphone permission at all.

    useRecordingStore
      .getState()
      .setActiveSourceLabel(buildSourceLabel(micWanted, nativeName, tabMixed));

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
    useUIStore.getState().addToast({ type: 'error', message, duration: 8000, source: 'recording' });
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
    if (nativeCaptureRunning) {
      nativeCaptureRunning = false;
      void stopNativeCapture();
    }
    nativeMixer = null;
    const store = useRecordingStore.getState();
    store.setNativeLevel(0);
    store.setConnected(false);
    store.setRecording(false);
    store.setRecordingSession(null);
    store.setActiveSourceLabel(null);
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
  // Native capture: tell the shell to drain and destroy the tap. Chunks
  // that race in before onStopped are dropped by handleNativeAudio's
  // isRecording check.
  if (nativeCaptureRunning) {
    nativeCaptureRunning = false;
    void stopNativeCapture();
  }
  nativeMixer = null;
  rec.setNativeLevel(0);
  rec.setActiveSourceLabel(null);
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

/** Live engine switch while recording: gate the new engine's weights (the
 *  shared download banner + Cancel), then hand the open socket over to it.
 *  On cancel or download failure the recording stays on the current engine
 *  and the header select is put back to match, so the UI never claims an
 *  engine the server isn't actually running. */
async function switchBackendLive(backend: string): Promise<void> {
  if (liveSwitchActive) return; // one switch at a time
  const previous = activeBackend;
  if (engineOf(backend) === engineOf(previous)) return; // no real change
  liveSwitchActive = true;
  try {
    // config.transcriptionBackend is already `backend` (the header select set
    // it before dispatching the event), so the gate targets the new engine's
    // weights plus the speaker encoder.
    const result = await ensureRecordingModels();
    if (result !== 'ready') {
      // Cancelled or failed — keep the engine we're on and reflect that in the
      // header select / config so they match the server.
      revertBackend(previous);
      return;
    }
    // Recording may have stopped during a long download; only relay onto a
    // still-open socket.
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    chunkSamples = chunkSamplesForBackend(backend);
    activeBackend = backend;
    ws.send(JSON.stringify({ type: 'set_model', backend }));
  } finally {
    liveSwitchActive = false;
  }
}

/** Put config / the header select back to `backend` (the engine actually
 *  running) after a live switch was cancelled or its download failed. The
 *  in-memory update flips the select back immediately; the PUT persists it. */
function revertBackend(backend: string): void {
  if (engineOf(useSettingsStore.getState().config.transcriptionBackend) === engineOf(backend)) {
    return; // already matches — nothing to revert
  }
  activeBackend = backend;
  chunkSamples = chunkSamplesForBackend(backend);
  useSettingsStore.getState().updateConfig({ transcriptionBackend: backend });
  void put('/api/config', { transcription_backend: backend }).catch(() => {
    /* best-effort persist; the in-memory revert already corrected the UI */
  });
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

  // Live model switch from the transcript panel or Settings (same event).
  window.addEventListener('whisper-set-model', (e: Event) => {
    const backend = (e as CustomEvent<{ backend?: string }>).detail?.backend ?? 'whisper';
    // Idle (no live socket): the model just changed in config; the next
    // start() gates its weights with the download banner. Keep the chunk
    // cadence and the tracked model in sync for that start.
    if (!(ws && ws.readyState === WebSocket.OPEN)) {
      activeBackend = backend;
      chunkSamples = chunkSamplesForBackend(backend);
      return;
    }
    // Recording: the new model's weights must be on disk BEFORE the server
    // switches to it — otherwise the server downloads gigabytes silently (no
    // banner, socket appears to stall) or shows a memory-load ramp stuck at
    // 90%. Gate the download with the same banner + Cancel as record-start,
    // then relay set_model.
    void switchBackendLive(backend);
  });

  // Translate dropdown from the transcript panel or Settings. Config
  // persistence is the dispatcher's job; a live socket just needs the relay
  // (the server seeds from config at connect for sockets opened later).
  window.addEventListener('whisper-set-translate', (e: Event) => {
    const detail = (e as CustomEvent<{ mode?: string; target?: string }>).detail ?? {};
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(
        JSON.stringify({
          type: 'set_translate',
          mode: detail.mode ?? 'off',
          target: detail.target ?? 'en',
          apple_available: isNativeTranslationAvailable(),
        }),
      );
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
