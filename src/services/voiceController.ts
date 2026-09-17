import { get, post } from '@/api/client';
import { getChatStore } from '@/stores/sessionRuntimes';
import { useSessionStore } from '@/stores/sessionStore';
import { useSettingsStore } from '@/stores/settingsStore';
import { useUIStore } from '@/stores/uiStore';
import { useVoiceStore } from '@/stores/voiceStore';
import { recordingController } from '@/services/recordingController';
import { applyVoiceEvent, mergeSpoken } from '@/services/voiceEvents';
import { buildHistoryPayload } from '@/hooks/chatStream/history';
import { VoiceServerEventSchema, VoiceStatusSchema } from '@/types/schemas/voice.schema';
import type { ChatMessage, TeamReportData, ToolUseEvent } from '@/types/chat';

/**
 * voiceController — the engine behind voice mode.
 *
 * One WebSocket to /ws/voice carries 16 kHz PCM16 microphone frames up and
 * JSON events plus 24 kHz PCM16 speech down. Playback is a scheduled chain of
 * AudioBuffer sources on a dedicated 24 kHz AudioContext; barge-in flushes it.
 *
 * The microphone is opened here with echo cancellation ON (the shared
 * recording mic deliberately runs without it for loopback devices), because
 * the speaker is playing the assistant back into the same room.
 *
 * Transcript persistence is frontend-owned, like every other chat message:
 * spoken user turns and the assistant's final words are added to the owning
 * session's chat store and saved through the normal session PUT.
 */
const MIC_SAMPLE_RATE = 16000;
const OUT_SAMPLE_RATE = 24000;
/** 32 ms of 16 kHz audio per frame, what Sonic's docs suggest. */
const FRAME_SAMPLES = 512;
/** Small scheduling lead so back-to-back buffers never gap. */
const PLAY_LEAD_S = 0.06;

/** What a hung-up conversation's socket may still deliver: its delegated
 *  runs' progress and outcomes. */
const DRAINING_EVENTS = new Set([
  'assistant_step', 'team_progress', 'assistant_answer', 'assistant_request',
  'assistant_request_resolved', 'client_action', 'tool_result', 'error',
]);

class VoiceController {
  private ws: WebSocket | null = null;
  private sessionId: string | null = null;
  private micStream: MediaStream | null = null;
  private micCtx: AudioContext | null = null;
  private worklet: AudioWorkletNode | null = null;
  private frameBuf: Float32Array[] = [];
  private frameLen = 0;
  private playCtx: AudioContext | null = null;
  private nextPlayAt = 0;
  private scheduled: AudioBufferSourceNode[] = [];
  private startToken = 0;
  private statusLoaded = false;
  /** Sockets of conversations the user ended while delegated runs were still
   *  going: they stay open until those runs have delivered (see onDraining). */
  private draining: Set<WebSocket> = new Set();
  /** The chat session each socket writes into, and the runs it carries, so a
   *  late answer lands in the right session and a cancelled run's leftovers
   *  can be cleared even when no answer ever comes. */
  private socketSession: Map<WebSocket, string> = new Map();
  private socketRuns: Map<WebSocket, Set<string>> = new Map();

  /** Fetch /api/voice/status once so the Talk button can explain itself. */
  async loadStatus(force = false): Promise<void> {
    if (this.statusLoaded && !force) return;
    try {
      const data = await get('/api/voice/status', { schema: VoiceStatusSchema });
      const parsed = VoiceStatusSchema.safeParse(data);
      if (!parsed.success) return;
      useVoiceStore.getState().setAvailability({
        available: parsed.data.available,
        reason: parsed.data.reason ?? null,
        voiceId: parsed.data.voice_id,
        region: parsed.data.region,
        voices: parsed.data.voices,
      });
      this.statusLoaded = true;
    } catch {
      /* status is advisory; the start() error path covers a dead backend */
    }
  }

