import type { ChatMessage } from '@/types/chat';

/** One rendered transcript entry: a store message (or a merged run of them),
 *  the store index it answers to for actions/checkpoints, and the store
 *  indices it covers. */
export interface DisplayEntry {
  message: ChatMessage;
  index: number;
  /** Every store index folded into this entry (length 1 for a plain message). */
  indices: number[];
}

/** An assistant row that carries nothing a reader would read: no prose, no
 *  thinking, no artifact, plan or visuals. Only tool traces and cards. Every
 *  agent card commits the in-progress segment so the card lands in order
 *  (see sseStream.flushSegment), which is exactly what leaves runs of these
 *  behind: 9 of 15 rows in one real turn were bare "ACTIVITY N steps". */
export function isToolOnly(m: ChatMessage): boolean {
  if (m.role !== 'assistant') return false;
  if ((m.content ?? '').trim()) return false;
  if (m._thinkingText || m.programArtifact || m.plan) return false;
  if (m.visuals && m.visuals.length > 0) return false;
  const hasTools = !!m.toolUse && m.toolUse.length > 0;
  const hasReports = !!m.teamReports && Object.keys(m.teamReports).length > 0;
  return hasTools || hasReports;
}

/**
 * Fold consecutive tool-only assistant rows into one entry whose traces are
 * concatenated in order (cards keep their team_id keys, so report matching
 * is unchanged). Everything else passes through one-to-one. Pure: store
 * messages are never mutated and indices stay real, so edit / delete /
 * regenerate and task checkpoints keep addressing the right rows.
 */
export function groupToolOnlyRuns(messages: ChatMessage[]): DisplayEntry[] {
  const out: DisplayEntry[] = [];
  for (let i = 0; i < messages.length; i += 1) {
    const m = messages[i];
    const prev = out[out.length - 1];
    if (isToolOnly(m) && prev && isToolOnly(prev.message)) {
      const merged: ChatMessage = {
        ...prev.message,
        toolUse: [...(prev.message.toolUse ?? []), ...(m.toolUse ?? [])],
        teamReports:
          prev.message.teamReports || m.teamReports
            ? { ...(prev.message.teamReports ?? {}), ...(m.teamReports ?? {}) }
            : undefined,
      };
      out[out.length - 1] = { message: merged, index: prev.index, indices: [...prev.indices, i] };
      continue;
    }
    out.push({ message: m, index: i, indices: [i] });
  }
  return out;
}
