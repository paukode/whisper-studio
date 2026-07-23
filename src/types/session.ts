import type { ChatMessage } from './chat';

export interface Session {
  id: string;
  title: string;
  customTitle: boolean;
  generatedTitle: boolean;
  createdAt: string;           // ISO 8601
  updatedAt: string;           // ISO 8601
  segments: TranscriptSegment[];
  chatHistory: ChatMessage[];
  speakerNames: Record<string, string>;
}

export interface TranscriptSegment {
  id: string;
  speaker: string;             // Original speaker key (e.g., "SPEAKER_00")
  text: string;
  timestamp: number;
  edited: boolean;             // True if user manually edited text
  // Transient, live-only fields driving the word-by-word reveal animation.
  // Both are optional and never persisted: segments loaded from history have
  // them undefined, so historic transcripts render instantly without animating.
  receivedAt?: number;         // epoch-ms when the latest text arrived
  freshIndex?: number;         // char offset where the freshly-arrived tail begins
  // Live-only: which backend chunk_ids built this segment and where each
  // chunk's text starts within it. Lets a `speaker_update` (diarization
  // re-clustering correction) relabel or split a merged segment
  // retroactively. Meaningless for historic segments — chunk_ids are
  // scoped to one live recording.
  chunks?: { id: number; start: number }[];
}

export interface Speaker {
  key: string;                 // Original key from diarization
  displayName: string;         // User-assigned name or default
}
