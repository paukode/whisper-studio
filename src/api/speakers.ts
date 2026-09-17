import { del, get, post } from '@/api/client';

export interface VoiceprintList {
  names: string[];
  enabled: boolean;
}

/**
 * Turn a speaker rename into an enrollment.
 *
 * Naming "Speaker 2" as "Anna" is the only gesture the user makes; the
 * server stores the centroid of that cluster's embeddings so Anna is
 * recognised by name in later recordings. Fire-and-forget: the rename has
 * already been applied locally, and a failed enrollment only means the
 * next recording starts from "Speaker N" again.
 */
export function enrollSpeaker(sessionId: string, speaker: string, name: string): void {
  void post('/api/speakers/enroll', { session_id: sessionId, speaker, name }).catch(() => {
    /* best-effort */
  });
}

export function listVoiceprints(): Promise<VoiceprintList> {
  return get<VoiceprintList>('/api/speakers/voiceprints');
}

export function forgetVoiceprint(name: string): Promise<{ removed: boolean }> {
  return del<{ removed: boolean }>(`/api/speakers/voiceprints/${encodeURIComponent(name)}`);
}
