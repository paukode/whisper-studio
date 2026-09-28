import { createStore } from 'zustand/vanilla';
import type { TranscriptSegment } from '@/types/session';

export interface TranscriptionState {
  segments: TranscriptSegment[];
  speakerNames: Record<string, string>;
  // The streaming backends' (Parakeet and Canary) volatile in-progress draft:
  // the live, word-by-word transcript of the utterance currently being spoken,
  // before it settles into a committed segment at the next pause. Empty when
  // there's no draft (e.g. the Whisper backend, between utterances, or right
  // after a sentence finalizes). Never persisted — purely a live-display value.
  interimText: string;
  // Chunk ids (and translation line ids) whose machine translation a manual
  // edit replaced. A late decode for one of them only clears its pending
  // marker, while chunks that reach the segment after the edit still get
  // their lines. Chunk ids are unique within one store (the recorder moves
  // its chunk base whenever the server's numbering restarts, between takes
  // or under a reconnect), so a flat list is enough. Live-only, never
  // persisted.
  supersededChunks: number[];
  // Actions
  addSegment: (segment: TranscriptSegment) => void;
  appendSegmentText: (
    segmentId: string,
    extraText: string,
    chunkId?: number,
    translating?: boolean,
  ) => void;
  applyTranslation: (chunkId: number, text: string, target?: string) => void;
  /** Drop the "Translating…" markers of chunks whose translation can no
   *  longer arrive (the socket that owed them is gone). */
  abandonPendingTranslations: (chunkIds: number[]) => void;
  /** A manual text edit invalidates the machine translation: park a pending
   *  marker (sentinel chunk -1) so the "Translating…" slot shows while the
   *  corrected text is re-translated, then commit the fresh line. Lines of
   *  chunks appended after the edit are kept. */
  beginSegmentRetranslation: (segmentId: string) => void;
  completeSegmentRetranslation: (segmentId: string, text: string, target?: string) => void;
  applySpeakerUpdates: (updates: { chunk_id: number; speaker: string }[]) => void;
  editSegmentText: (segmentId: string, newText: string) => void;
  renameSpeaker: (originalKey: string, newName: string) => void;
  clearSegments: () => void;
  loadSegments: (segments: TranscriptSegment[], speakers: Record<string, string>) => void;
  setInterimText: (text: string) => void;
}

/** Segments as a session stores them: without the live-only fields. Chunk
 *  ids belong to the recording that made them, so a later take must not
 *  match them, and a "Translating…" marker whose recording is gone would
 *  spin forever. Saves drop both, so a session loads exactly as it was
 *  stored; loads drop them too, for rows saved before that. */
export function persistedSegments(segments: TranscriptSegment[]): TranscriptSegment[] {
  return segments.map((seg) =>
    seg.pendingTranslations || seg.chunks
      ? { ...seg, chunks: undefined, pendingTranslations: undefined }
      : seg,
  );
}

/**
 * Per-session transcription store factory. Each live session owns one
 * instance via the runtime registry (sessionRuntimes.ts) — which is what
 * lets a recording keep writing into ITS session's transcript while the
 * user views another session.
 */
/** One past the highest chunk id the store still holds: the segments' chunks,
 *  pending markers and translation lines, and the ids an edit superseded
 *  (their lines are gone, but a new chunk under one of them would have its
 *  translation dropped). The edit sentinel -1 is ignored. A new recording
 *  take numbers its chunks from here, so its translations and speaker
 *  corrections can never match an earlier take's segments. */
export function nextChunkId(
  state: Pick<TranscriptionState, 'segments' | 'supersededChunks'>,
): number {
  let next = 0;
  const bump = (id: number) => {
    if (id + 1 > next) next = id + 1;
  };
  for (const seg of state.segments) {
    (seg.chunks ?? []).forEach((c) => bump(c.id));
    (seg.pendingTranslations ?? []).forEach(bump);
    (seg.translations ?? []).forEach((t) => bump(t.chunkId));
  }
  state.supersededChunks.forEach(bump);
  return next;
}

/** Everything an edit of `segmentId` makes stale: its chunks' machine lines,
 *  including the ones still decoding, and the lines it already shows. */
