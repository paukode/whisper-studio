/**
 * The ToolMeta of a tool row, from the two places the server reports one:
 * the skill_result frame's `meta` (server/tool_meta.py, read in ./sseStream)
 * and a write's diff stats returned by POST /api/approval/execute (the card
 * path, applied in ./approvalLeg). Both are snake_case on the wire; the row
 * keeps every key, null when the server did not send a number.
 */
import type { ApprovalStats } from '@/api/approval';
import type { SSEEventData, ToolMeta } from '@/types/chat';

const num = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null);

/** A skill_result frame's meta as the row stores it; undefined for a frame
 *  that carried none (an older server). */
export function toolMetaFromFrame(meta: SSEEventData['meta']): ToolMeta | undefined {
  if (!meta) return undefined;
  return {
    durationMs: num(meta.duration_ms),
    outputChars: num(meta.output_chars),
    truncated: typeof meta.truncated === 'boolean' ? meta.truncated : null,
    lines: num(meta.lines),
    bytes: num(meta.bytes),
    added: num(meta.added),
    removed: num(meta.removed),
  };
}

/** A row's meta once the card's approved write returned its diff stats: the
 *  write numbers (bytes, added, removed) come from the stats, anything the
 *  row already had stays. The stats' line count of the written file is not
 *  the meta's `lines`, which is the lines a read returned. */
export function toolMetaWithStats(meta: ToolMeta | undefined, stats: ApprovalStats): ToolMeta {
  return {
    durationMs: meta?.durationMs ?? null,
    outputChars: meta?.outputChars ?? null,
    truncated: meta?.truncated ?? null,
    lines: meta?.lines ?? null,
    bytes: num(stats.bytes),
    added: num(stats.added),
    removed: num(stats.removed),
  };
}
