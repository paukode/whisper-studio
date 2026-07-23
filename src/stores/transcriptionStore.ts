import { createStore } from 'zustand/vanilla';
import type { TranscriptSegment } from '@/types/session';

export interface TranscriptionState {
  segments: TranscriptSegment[];
  speakerNames: Record<string, string>;
  // The Parakeet (streaming) backend's volatile in-progress draft: the live,
  // word-by-word transcript of the utterance currently being spoken, before it
  // settles into a committed segment at the next pause. Empty when there's no
  // draft (e.g. the Whisper backend, between utterances, or right after a
  // sentence finalizes). Never persisted — purely a live-display value.
  interimText: string;
  // Actions
  addSegment: (segment: TranscriptSegment) => void;
  appendSegmentText: (segmentId: string, extraText: string, chunkId?: number) => void;
  applySpeakerUpdates: (updates: { chunk_id: number; speaker: string }[]) => void;
  editSegmentText: (segmentId: string, newText: string) => void;
  renameSpeaker: (originalKey: string, newName: string) => void;
  clearSegments: () => void;
  loadSegments: (segments: TranscriptSegment[], speakers: Record<string, string>) => void;
  setInterimText: (text: string) => void;
}

/**
 * Per-session transcription store factory. Each live session owns one
 * instance via the runtime registry (sessionRuntimes.ts) — which is what
 * lets a recording keep writing into ITS session's transcript while the
 * user views another session.
 */
export const createTranscriptionStore = () => createStore<TranscriptionState>()((set) => ({
  segments: [],
  speakerNames: {},
  interimText: '',

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
  appendSegmentText: (segmentId: string, extraText: string, chunkId?: number) => {
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
        };
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
          return {
            ...seg,
            id: gi === 0 ? seg.id : crypto.randomUUID(),
            speaker: g.speaker,
            text: seg.text.slice(start, end).trim(),
            chunks: g.chunks.map((c) => ({ id: c.id, start: c.start - base })),
            // Reveal animation fields are stale after a split — drop them.
            receivedAt: undefined,
            freshIndex: undefined,
          };
        }).filter((s) => s.text.length > 0);
      }),
    }));
  },

  editSegmentText: (segmentId: string, newText: string) => {
    set((state) => ({
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
    });
  },

  loadSegments: (segments: TranscriptSegment[], speakers: Record<string, string>) => {
    set({
      segments,
      speakerNames: speakers,
    });
  },

  setInterimText: (text: string) => {
    set({ interimText: text });
  },
}));
