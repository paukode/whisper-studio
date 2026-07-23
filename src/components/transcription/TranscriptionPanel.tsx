import { forwardRef, useCallback, useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { useActiveTranscriptionStore } from '@/stores/sessionRuntimes';
import { useSessionStore } from '@/stores/sessionStore';
import { useUIStore } from '@/stores/uiStore';
import { useSettingsStore } from '@/stores/settingsStore';
import { useChatStream } from '@/hooks/useChatStream';
import { put } from '@/api/client';
import { TranscriptSegment, getSpeakerClass } from './TranscriptSegment';

/** Custom event Header.tsx listens for to live-switch the ASR engine on the
 *  active recording's WebSocket (no reconnect). Carries { backend }. */
export const SET_BACKEND_EVENT = 'whisper-set-backend';
/** Custom event Header.tsx relays to the recording WebSocket as a
 *  participant-count hint for diarization. Carries { count } where 0 means
 *  automatic estimation. The user usually knows how many people are in the
 *  call; with the hint, speaker clustering cuts at exactly that count. */
export const SET_SPEAKERS_EVENT = 'whisper-set-speakers';
// NOTE: this panel only renders/edits transcript segments. Recording is owned
// entirely by the top-right Record button in Header.tsx, which captures mic +
// system audio directly. There is deliberately no second "Record" affordance
// here.

/** Custom event names used to talk to ChatInput / TranscriptSegment without
 * an extra global store. ChatInput listens for `whisper-chat-insert` and
 * inserts the text into its composer (without auto-sending). TranscriptSegment
 * listens for `whisper-segment-edit` to enter edit mode programmatically. */
export const CHAT_INSERT_EVENT = 'whisper-chat-insert';
export const SEGMENT_EDIT_EVENT = 'whisper-segment-edit';

interface MenuItem {
  action: string;
  icon: string;
  label: string;
  /** When true, the item renders greyed-out and clicking it is a no-op. */
  disabled?: boolean;
  /** Render as separator (the rest of the fields are ignored). */
  separator?: true;
}

interface ContextMenuState {
  x: number;
  y: number;
  text: string;
  /** id of the segment the selection started in, if we could find one. */
  segmentId: string | null;
  /** speaker key (e.g. "Speaker 1") for the segment we anchored to. */
  speaker: string | null;
}

const PROMPTS = {
  explain:   (t: string) => `Explain the following in simple terms, as if to someone unfamiliar with the topic:\n\n"${t}"`,
  translate: (t: string) => `Translate the following to English. If it's already in English, just confirm that and provide any clarification on meaning:\n\n"${t}"`,
  summarize: (t: string) => `Summarize the following concisely:\n\n"${t}"`,
  followup:  (t: string) => `Based on the following discussion excerpt, suggest 2-3 smart follow-up or clarifying questions:\n\n"${t}"`,
  define:    (t: string) => `Identify and define any technical terms, acronyms, or jargon in the following. Be brief:\n\n"${t}"`,
  search:    (t: string) => `Use the ws_grep tool to search the connected workspace for the literal phrase: "${t}". Show the file path and line for each hit.`,
} as const;

function TranscriptContextMenu({ state, onClose }: { state: ContextMenuState; onClose: () => void }) {
  const { send } = useChatStream();
  const wsConnected = useUIStore((s) => s.wsConnected);
  const speakerNames = useActiveTranscriptionStore((s) => s.speakerNames);
  const menuRef = useRef<HTMLDivElement>(null);

  // Single dismiss path — we hold one stable reference for both add and
  // remove so neither listener leaks across menu opens.
  useEffect(() => {
    // Only LEFT-button mousedowns count as "click outside to dismiss".
    // Right-button (button === 2) is the user opening another contextmenu;
    // closing on it would race with the new menu state and feel broken.
    // Middle-button (button === 1) is similarly never a dismissal.
    const onDocMouseDown = (e: MouseEvent) => {
      if (e.button !== 0) return;
      if (menuRef.current && menuRef.current.contains(e.target as Node)) return;
      onClose();
    };
    const onDocKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') { e.preventDefault(); onClose(); }
    };
    // Defer attaching the mousedown listener until after the current event
    // loop tick. Without this, the same mousedown that opened the menu
    // (via the parent's onContextMenu → setCtxMenu) would itself match this
    // listener and immediately dismiss the menu — there's a brief window in
    // which React mounts the menu but the user's mouseup hasn't happened yet.
    let raf = 0;
    raf = requestAnimationFrame(() => {
      document.addEventListener('mousedown', onDocMouseDown);
    });
    document.addEventListener('keydown', onDocKey);
    return () => {
      cancelAnimationFrame(raf);
      document.removeEventListener('mousedown', onDocMouseDown);
      document.removeEventListener('keydown', onDocKey);
    };
  }, [onClose]);

  // Build the menu fresh each render so disabled state (workspace connection,
  // segment availability) reflects the latest UI state.
  const items: MenuItem[] = [
    { action: 'copy',           icon: '📋', label: 'Copy' },
    { action: 'copy_speaker',   icon: '🗣️', label: 'Copy with speaker prefix' },
    { action: 'quote',          icon: '💬', label: 'Quote in chat input' },
    { action: 'edit',           icon: '✏️', label: 'Edit segment text', disabled: !state.segmentId },
    { action: 'search',         icon: '🔍', label: 'Search workspace for this', disabled: !wsConnected },
    { action: '__sep1',         icon: '',   label: '', separator: true },
    { action: 'explain',        icon: '💡', label: 'Explain' },
    { action: 'translate',      icon: '🌐', label: 'Translate to English' },
    { action: 'summarize',      icon: '📝', label: 'Summarize' },
    { action: 'followup',       icon: '❓', label: 'Suggest follow-up questions' },
    { action: 'define',         icon: '📚', label: 'Define key terms' },
  ];

  const handleAction = useCallback(
    (action: string) => {
      onClose();
      const text = state.text;
      switch (action) {
        case 'copy':
          void navigator.clipboard.writeText(text);
          return;
        case 'copy_speaker': {
          const label = state.speaker ? (speakerNames[state.speaker] ?? state.speaker) : 'Speaker';
          void navigator.clipboard.writeText(`${label}: ${text}`);
          return;
        }
        case 'quote': {
          const label = state.speaker ? (speakerNames[state.speaker] ?? state.speaker) : null;
          const quoted = label
            ? `> **${label}:** ${text}\n\n`
            : `> ${text}\n\n`;
          window.dispatchEvent(new CustomEvent(CHAT_INSERT_EVENT, { detail: { text: quoted } }));
          return;
        }
        case 'edit':
          if (state.segmentId) {
            window.dispatchEvent(
              new CustomEvent(SEGMENT_EDIT_EVENT, { detail: { segmentId: state.segmentId } }),
            );
          }
          return;
        case 'search':
          if (wsConnected) void send(PROMPTS.search(text));
          return;
        case 'explain':   void send(PROMPTS.explain(text));   return;
        case 'translate': void send(PROMPTS.translate(text)); return;
        case 'summarize': void send(PROMPTS.summarize(text)); return;
        case 'followup':  void send(PROMPTS.followup(text));  return;
        case 'define':    void send(PROMPTS.define(text));    return;
      }
    },
    [state.text, state.segmentId, state.speaker, speakerNames, wsConnected, onClose, send],
  );

  return createPortal(
    <div
      ref={menuRef}
      className="transcript-ctx-menu"
      style={{ left: state.x, top: state.y }}
    >
      {items.map((item) => {
        if (item.separator) {
          return <div key={item.action} className="ctx-separator" role="separator" />;
        }
        return (
          <div
            key={item.action}
            className={`ctx-item${item.disabled ? ' ctx-item-disabled' : ''}`}
            role="menuitem"
            aria-disabled={item.disabled || undefined}
            onMouseDown={(e) => {
              e.preventDefault();
              if (item.disabled) return;
              handleAction(item.action);
            }}
          >
            <span className="ctx-icon">{item.icon}</span> {item.label}
          </div>
        );
      })}
    </div>,
    document.body,
  );
}