function supersede(state: TranscriptionState, segmentId: string): number[] {
  const seg = state.segments.find((s) => s.id === segmentId);
  if (!seg) return state.supersededChunks;
  const ids = [
    ...(seg.chunks ?? []).map((c) => c.id),
    ...(seg.translations ?? []).map((t) => t.chunkId),
  ].filter((id) => id >= 0 && !state.supersededChunks.includes(id));
  return ids.length > 0 ? [...state.supersededChunks, ...ids] : state.supersededChunks;
}

export const createTranscriptionStore = () => createStore<TranscriptionState>()((set) => ({
  segments: [],
  speakerNames: {},
  interimText: '',
  supersededChunks: [],

  addSegment: (segment: TranscriptSegment) => {
    set((state) => ({
      segments: [...state.segments, segment],
    }));
  },

  // Continuous speech from the same speaker grows an existing segment rather
  // than spawning a new one. This is automatic transcription growth — NOT a
  // user edit — so it must not set `edited`. It records `freshIndex` (where the
  // appended tail starts) and `receivedAt` so the UI can fade in only the new
  // words. The original `timestamp` is preserved (segments are stamped at their
  // start, not on every append).
  appendSegmentText: (segmentId: string, extraText: string, chunkId?: number, translating?: boolean) => {
    set((state) => ({
      segments: state.segments.map((seg) => {
        if (seg.id !== segmentId) return seg;
        const freshIndex = seg.text.length + 1; // +1 skips the joining space
        return {
          ...seg,
          text: `${seg.text} ${extraText}`,
          receivedAt: Date.now(),
          freshIndex,
          // Track where this chunk's text starts so a later speaker_update
          // can split the segment at exactly this boundary.
          chunks: chunkId !== undefined
            ? [...(seg.chunks ?? []), { id: chunkId, start: freshIndex }]
            : seg.chunks,
          pendingTranslations: translating && chunkId !== undefined
            ? [...(seg.pendingTranslations ?? []), chunkId]
            : seg.pendingTranslations,
        };
      }),
    }));
  },

  // A chunk's English translation arrived (or came back empty — which still
  // clears the pending placeholder). The chunk may live in any segment, and
  // segments merge/split, so locate it by membership rather than position.
  applyTranslation: (chunkId: number, text: string, target?: string) => {
    set((state) => {
      // A manual edit replaced this chunk's text, and the decode translated
      // the audio as first heard: it only clears the pending marker. A chunk
      // that joined the segment after the edit is not superseded and lands
      // normally, even while the edit's own re-translation is in flight.
      const superseded = state.supersededChunks.includes(chunkId);
      return {
        segments: state.segments.map((seg) => {
          const pending = seg.pendingTranslations ?? [];
          const owns =
            pending.includes(chunkId) || (seg.chunks?.some((c) => c.id === chunkId) ?? false);
          if (!owns) return seg;
          const translations =
            text && !superseded
              ? [...(seg.translations ?? []), { chunkId, text, target }].sort(
                  (a, b) => a.chunkId - b.chunkId,
                )
              : seg.translations;
          const remaining = pending.filter((id) => id !== chunkId);
          return {
            ...seg,
            translations,
            pendingTranslations: remaining.length > 0 ? remaining : undefined,
          };
        }),
      };
    });
  },

  abandonPendingTranslations: (chunkIds: number[]) => {
    if (chunkIds.length === 0) return;
    set((state) => ({
      segments: state.segments.map((seg) => {
        const pending = seg.pendingTranslations;
        if (!pending?.some((id) => chunkIds.includes(id))) return seg;
        const remaining = pending.filter((id) => !chunkIds.includes(id));
        return { ...seg, pendingTranslations: remaining.length > 0 ? remaining : undefined };
      }),
    }));
  },

  // Diarization re-clustering corrections: relabel (or split) segments
  // whose chunks were retroactively assigned to a different speaker. Only
  // live segments carry `chunks`; historic ones are left untouched.
  applySpeakerUpdates: (updates: { chunk_id: number; speaker: string }[]) => {
    const bySpeakerChunk = new Map(updates.map((u) => [u.chunk_id, u.speaker]));
    set((state) => ({
      segments: state.segments.flatMap((seg) => {
        if (!seg.chunks?.length) return [seg];
        const labels = seg.chunks.map((c) => bySpeakerChunk.get(c.id) ?? seg.speaker);
        if (labels.every((l) => l === seg.speaker)) return [seg];

        // Group consecutive chunks that share a (possibly corrected) label.
        const groups: { speaker: string; chunks: { id: number; start: number }[] }[] = [];
        seg.chunks.forEach((c, i) => {
          const last = groups[groups.length - 1];
          if (last && last.speaker === labels[i]) last.chunks.push(c);
          else groups.push({ speaker: labels[i], chunks: [c] });
        });

        if (groups.length === 1) {
          // Whole segment moved to another speaker — just relabel.
          return [{ ...seg, speaker: groups[0].speaker }];
        }
        // Mixed labels: split at the recorded chunk boundaries. The first
        // part keeps the segment id so renames/edits anchored to it survive.
        return groups.map((g, gi) => {
          const start = g.chunks[0].start;
          const end = gi + 1 < groups.length ? groups[gi + 1].chunks[0].start : seg.text.length;
          const base = g.chunks[0].start;
          // Translations (and pending markers) follow their source chunk
          // into whichever part it landed in. An edit's re-translation line
          // (sentinel -1) stays with the part that keeps the segment id.
          const ids = new Set(g.chunks.map((c) => c.id));
          if (gi === 0) ids.add(-1);
          const translations = seg.translations?.filter((t) => ids.has(t.chunkId));
          const pending = seg.pendingTranslations?.filter((id) => ids.has(id));
          return {
            ...seg,
            id: gi === 0 ? seg.id : crypto.randomUUID(),
            speaker: g.speaker,
            text: seg.text.slice(start, end).trim(),
            chunks: g.chunks.map((c) => ({ id: c.id, start: c.start - base })),
            translations: translations?.length ? translations : undefined,
            pendingTranslations: pending?.length ? pending : undefined,
            // Reveal animation fields are stale after a split — drop them.
            receivedAt: undefined,
            freshIndex: undefined,
          };
        }).filter((s) => s.text.length > 0);
      }),
    }));
  },

  beginSegmentRetranslation: (segmentId: string) => {
    set((state) => ({
      supersededChunks: supersede(state, segmentId),
      segments: state.segments.map((seg) =>
        seg.id === segmentId
          ? { ...seg, translations: undefined, pendingTranslations: [-1] }
          : seg,
      ),
    }));
  },

  // Lines of chunks that joined the segment after the edit survive; the
  // edit's own line (or, with empty text, nothing) replaces the rest.
  completeSegmentRetranslation: (segmentId: string, text: string, target?: string) => {
    set((state) => ({
      segments: state.segments.map((seg) => {
        if (seg.id !== segmentId) return seg;
        const later = (seg.translations ?? []).filter(
          (t) => t.chunkId !== -1 && !state.supersededChunks.includes(t.chunkId),
        );
        const translations = text ? [{ chunkId: -1, text, target }, ...later] : later;
        const pending = (seg.pendingTranslations ?? []).filter((id) => id !== -1);
        return {
          ...seg,
          translations: translations.length > 0 ? translations : undefined,
          pendingTranslations: pending.length > 0 ? pending : undefined,
        };
      }),
    }));
  },

  editSegmentText: (segmentId: string, newText: string) => {
    set((state) => ({
      // The corrected text outdates every machine line of the old text,
      // whether or not a re-translation follows.
      supersededChunks: supersede(state, segmentId),
      segments: state.segments.map((seg) =>
        seg.id === segmentId
          ? { ...seg, text: newText, edited: true }
          : seg,
      ),
    }));
  },

  renameSpeaker: (originalKey: string, newName: string) => {
    set((state) => ({
      speakerNames: {
        ...state.speakerNames,
        [originalKey]: newName,
      },
    }));
  },

  clearSegments: () => {
    set({
      segments: [],
      speakerNames: {},
      interimText: '',
      supersededChunks: [],
    });
  },

  loadSegments: (segments: TranscriptSegment[], speakers: Record<string, string>) => {
    set({
      segments: persistedSegments(segments),
      speakerNames: speakers,
      supersededChunks: [],
    });
  },

  setInterimText: (text: string) => {
    set({ interimText: text });
  },
}));
