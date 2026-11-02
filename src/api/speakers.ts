import { post } from '@/api/client';

/**
 * Turn a speaker rename into an enrollment.
 *
 * Only does anything when the `speaker_voiceprints` flag is on, which it is
 * not by default: the server drops the call and the name stays local to this
 * recording. With the flag on, naming "Speaker 2" as "Anna" stores the
 * centroid of that cluster's embeddings so Anna is recognised in later
 * recordings. The rename is already applied locally; the promise rejects
 * when the enrollment failed, so the caller can say the name will not be
 * recognised next time.
 */
export function enrollSpeaker(sessionId: string, speaker: string, name: string): Promise<unknown> {
  return post('/api/speakers/enroll', { session_id: sessionId, speaker, name });
}