  async start(sessionId: string | null): Promise<void> {
    const store = useVoiceStore.getState();
    if (store.status !== 'off') return;
    const sid = sessionId ?? useSessionStore.getState().createSession();
    this.sessionId = sid;
    const myToken = ++this.startToken;
    store.begin(sid);

    try {
      await this.loadStatus();
      const availability = useVoiceStore.getState();
      if (availability.available === false) {
        throw new Error(availability.unavailableReason ?? 'Voice mode is unavailable');
      }

      // 1. Socket first, so a dead backend fails before the mic prompt.
      const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
      const ws = new WebSocket(`${protocol}//${window.location.host}/ws/voice?session_id=${encodeURIComponent(sid)}`);
      ws.binaryType = 'arraybuffer';
      this.ws = ws;
      this.socketSession.set(ws, sid);
      this.socketRuns.set(ws, new Set());
      ws.onmessage = (event: MessageEvent) => this.onMessage(event, ws);
      ws.onclose = () => {
        if (this.ws === ws) this.finish('closed');
        else if (this.draining.has(ws)) this.dropDraining(ws);
      };
      await new Promise<void>((resolve, reject) => {
        ws.onopen = () => resolve();
        ws.addEventListener('error', () => reject(new Error('Voice socket failed to open')), { once: true });
        ws.addEventListener('close', () => reject(new Error('Voice socket closed')), { once: true });
      });
      if (myToken !== this.startToken) { this.teardown(); return; }

      const messages = getChatStore(sid).getState().messages;
      ws.send(JSON.stringify({
        type: 'start',
        session_id: sid,
        history: buildHistoryPayload(messages, true),
        model_key: useSettingsStore.getState().selectedModel,
        voice_id: useVoiceStore.getState().voiceId,
        // The chat session's approval memory, so the delegated assistant is
        // gated exactly like typed chat in this session.
        session_approvals: getChatStore(sid).getState().sessionApprovals,
      }));

      // 2. Microphone with echo cancellation: the assistant's voice is coming
      // out of the speakers a few centimetres away.
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          channelCount: 1,
          sampleRate: MIC_SAMPLE_RATE,
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
      });
      if (myToken !== this.startToken) { stream.getTracks().forEach((t) => t.stop()); this.teardown(); return; }
      this.micStream = stream;
      const ctx = new AudioContext({ sampleRate: MIC_SAMPLE_RATE });
      this.micCtx = ctx;
      await ctx.audioWorklet.addModule(`${import.meta.env.BASE_URL}pcm-processor.js`);
      if (myToken !== this.startToken) { this.teardown(); return; }
      const node = new AudioWorkletNode(ctx, 'pcm-processor', { channelCount: 1, channelCountMode: 'explicit' });
      this.worklet = node;
      node.port.onmessage = (e: MessageEvent<Float32Array>) => this.onMicFrame(e.data);
      ctx.createMediaStreamSource(stream).connect(node);
      node.connect(ctx.destination);

