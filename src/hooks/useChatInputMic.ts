import { useCallback, useEffect, useRef, useState } from 'react';
import { useRecordingStore } from '@/stores/recordingStore';
import { useUIStore } from '@/stores/uiStore';

/**
 * useChatInputMic — push-to-talk style microphone capture for the chat input.
 *
 * Independent of the meeting transcription pipeline (Header.tsx /
 * useRecordingStore). Opens its own WebSocket to /ws, captures mic audio via
 * the existing pcm-processor AudioWorklet, streams PCM16 chunks, and invokes
 * `onTranscript` for every transcript message the server emits.
 *
 * Each session of recording is short and self-contained: a fresh WS, a fresh
 * AudioContext, fully torn down on stop().
 */
export interface UseChatInputMicOptions {
  /** Called with each transcript fragment as it arrives from the server. */
  /** Called for each transcript update. `isFinal` is false for the live,
   *  word-by-word interim draft (replaces the prior draft) and true for a
   *  settled sentence (commit it). Mirrors the meeting recorder's
   *  interim/transcript split so dictation reveals word-by-word. */
  onTranscript: (text: string, isFinal: boolean) => void;
  /** Called when an unrecoverable error occurs (e.g. mic permission denied). */
  onError?: (err: Error) => void;
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

const SAMPLE_RATE = 16000;
// Short chunk so trailing audio (and a spoken submit command) reaches the server
// fast: dictation is interactive, not a long recording, so we trade a little
// extra per-chunk decode overhead for much lower time-to-transcript. The meeting
// recorder uses a larger ~1.5s chunk for throughput; here latency wins.
const CHUNK_SECONDS = 0.5;
const CHUNK_SAMPLES = Math.round(CHUNK_SECONDS * SAMPLE_RATE);

export function useChatInputMic(opts: UseChatInputMicOptions): UseChatInputMicResult {
  const { onTranscript, onError, sessionId } = opts;

  const [isRecording, setIsRecording] = useState(false);
  const [isConnecting, setIsConnecting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const wsRef = useRef<WebSocket | null>(null);
  const audioCtxRef = useRef<AudioContext | null>(null);
  const workletRef = useRef<AudioWorkletNode | null>(null);
  const micStreamRef = useRef<MediaStream | null>(null);
  const pcmBufferRef = useRef<Float32Array[]>([]);
  const pcmBufferLenRef = useRef<number>(0);
  // Cancellation guard. `start()` is async (it awaits the socket open,
  // acquireMic, and the worklet module), so a stop()/cancel can land while
  // it is still connecting. Each start() captures the current token; stop()
  // and unmount bump it. After every await, start() checks whether its token
  // is still current — if not, it releases whatever it acquired and bails out
  // WITHOUT activating a session that was already cancelled (which would leave
  // isRecording=true on a dead socket + an unreleased mic device).
  const startTokenRef = useRef(0);
  // True only while start() is between calling acquireMic() and receiving the
  // stream. acquireMic() registers our owner synchronously but only stores the
  // MediaStream after getUserMedia resolves, so a teardown() in this window
  // would drop the owner before the stream exists (releasing nothing, then
  // orphaning the resolved stream). While this is set, teardown() defers the
  // releaseMic() to the in-flight start(), which performs exactly one release
  // once acquireMic() resolves — so the mic track is always stopped.
  const acquireInFlightRef = useRef(false);
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
    if (ws) {
      try {
        if (ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: 'stop' }));
        }
        ws.close();
      } catch { /* ignore */ }
      wsRef.current = null;
    }
    pcmBufferRef.current = [];
    pcmBufferLenRef.current = 0;
    // Defer the mic release while a start() is mid-acquireMic: our owner is
    // registered but the stream is not yet stored, so releasing here would
    // stop nothing and then leak the stream acquireMic is about to resolve.
    // The in-flight start() releases it exactly once when it resumes.
    if (!acquireInFlightRef.current) {
      void useRecordingStore.getState().releaseMic('chat-input-mic');
    }
  }, []);

  const stop = useCallback(() => {
    // Bump the token so any in-flight start() sees the cancel and bails out
    // instead of activating a session on a socket/stream we are tearing down.
    startTokenRef.current += 1;
    if (pcmBufferLenRef.current > 0) flushPcm();
    teardown();
    setIsRecording(false);
    setIsConnecting(false);
  }, [flushPcm, teardown]);

  const start = useCallback(async () => {
    if (isRecording || isConnecting) return;
    setError(null);
    setIsConnecting(true);

    // Token for THIS start attempt. If a stop()/cancel (or unmount) bumps the
    // token while we are awaiting below, `cancelled()` becomes true and we
    // bail out. On bail-out we call teardown(), which releases the socket and
    // (via releaseMic) the mic device, so nothing acquired so far is left open.
    const myToken = ++startTokenRef.current;
    const cancelled = () => startTokenRef.current !== myToken;

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
      ws.onmessage = (event: MessageEvent) => {
        try {
          const msg = JSON.parse(event.data as string) as {
            type?: string;
            text?: string;
            label?: string;
            progress?: number;
            stage?: string;
          };
          if (msg.type === 'interim' && msg.text) {
            // Live, word-by-word draft of the utterance in progress.
            onTranscriptRef.current(msg.text, false);
          } else if (msg.type === 'transcript' && msg.text) {
            // Settled sentence — commit it.
            onTranscriptRef.current(msg.text, true);
          } else if (msg.type === 'model_loading') {
            // Local mode: dictation forces the Parakeet ASR engine, which on a
            // cold start must load (or first-run download) into memory. Without
            // surfacing this the mic just seems stuck. Drive the shared
            // model-loading banner (same store the meeting recorder uses via
            // recordingController) so the user sees the progress; 'ready'
            // auto-hides after a brief beat.
            const stage = (msg.stage ?? 'loading') as 'start' | 'downloading' | 'loading' | 'ready';
            useUIStore.getState().setModelLoading({
              label: msg.label ?? 'Model',
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
            // Engine freed on switch — clear any stale banner.
            useUIStore.getState().setModelLoading(null);
          }
        } catch { /* ignore */ }
      };
      ws.onerror = () => {
        setError('Connection error');
      };

      // Wait for socket to open before sending audio
      await new Promise<void>((resolve, reject) => {
        ws.onopen = () => resolve();
        ws.addEventListener('error', () => reject(new Error('WebSocket failed to open')), { once: true });
        ws.addEventListener('close', () => reject(new Error('WebSocket closed')), { once: true });
      });

      // Cancelled while the socket was opening — bail before touching the mic.
      if (cancelled()) { teardown(); return; }

      // 2. Mic + AudioWorklet — shared refcounted acquisition. If the
      // meeting recorder is already running, this returns the same
      // MediaStream + AudioContext rather than spawning a competing
      // getUserMedia call (which the OS often resolves with a silent
      // stream when the device is already in use).
      acquireInFlightRef.current = true;
      const { stream, context: ctx } = await useRecordingStore.getState().acquireMic('chat-input-mic');
      acquireInFlightRef.current = false;
      micStreamRef.current = stream;
      audioCtxRef.current = ctx;

      // Cancelled while acquiring the mic. Our owner is still held (teardown()
      // deferred the release while acquireInFlightRef was set), so teardown()
      // now performs the single releaseMic() that stops the just-acquired
      // getUserMedia track — the device is never left open.
      if (cancelled()) { teardown(); return; }

      // BASE_URL resolves correctly in both dev (Vite serves /public
      // at '/') and prod ('/static/dist/'). See Header.tsx for the
      // same fix and the SPA-fallback bug it works around.
      await ctx.audioWorklet.addModule(`${import.meta.env.BASE_URL}pcm-processor.js`);

      // Cancelled while loading the worklet module — release the mic + socket.
      if (cancelled()) { teardown(); return; }

      const node = new AudioWorkletNode(ctx, 'pcm-processor');
      workletRef.current = node;

      node.port.onmessage = (e: MessageEvent<Float32Array>) => {
        pcmBufferRef.current.push(e.data);
        pcmBufferLenRef.current += e.data.length;
        if (pcmBufferLenRef.current >= CHUNK_SAMPLES) flushPcm();
      };

      ctx.createMediaStreamSource(stream).connect(node);
      node.connect(ctx.destination);

      setIsConnecting(false);
      setIsRecording(true);
    } catch (err) {
      // Ensure the deferral flag never sticks if acquireMic() itself threw.
      acquireInFlightRef.current = false;
      // A cancel can surface here too (e.g. stop() closed the socket mid-open,
      // rejecting the open promise). That is not a real error, so tear down
      // quietly without surfacing an error or clobbering a later start's state.
      if (cancelled()) { teardown(); return; }
      const e = err instanceof Error ? err : new Error(String(err));
      setError(e.message);
      onError?.(e);
      teardown();
      setIsConnecting(false);
      setIsRecording(false);
    }
  }, [isRecording, isConnecting, flushPcm, teardown, onError, sessionId]);

  const toggle = useCallback(() => {
    if (isRecording || isConnecting) stop();
    else void start();
  }, [isRecording, isConnecting, start, stop]);

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      // Treat unmount as a cancel so an in-flight start() bails on resume
      // (and releases its mic) instead of activating on a dead component.
      startTokenRef.current += 1;
      teardown();
    };
  }, [teardown]);

  return { isRecording, isConnecting, error, start, stop, toggle };
}
