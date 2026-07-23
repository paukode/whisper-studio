import React, { useCallback, useEffect, useRef } from 'react';
import { useChatInputMic } from '@/hooks/useChatInputMic';
import { resolveDictationFinal } from '@/components/chat/chatInputConstants';

interface UseDictationInputArgs {
  text: string;
  setText: (v: string) => void;
  textareaRef: React.RefObject<HTMLTextAreaElement | null>;
  sessionId: string | null;
}

/**
 * Mic dictation for the chat composer — reveals transcription word-by-word into
 * the input as the server streams it (same /ws Parakeet pipeline as the meeting
 * recorder, on its own WebSocket). Handles the interim/final draft settling and
 * the hands-free "voice submit" phrase.
 *
 * Owns the ref-trampolines the dictation hot path needs (`inputTextRef`,
 * `submitRef`, `micStopRef`) and returns the ones the composer must write:
 *
 *  - `inputTextRef` — the composer clears/reads it on every submit path.
 *  - `submitRef` — the composer points it at its live submit handler so a voice
 *    command can fire without the dictation callback depending on it.
 *  - `stopMic` — stable stop for the composer's submit paths.
 */
export function useDictationInput({
  text,
  setText,
  textareaRef,
  sessionId,
}: UseDictationInputArgs) {
  // Ref-trampolines so handleMicTranscript (deps []) can reach the live mic.stop
  // and the live submit handler without re-running on every render.
  const micStopRef = useRef<(() => void) | null>(null);
  const submitRef = useRef<((raw: string) => void | Promise<void>) | null>(null);
  // Mirror of the input text, kept in sync synchronously at every write site so
  // the dictation handler (deps []) can read the committed text without going
  // through a React state updater (which does not run synchronously under
  // automatic batching, so reading values mutated inside it is unreliable).
  const inputTextRef = useRef('');
  // Set once a voice command fires so the trailing final (and any late chunks)
  // for the same utterance cannot submit a second time. Reset when recording
  // starts again.
  const submittedRef = useRef(false);

  // Dictation interim tracking. `dictationBase` is the committed text the
  // current utterance is being appended onto; `dictationLive` is true while a
  // word-by-word interim draft is showing. On a final we replace the draft
  // with the settled sentence and advance the base.
  const dictationBaseRef = useRef('');
  const dictationLiveRef = useRef(false);

  /* Mic dictation — reveals transcription word-by-word into the input as the
   * server streams it (same /ws Parakeet pipeline as the meeting recorder,
   * but on its own WebSocket so the two coexist).
   *
   *   isFinal=false → live interim draft: replace the in-flight tail.
   *   isFinal=true  → settled sentence: commit it and advance the base.
   *
   * Everything here is computed synchronously from refs (never from values
   * mutated inside a setText updater, which does not run reliably mid-render
   * under batching). The submit command is evaluated on EVERY transcript,
   * interim or final, and fires on the FIRST one whose tail is a command: the
   * command text shows up in an interim ~0.3s into the trailing audio, whereas
   * waiting for the server to finalize the utterance adds ~0.4-1.9s (VAD
   * trailing-silence plus client chunk buffering). On a hit we submit the
   * accumulated message DIRECTLY via submitRef and mark submittedRef so the
   * trailing final cannot double-send. */
  const handleMicTranscript = useCallback(
    (transcript: string, isFinal: boolean) => {
      if (submittedRef.current) return;

      const caretToEnd = () =>
        requestAnimationFrame(() => {
          const el = textareaRef.current;
          if (el) {
            const len = el.value.length;
            el.setSelectionRange(len, len);
          }
        });
      const commit = (value: string) => {
        inputTextRef.current = value;
        setText(value);
      };

      const clean = transcript.trim();
      if (!isFinal && !clean) return;

      // The first interim of an utterance captures the committed text it builds on.
      if (!isFinal && !dictationLiveRef.current) {
        dictationBaseRef.current = inputTextRef.current;
        dictationLiveRef.current = true;
      }
      const base = dictationLiveRef.current ? dictationBaseRef.current : inputTextRef.current;
      const { text: next, submit } = resolveDictationFinal(base, clean);

      if (submit) {
        // Command spoken — fire now (on interim or final) for low latency.
        // submitMessage clears the input and stops the mic, closing out dictation.
        submittedRef.current = true;
        dictationLiveRef.current = false;
        dictationBaseRef.current = '';
        commit('');
        void submitRef.current?.(submit);
        return;
      }

      if (isFinal) {
        // Settle the sentence and advance the base for the next utterance.
        dictationLiveRef.current = false;
        dictationBaseRef.current = next;
      }
      commit(next);
      caretToEnd();
    },
    [setText, textareaRef],
  );

  const mic = useChatInputMic({
    onTranscript: handleMicTranscript,
    // Pass through the active session so reconnects share speaker
    // profiles via the backend's per-session bucket. Safe when null
    // (no session yet) — the hook falls back to the no-query URL.
    sessionId,
  });

  // Keep micStopRef pointed at the live mic.stop without re-running
  // handleMicTranscript every render.
  useEffect(() => {
    micStopRef.current = mic.stop;
  }, [mic.stop]);

  // Mirror the input text into a ref so the dictation handler can read the
  // committed value synchronously (covers typing and programmatic inserts; the
  // dictation handler also writes inputTextRef directly on its own hot path).
  useEffect(() => {
    inputTextRef.current = text;
  }, [text]);

  /** Click handler for the mic icon. Toggles the recording state and,
   *  when we're about to START recording (not stopping), pivots focus
   *  to the chat textarea so the user can see + edit dictation as it
   *  lands. Mirrors the focus/caret pattern used by the slash-command
   *  dispatcher and the whisper-chat-insert event. */
  const handleMicClick = useCallback(() => {
    const wasRecording = mic.isRecording || mic.isConnecting;
    mic.toggle();
    if (!wasRecording) {
      // Fresh dictation session — the next interim re-captures the base from
      // whatever's currently in the input, and a new utterance may submit again.
      dictationLiveRef.current = false;
      submittedRef.current = false;
      textareaRef.current?.focus();
      requestAnimationFrame(() => {
        const el = textareaRef.current;
        if (el) {
          const len = el.value.length;
          el.setSelectionRange(len, len);
        }
      });
    }
  }, [mic, textareaRef]);

  const stopMic = useCallback(() => micStopRef.current?.(), []);

  return { mic, handleMicClick, inputTextRef, submitRef, stopMic };
}
