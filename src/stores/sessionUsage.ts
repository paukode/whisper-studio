import { get } from '@/api/client';
import { getChatStore } from './sessionRuntimes';

interface SessionUsageResponse {
  prompt_tokens?: number;
  output_tokens?: number;
  cost_usd?: number;
  /** The dated GPT list-rate note when any round ran on GPT, '' otherwise. */
  note?: string;
  estimated_rounds?: number;
}

/** Seed or refresh a session's token/cost readout from the spend the server
 *  recorded for it, so the composer counter covers the whole conversation
 *  rather than restarting at the next turn, and carries the pricing note and
 *  estimate count of the rounds behind it. Called when a session is opened
 *  and after each of its turns. Best-effort: a session with no recorded
 *  rounds (or a failed fetch) keeps what it shows. */
export async function refreshSessionUsage(sessionId: string): Promise<void> {
  try {
    const usage = await get<SessionUsageResponse>(
      `/api/costs/session/${encodeURIComponent(sessionId)}`,
    );
    getChatStore(sessionId).getState().hydrateSessionUsage(
      usage.prompt_tokens ?? 0,
      usage.output_tokens ?? 0,
      usage.cost_usd ?? 0,
      { note: usage.note ?? '', estimatedRounds: usage.estimated_rounds ?? 0 },
    );
  } catch {
    // Cosmetic counter: never let it fail a session load or a turn.
  }
}
