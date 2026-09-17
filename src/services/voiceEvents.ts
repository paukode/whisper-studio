import type { ChatMessage, TeamReportData, ToolUseEvent } from '@/types/chat';
import type { VoiceServerEvent } from '@/types/schemas/voice.schema';
import { stepBelongsTo, useVoiceStore, type VoiceStep } from '@/stores/voiceStore';

/**
 * Pure application of one server event to the voice store plus a few side
 * effects the controller injects (committing chat messages, audio playback,
 * app actions). Kept free of sockets and audio so it is unit-testable.
 */
export interface VoiceEventDeps {
  /** Persist a user turn (spoken or typed) into the chat. */
  commitUser: (text: string, typed: boolean) => void;
  /** Persist an assistant message: what Sonic actually said (spoken=true) or
   *  the delegated assistant's written answer shown on screen (spoken=false),
   *  with its tool activity and, when agents ran, their reports. */
  commitAssistant: (
    text: string,
    tools: ToolUseEvent[],
    spoken: boolean,
    teamReports?: Record<string, TeamReportData>,
  ) => void;
  /** The user hung up while delegated runs were still going; the socket stays
   *  open for their events until ended. */
  onDraining: (runs: { run_id: string; request: string }[]) => void;
  /** Drop any queued playback (barge-in). */
  flushAudio: () => void;
  /** An app control Sonic asked for (recording start/stop). */
  onClientAction: (action: string, value: string) => void;
  /** The conversation is over; tear everything down. */
  onEnded: (reason: string) => void;
  toast: (type: 'error' | 'info' | 'warning', message: string) => void;
}

const PREVIEW_CHARS = 160;

function preview(value: unknown): string {
  const text = typeof value === 'string' ? value : JSON.stringify(value ?? '');
  const flat = text.replace(/\s+/g, ' ').trim();
  return flat.length > PREVIEW_CHARS ? `${flat.slice(0, PREVIEW_CHARS - 1)}…` : flat;
}

/** Steps for the utterance in progress, as the persisted trace shape the
 *  existing ChatMessage activity renderer understands. */
export function stepsToToolUse(steps: VoiceStep[]): ToolUseEvent[] {
  return steps.map((s) => ({
    toolId: s.id,
    toolName: s.name,
    input: s.input ?? {},
    result: s.output || s.detail || undefined,
    status: s.status === 'running' ? 'running' : s.status === 'error' ? 'error' : 'complete',
  }));
}

/** Sonic hands over speech in pieces: several USER transcripts per turn, and a
 *  FINAL assistant block per sentence. Consecutive spoken pieces of the same
 *  role within this window are one turn, shown as one bubble. */
export const SPOKEN_MERGE_WINDOW_MS = 180_000;

/** The delegated assistant's own calls are the bubble, not its activity. */
const DELEGATE_TOOLS = new Set(['ask_assistant', 'resolve_request']);

/** Merge a spoken USER message into the previous bubble when it continues the
 *  same spoken turn (Sonic transcribes speech in several pieces); returns the
 *  new message list, or null when it must be appended. Assistant utterances
 *  are committed whole by the server, one bubble each, so they never merge. */
export function mergeSpoken(
  messages: ChatMessage[],
  incoming: ChatMessage,
  now: number = Date.now(),
): ChatMessage[] | null {
  const last = messages[messages.length - 1];
  if (!last || !incoming.spoken || !last.spoken || last.role !== incoming.role) return null;
  if (incoming.role !== 'user') return null;
  const age = now - Date.parse(last.timestamp);
  if (!Number.isFinite(age) || age < 0 || age > SPOKEN_MERGE_WINDOW_MS) return null;
  const merged: ChatMessage = { ...last, content: `${last.content} ${incoming.content}`.trim() };
  const tools = [...(last.toolUse ?? []), ...(incoming.toolUse ?? [])];
  if (tools.length > 0) {
    const seen = new Set<string>();
    merged.toolUse = tools.filter((t) => (seen.has(t.toolId) ? false : (seen.add(t.toolId), true)));
  }
  return [...messages.slice(0, -1), merged];
}

