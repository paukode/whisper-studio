import { post } from '@/api/client';

/**
 * Turn a speaker rename into an enrollment.
 *
 * Only does anything when the `speaker_voiceprints` flag is on, which it is
 * not by default: the server drops the call and the name stays local to this
 * recording. With the flag on, naming "Speaker 2" as "Anna" stores the
 * centroid of that cluster's embeddings so Anna is recognised in later
 * recordings. Fire-and-forget either way: the rename has already been applied
 * locally, and a failed enrollment only means the next recording starts from
 * "Speaker N" again.
 */
export function enrollSpeaker(sessionId: string, speaker: string, name: string): void {
  void post('/api/speakers/enroll', { session_id: sessionId, speaker, name }).catch(() => {
    /* best-effort */
  });
}