      // 3. Playback context, created after the user gesture that started us.
      this.playCtx = new AudioContext({ sampleRate: OUT_SAMPLE_RATE });
      this.nextPlayAt = 0;
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      useVoiceStore.getState().setError(message);
      useUIStore.getState().addToast({ type: 'error', message: `Voice mode: ${message}`, duration: 5000 });
      this.teardown();
      useVoiceStore.getState().reset();
    }
  }

  /** Hang up. The server answers `ended`, or `draining` first when delegated
   *  runs are still going (their work stays on screen and keeps arriving). */
  stop(): void {
    const status = useVoiceStore.getState().status;
    if (status === 'off') return;
    useVoiceStore.getState().setStatus('ending');
    const ws = this.ws;
    if (ws && ws.readyState === WebSocket.OPEN) {
      try { ws.send(JSON.stringify({ type: 'end' })); } catch { /* finish below */ }
      // If the server never answers, do not leave the UI stuck.
      const token = this.startToken;
      setTimeout(() => {
        if (token === this.startToken && useVoiceStore.getState().status === 'ending') this.finish('user');
      }, 4000);
    } else {
      this.finish('user');
    }
  }

  /** A typed turn while voice is on (cross-modal input). */
  sendText(text: string): void {
    const clean = text.trim();
    const ws = this.ws;
    if (!clean || !ws || ws.readyState !== WebSocket.OPEN) return;
    ws.send(JSON.stringify({ type: 'text', content: clean }));
  }

  /** The user clicked the pending request card (Yes / No / an option). The
   *  request may belong to a run that outlived its conversation, so every
   *  open socket gets the decision; sessions with nothing pending ignore it. */
  resolveRequest(decision: string): void {
    const payload = JSON.stringify({ type: 'resolve', decision });
    for (const ws of [this.ws, ...this.draining]) {
      if (ws && ws.readyState === WebSocket.OPEN) {
        try { ws.send(payload); } catch { /* socket on its way out */ }
      }
    }
  }

  setMuted(muted: boolean): void {
    useVoiceStore.getState().setMuted(muted);
    if (muted) { this.frameBuf = []; this.frameLen = 0; }
  }

  // ── incoming ─────────────────────────────────────────────────────────

  private onMessage(event: MessageEvent, from: WebSocket): void {
    const isDraining = this.draining.has(from);
    if (event.data instanceof ArrayBuffer) {
      if (!isDraining) this.playPcm(event.data);
      return;
    }
    let raw: unknown;
    try { raw = JSON.parse(String(event.data)); } catch { return; }
    const parsed = VoiceServerEventSchema.safeParse(raw);
    if (!parsed.success) return;
    const ev = parsed.data;
    if ('run_id' in ev && typeof ev.run_id === 'string') this.socketRuns.get(from)?.add(ev.run_id);
    if (ev.type === 'draining') for (const r of ev.runs) this.socketRuns.get(from)?.add(r.run_id);
    if (isDraining) {
      // A hung-up conversation only delivers its background runs: their
      // steps, agent progress, requests and answers. Everything else about
      // that conversation is over, and `ended` just closes the socket.
      if (ev.type === 'ended') { this.dropDraining(from); return; }
      if (!DRAINING_EVENTS.has(ev.type)) return;
    }
    const sid = this.socketSession.get(from) ?? this.sessionId;
    applyVoiceEvent(ev, {
      commitUser: (text, typed) => this.commit(sid, { role: 'user', content: text, spoken: !typed }),
      commitAssistant: (text, tools, spoken, teamReports) =>
        this.commit(sid, { role: 'assistant', content: text, spoken, toolUse: tools, teamReports }),
      flushAudio: () => this.flushAudio(),
      onClientAction: (action, value) => this.clientAction(action, value),
      onEnded: (reason) => this.finish(reason),
      onDraining: () => this.onDraining(from),
      toast: (type, message) => useUIStore.getState().addToast({ type, message, duration: 5000 }),
    });
  }

  /** The user hung up but delegated runs are still going: keep this socket
   *  open in the background so their answers still land in the chat, and let
   *  the voice UI (and a new conversation) proceed. */
  private onDraining(ws: WebSocket): void {
    if (this.ws !== ws) return;
    this.draining.add(ws);
    useVoiceStore.getState().adjustDraining(1);
    this.ws = null;
    this.finish('draining');
  }

  private dropDraining(ws: WebSocket): void {
    if (!this.draining.delete(ws)) return;
    ws.onmessage = null;
    ws.onclose = null;
    try { ws.close(); } catch { /* already closed */ }
    // Whatever its runs left on screen without an answer (cancelled, or the
    // server went away) must not linger in the next conversation.
    const store = useVoiceStore.getState();
    for (const runId of this.socketRuns.get(ws) ?? []) store.removeRun(runId);
    this.socketRuns.delete(ws);
    this.socketSession.delete(ws);
    store.adjustDraining(-1);
  }

  /** Stop the background work of hung-up conversations (the chat's Stop or
   *  ESC while voice is off but the assistant is still finishing). */
  cancelRuns(): void {
    const payload = JSON.stringify({ type: 'cancel' });
    for (const ws of this.draining) {
      if (ws.readyState === WebSocket.OPEN) {
        try { ws.send(payload); } catch { /* socket on its way out */ }
      }
    }
  }

  private commit(
    sid: string | null,
    partial: {
      role: 'user' | 'assistant';
      content: string;
      spoken: boolean;
      toolUse?: ToolUseEvent[];
      teamReports?: Record<string, TeamReportData>;
    },
  ): void {
    if (!sid) return;
    const msg: ChatMessage = {
      role: partial.role,
      content: partial.content,
      timestamp: new Date().toISOString(),
      spoken: partial.spoken,
      ...(partial.toolUse && partial.toolUse.length > 0 ? { toolUse: partial.toolUse } : {}),
      ...(partial.teamReports && Object.keys(partial.teamReports).length > 0 ? { teamReports: partial.teamReports } : {}),
    };
    const chat = getChatStore(sid).getState();
    // Consecutive spoken pieces of one turn share a bubble (see mergeSpoken).
    const merged = mergeSpoken(chat.messages, msg);
    if (merged) chat.setMessages(merged);
    else chat.addMessage(msg);
    void useSessionStore.getState().saveSession(sid);
  }

  private clientAction(action: string, value: string): void {
    if (action === 'recording') {
      if (value === 'start') {
        if (this.sessionId) void recordingController.start(this.sessionId);
      } else if (value === 'stop') {
        recordingController.stop();
      }
      return;
    }
    if (action === 'workspace' && value) {
      void this.syncWorkspace(value);
    }
  }

  /** The delegated assistant connected a workspace folder: bring the UI along
   *  through the same endpoint the Workspace dropdown uses (idempotent). */
  private async syncWorkspace(path: string): Promise<void> {
    try {
      const data = await post<{ path?: string }>('/api/workspace/connect', { path });
      const connected = data?.path || path;
      useUIStore.getState().setWsConnected(true, connected);
      useUIStore.getState().addToast({
        type: 'success',
        message: `Connected to ${connected.split('/').pop() ?? connected}`,
        duration: 2500,
      });
    } catch {
      useUIStore.getState().setWsConnected(true, path);
    }
  }

  // ── microphone ───────────────────────────────────────────────────────

  private onMicFrame(samples: Float32Array): void {
    if (useVoiceStore.getState().muted) return;
    this.frameBuf.push(samples);
    this.frameLen += samples.length;
    if (this.frameLen < FRAME_SAMPLES) return;
    const ws = this.ws;
    const combined = new Float32Array(this.frameLen);
    let off = 0;
    for (const chunk of this.frameBuf) { combined.set(chunk, off); off += chunk.length; }
    this.frameBuf = [];
    this.frameLen = 0;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    // Int16Array.buffer is typed ArrayBufferLike; floatToPcm16 allocates a plain ArrayBuffer.
    ws.send(floatToPcm16(combined).buffer as ArrayBuffer);
  }

  // ── playback ─────────────────────────────────────────────────────────

  private playPcm(buf: ArrayBuffer): void {
    const ctx = this.playCtx;
    if (!ctx || buf.byteLength < 2) return;
    const int16 = new Int16Array(buf, 0, Math.floor(buf.byteLength / 2));
    const audio = ctx.createBuffer(1, int16.length, OUT_SAMPLE_RATE);
    const channel = audio.getChannelData(0);
    for (let i = 0; i < int16.length; i++) channel[i] = int16[i] / 0x8000;
    const src = ctx.createBufferSource();
    src.buffer = audio;
    src.connect(ctx.destination);
    const startAt = Math.max(ctx.currentTime + PLAY_LEAD_S, this.nextPlayAt);
    src.start(startAt);
    this.nextPlayAt = startAt + audio.duration;
    this.scheduled.push(src);
    src.onended = () => {
      this.scheduled = this.scheduled.filter((s) => s !== src);
    };
    if (ctx.state === 'suspended') void ctx.resume();
  }

  private flushAudio(): void {
    for (const src of this.scheduled) {
      try { src.stop(); } catch { /* already ended */ }
    }
    this.scheduled = [];
    this.nextPlayAt = 0;
  }

  // ── teardown ─────────────────────────────────────────────────────────

  private finish(reason: string): void {
    if (useVoiceStore.getState().status === 'off') return;
    this.teardown();
    useVoiceStore.getState().reset();
    if (reason === 'draining') {
      useUIStore.getState().addToast({
        type: 'info',
        message: 'Voice off. The assistant is still finishing its work; results land in the chat.',
        duration: 4000,
      });
    } else if (reason === 'assistant' || reason === 'user' || reason === 'closed') {
      useUIStore.getState().addToast({ type: 'info', message: 'Voice conversation ended', duration: 2500 });
    }
  }

  private teardown(): void {
    this.startToken += 1;
    this.flushAudio();
    if (this.worklet) {
      this.worklet.port.onmessage = null;
      try { this.worklet.disconnect(); } catch { /* ignore */ }
      this.worklet = null;
    }
    if (this.micStream) {
      this.micStream.getTracks().forEach((t) => t.stop());
      this.micStream = null;
    }
    if (this.micCtx) {
      void this.micCtx.close().catch(() => undefined);
      this.micCtx = null;
    }
    if (this.playCtx) {
      void this.playCtx.close().catch(() => undefined);
      this.playCtx = null;
    }
    const ws = this.ws;
    this.ws = null;
    if (ws) {
      ws.onmessage = null;
      ws.onclose = null;
      try { ws.close(); } catch { /* ignore */ }
      this.socketSession.delete(ws);
      this.socketRuns.delete(ws);
    }
    this.frameBuf = [];
    this.frameLen = 0;
  }
}

export function floatToPcm16(samples: Float32Array): Int16Array {
  const out = new Int16Array(new ArrayBuffer(samples.length * 2));
  for (let i = 0; i < samples.length; i++) {
    const s = Math.max(-1, Math.min(1, samples[i]));
    out[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
  }
  return out;
}

export const voiceController = new VoiceController();
