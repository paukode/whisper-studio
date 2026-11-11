import { useCallback, useEffect, useRef, useState } from 'react';
import { useRecordingStore } from '@/stores/recordingStore';
import { useUIStore, type ModelBannerFlow } from '@/stores/uiStore';
import { createStartGuard, StartCancelled } from '@/utils/startGuard';
import { acquireNativeMic, isNativeMicAvailable, releaseNativeMic } from '@/services/nativeMicSource';

/**
 * useChatInputMic — push-to-talk style microphone capture for the chat input.
 *
 * Independent of the meeting transcription pipeline (recordingController /
 * useRecordingStore). Opens its own WebSocket to /ws, captures mic audio,
 * streams PCM16 chunks, and invokes `onTranscript` for every transcript
 * message the server emits. Inside the Mac app the audio comes from the
 * shell's own mic, the one the recorder uses (services/nativeMicSource), with
 * Apple's voice processing off: the web view's mic turned other sound on the
 * Mac down for a few seconds. A plain browser captures through the
 * pcm-processor AudioWorklet.
 *
 * Each session of recording is short and self-contained: a fresh WS, a fresh
 * AudioContext, fully torn down on stop().
 */
export interface UseChatInputMicOptions {
  /** Called for each transcript update. `isFinal` is false for the live,
   *  word-by-word interim draft (replaces the prior draft) and true for a
   *  settled sentence (commit it). Mirrors the meeting recorder's
   *  interim/transcript split so dictation reveals word-by-word. */
  onTranscript: (text: string, isFinal: boolean) => void;
  /** Optional session id — when set, server keeps speaker profiles in
   *  the per-session bucket so reconnects don't lose voice memory. */
  sessionId?: string | null;
}

export interface UseChatInputMicResult {
  isRecording: boolean;
  isConnecting: boolean;
  error: string | null;
  start: () => Promise<void>;
  stop: () => void;
  toggle: () => void;
}

/** Dictation's name on the shared native mic (the recorder has its own). */
const MIC_OWNER = 'chat-input-mic';

const SAMPLE_RATE = 16000;
// Short chunk so trailing audio (and a spoken submit command) reaches the server
// fast: dictation is interactive, not a long recording, so we trade a little
// extra per-chunk decode overhead for much lower time-to-transcript. The meeting
// recorder uses a larger ~1.5s chunk for throughput; here latency wins.
const CHUNK_SECONDS = 0.5;
const CHUNK_SAMPLES = Math.round(CHUNK_SECONDS * SAMPLE_RATE);