export interface TranscriptionPanelProps {
  hidden?: boolean;
}

/**
 * Transcript panel matching the vanilla HTML structure exactly.
 *
 * Structure:
 *   div.panel#transcriptPanel
 *     div.panel-header
 *       h2 (icon + "Transcript")
 *       div.panel-header-actions (Export + Clear buttons)
 *     div.panel-body.transcript-area#transcriptArea
 *       div.transcript-empty#transcriptEmpty (empty state)
 *       div.transcript-segments#transcriptSegments
 */
export const TranscriptionPanel = forwardRef<HTMLDivElement, TranscriptionPanelProps>(
  ({ hidden }, ref) => {
  const segments = useActiveTranscriptionStore((s) => s.segments);
  const interimText = useActiveTranscriptionStore((s) => s.interimText);
  const speakerNames = useActiveTranscriptionStore((s) => s.speakerNames);
  const editSegmentText = useActiveTranscriptionStore((s) => s.editSegmentText);
  const renameSpeaker = useActiveTranscriptionStore((s) => s.renameSpeaker);
  const clearSegments = useActiveTranscriptionStore((s) => s.clearSegments);
  const currentSessionId = useSessionStore((s) => s.currentSessionId);
  const transcriptionBackend = useSettingsStore((s) => s.config.transcriptionBackend);

  // Audio capture and the transcription socket are owned by Header.tsx — wiring
  // a second pipeline here would spawn a duplicate WebSocket and a confusing
  // second Record button.

  const transcriptAreaRef = useRef<HTMLDivElement>(null);
  const [ctxMenu, setCtxMenu] = useState<ContextMenuState | null>(null);

  // One-time teaching tile: the transcript's power features (quote in chat,
  // explain, translate, search workspace, …) hide behind select+right-click
  // and nothing else in the UI reveals they exist. Dismissal is persisted.
  const [tipDismissed, setTipDismissed] = useState(() => {
    try { return localStorage.getItem('whisper_transcript_tip_dismissed') === 'true'; } catch { return false; }
  });
  const dismissTip = useCallback(() => {
    setTipDismissed(true);
    try { localStorage.setItem('whisper_transcript_tip_dismissed', 'true'); } catch { /* private mode */ }
  }, []);

  useEffect(() => {
    if (transcriptAreaRef.current) {
      transcriptAreaRef.current.scrollTop = transcriptAreaRef.current.scrollHeight;
    }
  }, [segments, interimText]);

  const handleTextEdit = useCallback(
    (segmentId: string, newText: string) => {
      editSegmentText(segmentId, newText);
    },
    [editSegmentText],
  );

  const handleSpeakerRename = useCallback(
    (originalKey: string, newName: string) => {
      renameSpeaker(originalKey, newName);
    },
    [renameSpeaker],
  );

  const getSpeakerDisplayName = useCallback(
    (speakerKey: string): string => {
      return speakerNames[speakerKey] ?? speakerKey;
    },
    [speakerNames],
  );

  const handleExport = useCallback(() => {
    if (segments.length === 0) return;
    const text = segments
      .map((s) => `[${getSpeakerDisplayName(s.speaker)}] ${s.text}`)
      .join('\n');
    const blob = new Blob([text], { type: 'text/plain' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `transcript-${currentSessionId ?? 'export'}.txt`;
    a.click();
    URL.revokeObjectURL(url);
  }, [segments, getSpeakerDisplayName, currentSessionId]);

  const handleClear = useCallback(() => {
    clearSegments();
  }, [clearSegments]);

  const handleBackendChange = useCallback((backend: string) => {
    // 1) Reflect immediately in the shared config (so the Settings dropdown
    //    and a later reconnect agree). 2) Persist so the choice sticks for
    //    the next recording. 3) Live-switch the active stream — Header.tsx
    //    relays this to the open WebSocket; it's a no-op when not recording.
    useSettingsStore.getState().updateConfig({ transcriptionBackend: backend });
    void put('/api/config', { transcription_backend: backend });
    window.dispatchEvent(new CustomEvent(SET_BACKEND_EVENT, { detail: { backend } }));
  }, []);

  // Diarization participant-count hint. 0 = auto. Header.tsx keeps the
  // latest value and sends it on the recording WebSocket (on change while
  // recording, and at connect for hints set beforehand).
  const [speakerCount, setSpeakerCount] = useState(0);
  const handleSpeakerCountChange = useCallback((count: number) => {
    setSpeakerCount(count);
    window.dispatchEvent(new CustomEvent(SET_SPEAKERS_EVENT, { detail: { count } }));
  }, []);

  const hasSegments = segments.length > 0;
  const hasInterim = interimText.trim().length > 0;
  const hasContent = hasSegments || hasInterim;

  // The live row commits into the current turn, so it should look like the
  // segment it will become: same speaker key, label and rail colour as the
  // last committed segment. Before any speech lands we fall back to Speaker 1.
  const lastSegment = hasSegments ? segments[segments.length - 1] : null;
  const liveSpeakerKey = lastSegment?.speaker ?? 'Speaker 1';
  const liveSpeakerName = getSpeakerDisplayName(liveSpeakerKey);

  return (
    <div ref={ref} className={`panel${hidden ? ' hidden' : ''}`} id="transcriptPanel">
      <div className="panel-header">
        <h2>
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
            <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
            <polyline points="14 2 14 8 20 8"/>
            <line x1="16" y1="13" x2="8" y2="13"/>
            <line x1="16" y1="17" x2="8" y2="17"/>
          </svg>
          Transcript
        </h2>
        <div className="panel-header-actions">
          <select
            className="transcript-engine-select"
            id="transcriptEngineSelect"
            title="Transcription engine, switch live, even mid-recording"
            aria-label="Transcription engine"
            value={transcriptionBackend}
            onChange={(e) => handleBackendChange(e.target.value)}
          >
            <option value="whisper">Whisper</option>
            <option value="streaming">Parakeet (live)</option>
          </select>
          <select
            className="transcript-engine-select"
            id="transcriptSpeakersSelect"
            title="How many people are in this meeting. Setting it makes speaker labels far more accurate; Auto estimates from the audio."
            aria-label="Number of speakers"
            value={speakerCount}
            onChange={(e) => handleSpeakerCountChange(Number(e.target.value))}
          >
            <option value={0}>Speakers: auto</option>
            {[1, 2, 3, 4, 5, 6, 7, 8].map((n) => (
              <option key={n} value={n}>{n} speaker{n > 1 ? 's' : ''}</option>
            ))}
          </select>
          <button
            className="btn btn-sm"
            id="downloadBtn"
            disabled={!hasSegments}
            onClick={handleExport}
          >
            Export
          </button>
          <button
            className="btn btn-sm"
            id="transcriptClearBtn"
            onClick={handleClear}
          >
            Clear
          </button>
        </div>
      </div>
      {/* Pinned ABOVE the scroll area — the transcript auto-scrolls to the
       *  bottom on every new segment, which would push an in-flow tile out
       *  of view immediately. */}
      {!tipDismissed && (
        <div className="transcript-tip" role="note">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <path d="M12 2l2.4 5.3L20 8.5l-4 4.1.9 5.9L12 15.8l-4.9 2.7.9-5.9-4-4.1 5.6-1.2z" />
          </svg>
          <div className="transcript-tip-text">
            <strong>Every line here is interactive</strong>
            <span>
              Select any transcript text and right-click: quote it in chat, edit it,
              search your workspace, or have the assistant explain, translate,
              summarize, define terms, or suggest follow-up questions.
            </span>
          </div>
          <button
            type="button"
            className="transcript-tip-close"
            onClick={dismissTip}
            aria-label="Dismiss tip"
          >
            ×
          </button>
        </div>
      )}
      <div
        ref={transcriptAreaRef}
        className="panel-body transcript-area"
        id="transcriptArea"
        onContextMenu={(e) => {
          const sel = window.getSelection();
          const selected = sel?.toString().trim();
          if (!selected) return;
          e.preventDefault();
          // Walk up from the selection's anchor node to find which transcript
          // segment it lives in, so menu actions like "Edit segment text" or
          // "Copy with speaker prefix" can target the right segment.
          let segmentId: string | null = null;
          let speaker: string | null = null;
          const anchor = sel?.anchorNode ?? null;
          const startEl = anchor instanceof Element ? anchor : anchor?.parentElement ?? null;
          const segEl = startEl?.closest('.transcript-segment[data-segment-id]') as HTMLElement | null;
          if (segEl) {
            segmentId = segEl.getAttribute('data-segment-id');
            const segData = segments.find((s) => s.id === segmentId);
            speaker = segData?.speaker ?? null;
          }
          setCtxMenu({ x: e.clientX, y: e.clientY, text: selected, segmentId, speaker });
        }}
      >
        {!hasContent && (
          <div className="transcript-empty" id="transcriptEmpty">
            <div className="empty-state">
              <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="var(--text-muted)" strokeWidth="1.5" strokeLinecap="round">
                <path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/>
                <path d="M19 10v2a7 7 0 0 1-14 0v-2"/>
                <line x1="12" y1="19" x2="12" y2="23"/>
                <line x1="8" y1="23" x2="16" y2="23"/>
              </svg>
              <p>Press <strong>Record</strong> to start transcribing</p>
              <span className="empty-hint">Captures microphone audio</span>
            </div>
          </div>
        )}
        <div className="transcript-segments" id="transcriptSegments">
          {segments.map((segment) => (
            <TranscriptSegment
              key={segment.id}
              segment={segment}
              speakerName={getSpeakerDisplayName(segment.speaker)}
              onTextEdit={handleTextEdit}
              onSpeakerRename={handleSpeakerRename}
            />
          ))}
          {/* Live, word-by-word draft from the Parakeet streaming backend.
              Rendered as a real transcript row so it lines up under the speaker
              boxes exactly like committed segments — same timestamp/speaker/text
              grid. It updates in place as more speech arrives and is removed the
              instant the sentence finalizes into a committed segment above. */}
          {hasInterim && (
            <div
              className="transcript-segment transcript-segment-live"
              id="transcriptInterim"
              aria-live="polite"
            >
              {/* Empty slot keeps the speaker rail aligned under committed
                  rows, which always render a 58px timestamp. */}
              <span className="segment-timestamp" aria-hidden="true" />
              <div
                className={`segment-speaker ${getSpeakerClass(liveSpeakerKey)}`}
                data-speaker={liveSpeakerKey}
              >
                {liveSpeakerName}
              </div>
              <div className="segment-text">{interimText}</div>
            </div>
          )}
        </div>
      </div>
      {ctxMenu && <TranscriptContextMenu state={ctxMenu} onClose={() => setCtxMenu(null)} />}
    </div>
  );
});

TranscriptionPanel.displayName = 'TranscriptionPanel';
