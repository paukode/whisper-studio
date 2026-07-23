import React, { useCallback, useEffect, useRef, useState } from 'react';
import type { TranscriptSegment as TranscriptSegmentType } from '@/types/session';
import { formatSegmentTimestamp } from '@/utils/formatTimestamp';

/** Mirrors `SEGMENT_EDIT_EVENT` exported from TranscriptionPanel.tsx. We
 * duplicate the constant here to avoid an import cycle (the panel imports
 * this component). Both names must stay in sync. */
const SEGMENT_EDIT_EVENT = 'whisper-segment-edit';

export interface TranscriptSegmentProps {
  segment: TranscriptSegmentType;
  speakerName: string;
  onTextEdit: (segmentId: string, newText: string) => void;
  onSpeakerRename: (originalKey: string, newName: string) => void;
}

/**
 * Map a speaker string like "Speaker 2" or "SPEAKER_01" to a CSS class
 * `speaker-1` through `speaker-6`, matching the vanilla stylesheet.
 *
 * Exported so the live (interim) row in TranscriptionPanel can colour its
 * speaker rail to match the segment it will commit into.
 */
export function getSpeakerClass(speaker: string): string {
  const num = speaker.replace(/\D/g, '') || '1';
  return `speaker-${Math.min(parseInt(num, 10) || 1, 6)}`;
}

/**
 * Render segment text with a word-by-word fade-in for freshly-arrived speech.
 *
 * Live segments carry `receivedAt` (when the latest text landed) and
 * `freshIndex` (where the new tail begins). Only the tail beyond `freshIndex`
 * animates — the already-settled head renders as a plain string, so earlier
 * words never re-animate when a segment grows. Segments without `receivedAt`
 * (history, user edits) render instantly.
 */
const REVEAL_WINDOW_MS = 4000;
const WORD_STAGGER_MS = 55;

function renderSegmentText(segment: TranscriptSegmentType): React.ReactNode {
  const { text, receivedAt, freshIndex } = segment;
  const animate =
    typeof receivedAt === 'number' &&
    typeof freshIndex === 'number' &&
    freshIndex < text.length &&
    Date.now() - receivedAt < REVEAL_WINDOW_MS;

  if (!animate) return text;

  const head = text.slice(0, freshIndex ?? 0);
  const words = text.slice(freshIndex ?? 0).split(' ');
  return (
    <>
      {head}
      {words.map((word, i) => (
        <span
          // receivedAt in the key makes each append batch mount fresh spans,
          // so the CSS animation replays for the new words only.
          key={`${receivedAt}-${i}`}
          className="segment-word-reveal"
          style={{ animationDelay: `${i * WORD_STAGGER_MS}ms` }}
        >
          {i < words.length - 1 ? `${word} ` : word}
        </span>
      ))}
    </>
  );
}

/**
 * Renders a single transcript segment matching the vanilla HTML structure:
 *
 *   div.transcript-segment
 *     div.segment-speaker.speaker-N[data-speaker]
 *     div.segment-text
 *
 * Speaker label is click-to-rename inline.
 * Text is click-to-edit inline.
 */