export function useChatInputMic(opts: UseChatInputMicOptions): UseChatInputMicResult {
  const { onTranscript, sessionId } = opts;

  const [isRecording, setIsRecording] = useState(false);
  const [isConnecting, setIsConnecting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const wsRef = useRef<WebSocket | null>(null);
  // The socket's run on the shared model banner: teardown ends it.
  const bannerRef = useRef<ModelBannerFlow | null>(null);
  const audioCtxRef = useRef<AudioContext | null>(null);
  const workletRef = useRef<AudioWorkletNode | null>(null);
  const micStreamRef = useRef<MediaStream | null>(null);
  const pcmBufferRef = useRef<Float32Array[]>([]);
  const pcmBufferLenRef = useRef<number>(0);
  // `start()` is async (it awaits the socket open, acquireMic, and the
  // worklet module), so a stop() can land while it is still connecting.
  // stop() and unmount cancel the attempt; start() awaits through it and
  // unwinds instead of activating a session on a socket stop() already
  // closed (see utils/startGuard).
  const [startGuard] = useState(createStartGuard);
  // Keep a stable reference to the latest callback so the worklet message
  // handler always sees the current function without re-binding.
  const onTranscriptRef = useRef(onTranscript);
  useEffect(() => { onTranscriptRef.current = onTranscript; }, [onTranscript]);

  const flushPcm = useCallback(() => {
    if (pcmBufferLenRef.current === 0) return;
    const ws = wsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;

    const combined = new Float32Array(pcmBufferLenRef.current);
    let offset = 0;
    for (const chunk of pcmBufferRef.current) {
      combined.set(chunk, offset);
      offset += chunk.length;
    }
    pcmBufferRef.current = [];
    pcmBufferLenRef.current = 0;

    const pcm16 = new Int16Array(combined.length);
    for (let i = 0; i < combined.length; i++) {
      const s = Math.max(-1, Math.min(1, combined[i]));
      pcm16[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
    }
    ws.send(pcm16.buffer);
  }, []);

  const teardown = useCallback(() => {
    // Per-consumer teardown only — never touch the underlying
    // MediaStream tracks or the AudioContext directly. Those are
    // refcounted in recordingStore; releaseMic() handles the actual
    // device shutdown when no other consumer is left. Touching them
    // here would yank the device out from under the meeting recorder
    // if it's also active.
    if (workletRef.current) {
      workletRef.current.port.onmessage = null;
      try { workletRef.current.disconnect(); } catch { /* ignore */ }
      workletRef.current = null;
    }
    audioCtxRef.current = null;
    micStreamRef.current = null;
    const ws = wsRef.current;
    // Out of the ref before it closes, so its own close event is not taken
    // for the server ending the session (see the close handler in start()).
    wsRef.current = null;
    if (ws) {
      try {
        if (ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: 'stop' }));
        }
        ws.close();
      } catch { /* ignore */ }
    }
    pcmBufferRef.current = [];
    pcmBufferLenRef.current = 0;
    // A Stop while the model loads: nothing on the closed socket would clear
    // its banner any more.
    bannerRef.current?.done();
    bannerRef.current = null;
    // Safe while either mic is still opening (each closes a mic that opens
    // with no owner left), and a no-op for the one this start did not use.
    void releaseNativeMic(MIC_OWNER);
    void useRecordingStore.getState().releaseMic(MIC_OWNER);
  }, []);

  const stop = useCallback(() => {
    // Cancel an in-flight start() so it unwinds instead of activating a
    // session on a socket/stream we are tearing down.
    startGuard.cancel();
    if (pcmBufferLenRef.current > 0) flushPcm();
    teardown();
    setIsRecording(false);
    setIsConnecting(false);
  }, [startGuard, flushPcm, teardown]);

  const start = useCallback(async () => {
    if (isRecording || isConnecting) return;
    setError(null);
    setIsConnecting(true);

    // Every await below goes through this attempt. Once stop() (or unmount)
    // cancels it, the await throws StartCancelled: stop() has already torn
    // down the socket and released the mic, so the start just returns.
    const attempt = startGuard.begin();

    try {
      // 1. Open WebSocket.
      //   - backend=streaming  → dictation ALWAYS uses Parakeet, regardless of
      //     the global transcription backend (so it can run alongside a
      //     Whisper or Parakeet meeting recorder).
      //   - dictation=1        → backend skips speaker-ID, so the mic never
      //     races the meeting recorder on the shared speaker-profile bucket.
      //   - session_id (optional) is still passed for continuity; with
      //     dictation=1 it's harmless (no speaker writes happen).
      const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
      const params = new URLSearchParams({ backend: 'streaming', dictation: '1' });
      if (sessionId) params.set('session_id', sessionId);
      const wsUrl = `${protocol}//${window.location.host}/ws?${params.toString()}`;
      const ws = new WebSocket(wsUrl);
      wsRef.current = ws;
      // This socket's run on the shared model banner (see onmessage).
      const banner = useUIStore.getState().openModelBanner('dictation');
      bannerRef.current = banner;
      ws.onmessage = (event: MessageEvent) => {
        try {
          const msg = JSON.parse(event.data as string) as {
            type?: string;
            text?: string;
            label?: string;
            progress?: number;
            stage?: string;
          };
          if (msg.type === 'interim' && typeof msg.text === 'string') {
            // Live, word-by-word draft of the utterance in progress. An empty
            // one withdraws the draft: its utterance closed without a final.
            onTranscriptRef.current(msg.text, false);
          } else if (msg.type === 'transcript' && msg.text) {
            // Settled sentence — commit it.
            onTranscriptRef.current(msg.text, true);
          } else if (msg.type === 'model_loading') {
            // Local mode: dictation forces the Parakeet ASR engine, which on a
            // cold start must load (or first-run download) into memory. Without
            // surfacing this the mic just seems stuck. Drive the shared
            // model-loading banner (the meeting recorder drives it the same
            // way) so the user sees the progress; 'ready' hides itself after
            // a brief beat.
            banner.show({
              label: msg.label ?? 'Model',
              progress: typeof msg.progress === 'number' ? msg.progress : 0,
              stage: (msg.stage ?? 'loading') as 'start' | 'downloading' | 'loading' | 'ready',
            });
          } else if (msg.type === 'model_unloaded') {
            // Engine freed on switch: clear the stale banner if it is ours.
            banner.clear();
          }
        } catch { /* ignore */ }
      };
      ws.onerror = () => {
        setError('Connection error');
      };

      // Wait for socket to open before sending audio
      await attempt.hold(
        new Promise<void>((resolve, reject) => {
          ws.onopen = () => resolve();
          ws.addEventListener('error', () => reject(new Error('WebSocket failed to open')), { once: true });
          ws.addEventListener('close', () => reject(new Error('WebSocket closed')), { once: true });
        }),
      );
      // Open: from here a close of this socket is the server ending the
      // session (teardown() takes it out of wsRef before closing it). It ends
      // dictation through the Stop button's path, so the hook never shows a
      // recording on a dead socket while unsent audio piles up.
      ws.onclose = () => {
        if (wsRef.current === ws) stop();
      };

      const takeSamples = (samples: Float32Array) => {
        pcmBufferRef.current.push(samples);
        pcmBufferLenRef.current += samples.length;
        if (pcmBufferLenRef.current >= CHUNK_SAMPLES) flushPcm();
      };

      if (isNativeMicAvailable()) {
        // 2. The shell's mic, shared with the recorder: already 16 kHz mono.
        await attempt.hold(
          acquireNativeMic(MIC_OWNER, {
            onAudio: (samples) => takeSamples(samples),
            onError: (message) => {
              setError(message);
              stop();
            },
          }),
          () => void releaseNativeMic(MIC_OWNER),
        );
      } else {
        // 2. Mic + AudioWorklet: shared refcounted acquisition. If the
        // meeting recorder is already running, this returns the same
        // MediaStream + AudioContext rather than spawning a competing
        // getUserMedia call (which the OS often resolves with a silent
        // stream when the device is already in use).
        const { stream, context: ctx } = await attempt.hold(
          useRecordingStore.getState().acquireMic(MIC_OWNER),
        );
        micStreamRef.current = stream;
        audioCtxRef.current = ctx;

        // BASE_URL resolves correctly in both dev (Vite serves /public
        // at '/') and prod ('/static/dist/'). services/recordingController.ts
        // loads the worklet the same way.
        await attempt.hold(ctx.audioWorklet.addModule(`${import.meta.env.BASE_URL}pcm-processor.js`));

        const node = new AudioWorkletNode(ctx, 'pcm-processor');
        workletRef.current = node;

        node.port.onmessage = (e: MessageEvent<Float32Array>) => takeSamples(e.data);

        ctx.createMediaStreamSource(stream).connect(node);
        node.connect(ctx.destination);
      }

      setIsConnecting(false);
      setIsRecording(true);
    } catch (err) {
      // Cancelled (stop() may also have closed the socket mid-open, rejecting
      // the open promise): not a real error, and stop() already tore down
      // this start's socket and mic. A later start's state is left alone.
      if (err instanceof StartCancelled) return;
      const e = err instanceof Error ? err : new Error(String(err));
      setError(e.message);
      teardown();
      setIsConnecting(false);
      setIsRecording(false);
    }
  }, [isRecording, isConnecting, startGuard, flushPcm, teardown, stop, sessionId]);

  const toggle = useCallback(() => {
    if (isRecording || isConnecting) stop();
    else void start();
  }, [isRecording, isConnecting, start, stop]);

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      // Treat unmount as a cancel so an in-flight start() unwinds on resume
      // instead of activating on a dead component.
      startGuard.cancel();
      teardown();
    };
  }, [startGuard, teardown]);

  return { isRecording, isConnecting, error, start, stop, toggle };
}