export function applyVoiceEvent(ev: VoiceServerEvent, deps: VoiceEventDeps): void {
  const store = useVoiceStore.getState();
  switch (ev.type) {
    case 'ready':
      store.setReady(ev.model_id, ev.voice_id);
      return;
    case 'state': {
      const current = useVoiceStore.getState().status;
      if (current === 'off' || current === 'ending') return;
      store.setStatus(ev.state);
      return;
    }
    case 'user_transcript':
      deps.commitUser(ev.text, ev.typed === true);
      return;
    case 'assistant_text':
      if (ev.final) {
        // One final per utterance, sent once its speech has ended: the live
        // preview becomes this committed bubble in place. A spoken bubble is
        // just what Sonic said; tool activity belongs to the delegated
        // assistant's written answer, which commits when its result arrives.
        deps.commitAssistant(ev.text, [], true);
        store.clearLive(ev.utterance_id);
      } else {
        store.appendLiveText(ev.text, ev.utterance_id);
      }
      return;
    case 'interrupted':
      deps.flushAudio();
      return;
    case 'tool_call':
      store.upsertStep({
        id: ev.tool_use_id,
        name: ev.name,
        status: 'running',
        detail: preview(ev.input),
        input: ev.input,
        source: 'sonic',
      });
      store.adjustPending(1);
      return;
    case 'assistant_step':
      if (ev.status === 'running') {
        store.upsertStep({
          id: `step-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
          name: ev.name,
          status: 'running',
          detail: ev.detail,
          source: 'assistant',
          runId: ev.run_id,
          ...(ev.input ? { input: ev.input } : {}),
        });
      } else {
        store.finishStep(ev.name, ev.status, ev.detail, ev.run_id, ev.output);
      }
      return;
    case 'team_progress':
      // An agent spawned by a delegated run: same cards as typed chat.
      store.foldTeamEvent(ev.run_id, ev.event);
      return;
    case 'assistant_answer': {
      // The delegated assistant's written answer goes on screen as its own
      // bubble, carrying the steps and agent reports of that run and only
      // those: other runs may still be going, and their steps stay on screen
      // until they answer. Sonic's spoken summary follows as a separate
      // spoken bubble.
      const inner = useVoiceStore.getState().steps.filter((st) => stepBelongsTo(st, ev.run_id));
      const reports = store.takeTeamReports(ev.run_id);
      if (ev.status === 'stopped') {
        // Cancelled by the user: keep the trace like a stopped chat turn, with
        // the unfinished steps marked stopped rather than spinning forever.
        // A running step's "result" is only its input preview; the trace says
        // it was stopped.
        const tools = stepsToToolUse(inner).map((t) =>
          t.status === 'running' ? { ...t, status: 'stopped' as const, result: '[Stopped by user]' } : t,
        );
        if (tools.length > 0 || reports) {
          if (reports) deps.commitAssistant('*(Stopped)*', tools, false, reports);
          else deps.commitAssistant('*(Stopped)*', tools, false);
        }
      } else if (!ev.output.startsWith('Error:')) {
        if (reports) deps.commitAssistant(ev.output, stepsToToolUse(inner), false, reports);
        else deps.commitAssistant(ev.output, stepsToToolUse(inner), false);
      }
      store.removeRun(ev.run_id);
      return;
    }
    case 'draining':
      deps.onDraining(ev.runs);
      return;
    case 'tool_result':
      store.adjustPending(-1);
      if (DELEGATE_TOOLS.has(ev.name)) {
        // What ask_assistant / resolve_request return is guidance for Sonic:
        // the answer itself arrives as assistant_answer (now or, for a run
        // that continues in the background, minutes later), a pause shows as
        // the request card, and a tool-misuse error ("Error: there is no
        // pending request") is never something to show. The run's steps stay
        // on screen until its answer lands; only Sonic's own call is settled.
        store.dropStep(ev.tool_use_id);
        return;
      }
      store.upsertStep({
        id: ev.tool_use_id,
        name: ev.name,
        status: ev.status === 'error' ? 'error' : 'ok',
        detail: ev.output,
        source: 'sonic',
      });
      return;
    case 'assistant_request':
      store.setPendingRequest({
        kind: ev.kind,
        runId: ev.run_id,
        toolUseId: ev.tool_use_id,
        action: ev.action,
        category: ev.category,
        summary: ev.summary,
        question: ev.question,
        options: ev.options,
        riskHint: ev.risk_hint ?? null,
        detail: ev.detail,
      });
      return;
    case 'assistant_request_resolved':
      store.setPendingRequest(null);
      return;
    case 'client_action':
      deps.onClientAction(ev.action, ev.value);
      return;
    case 'usage':
      store.setUsage({
        inputSpeech: ev.input_speech,
        inputText: ev.input_text,
        outputSpeech: ev.output_speech,
        outputText: ev.output_text,
        totalTokens: ev.total_tokens,
      });
      return;
    case 'renewing':
      return;
    case 'renewed':
      store.markStreamStarted();
      return;
    case 'error':
      store.setError(ev.message);
      deps.toast('error', ev.message);
      return;
    case 'ended':
      deps.onEnded(ev.reason);
      return;
    case 'pong':
      return;
  }
}