export const TranscriptSegment: React.FC<TranscriptSegmentProps> = ({
  segment,
  speakerName,
  onTextEdit,
  onSpeakerRename,
}) => {
  const [isEditingSpeaker, setIsEditingSpeaker] = useState(false);
  const [speakerEditValue, setSpeakerEditValue] = useState(speakerName);
  const speakerInputRef = useRef<HTMLInputElement>(null);

  const [isEditingText, setIsEditingText] = useState(false);
  const [textEditValue, setTextEditValue] = useState(segment.text);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const handleSpeakerClick = useCallback(() => {
    setSpeakerEditValue(speakerName);
    setIsEditingSpeaker(true);
    requestAnimationFrame(() => {
      speakerInputRef.current?.focus();
      speakerInputRef.current?.select();
    });
  }, [speakerName]);

  const commitSpeakerEdit = useCallback(() => {
    const trimmed = speakerEditValue.trim();
    setIsEditingSpeaker(false);
    if (trimmed && trimmed !== speakerName) {
      onSpeakerRename(segment.speaker, trimmed);
    }
  }, [speakerEditValue, speakerName, segment.speaker, onSpeakerRename]);

  const handleSpeakerKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLInputElement>) => {
      if (e.key === 'Enter') {
        commitSpeakerEdit();
      } else if (e.key === 'Escape') {
        e.preventDefault();
        setIsEditingSpeaker(false);
      }
    },
    [commitSpeakerEdit],
  );

  // Text editing handlers
  const handleTextClick = useCallback(() => {
    setTextEditValue(segment.text);
    setIsEditingText(true);
    requestAnimationFrame(() => {
      textareaRef.current?.focus();
    });
  }, [segment.text]);

  // Listen for the right-click menu's "Edit segment text" action so it can
  // open this segment's textarea remotely. The detail.segmentId must match
  // this segment's id; everything else is ignored.
  useEffect(() => {
    const onEditEvent = (e: Event) => {
      const detail = (e as CustomEvent<{ segmentId?: string }>).detail;
      if (detail?.segmentId === segment.id) {
        handleTextClick();
      }
    };
    window.addEventListener(SEGMENT_EDIT_EVENT, onEditEvent);
    return () => window.removeEventListener(SEGMENT_EDIT_EVENT, onEditEvent);
  }, [segment.id, handleTextClick]);

  const commitTextEdit = useCallback(() => {
    const trimmed = textEditValue.trim();
    setIsEditingText(false);
    if (trimmed && trimmed !== segment.text) {
      onTextEdit(segment.id, trimmed);
    }
  }, [textEditValue, segment.text, segment.id, onTextEdit]);

  const handleTextKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        commitTextEdit();
      } else if (e.key === 'Escape') {
        e.preventDefault();
        setIsEditingText(false);
      }
    },
    [commitTextEdit],
  );

  return (
    <div className="transcript-segment" data-segment-id={segment.id}>
      {segment.timestamp > 0 && (
        <span className="segment-timestamp">{formatSegmentTimestamp(segment.timestamp)}</span>
      )}
      {isEditingSpeaker ? (
        <input
          ref={speakerInputRef}
          className={`segment-speaker ${getSpeakerClass(segment.speaker)}`}
          type="text"
          value={speakerEditValue}
          onChange={(e) => setSpeakerEditValue(e.target.value)}
          onBlur={commitSpeakerEdit}
          onKeyDown={handleSpeakerKeyDown}
          aria-label={`Rename speaker ${speakerName}`}
        />
      ) : (
        <div
          className={`segment-speaker ${getSpeakerClass(segment.speaker)}`}
          data-speaker={segment.speaker}
          role="button"
          tabIndex={0}
          onClick={handleSpeakerClick}
          onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); handleSpeakerClick(); } }}
          style={{ cursor: 'pointer' }}
          title="Click to rename"
        >
          {speakerName}
        </div>
      )}
      {isEditingText ? (
        <textarea
          ref={textareaRef}
          className="segment-text"
          value={textEditValue}
          onChange={(e) => setTextEditValue(e.target.value)}
          onBlur={commitTextEdit}
          onKeyDown={handleTextKeyDown}
          aria-label="Edit segment text"
          style={{ width: '100%', resize: 'vertical' }}
        />
      ) : (
        <div
          className="segment-text"
          role="button"
          tabIndex={0}
          // Double-click (not single-click) to enter edit mode so drag-to-
          // select keeps working without the segment turning into a textarea.
          // Single-click (Enter/Space on keyboard) still works for users
          // who can't double-click easily.
          onDoubleClick={handleTextClick}
          onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); handleTextClick(); } }}
          style={{ cursor: 'text' }}
          title="Double-click to edit. Right-click for more options."
        >
          {renderSegmentText(segment)}
        </div>
      )}
    </div>
  );
};
