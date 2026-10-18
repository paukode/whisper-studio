/**
 * Writing what a call did onto its committed row, in place, found by the
 * call's tool_use_id. Two moments need it: a skill_result that arrives after
 * its row was committed (a card frame commits the rows above it first:
 * sseStream), and the answer to an approval card (approvalLeg). Every row
 * that holds the id is patched, since a Stop while a card waited can have
 * committed a second copy of it. Saved explicitly: the row count does not
 * change, which is what the runtime's own save trigger keys on. A row that
 * is gone (deleted, or an older transcript shape) is left alone.
 */
import type { StoreApi } from 'zustand/vanilla';
import type { ChatState } from '@/stores/chatStore';
import { useSessionStore } from '@/stores/sessionStore';
import type { SkillTrace, ToolUseEvent } from '@/types/chat';

/** What a call's outcome changes on its row: the ToolUseEvent fields, and
 *  the fields a message's raw traces keep of them. */
export interface RowOutcome {
  result: string;
  status: 'complete' | 'error' | 'stopped';
  isError: boolean;
  exitCode: number | null;
  /** The row's new meta from its old one; left out, the meta stays. */
  meta?: (old: ToolUseEvent['meta']) => ToolUseEvent['meta'];
  previewImage?: ToolUseEvent['previewImage'];
}

/** Patch every committed row of call `id` with `outcome`, and save. Returns
 *  whether a row held the id. */
export function patchCommittedRows(
  chat: StoreApi<ChatState>,
  sessionId: string,
  id: string,
  outcome: RowOutcome,
): boolean {
  const messages = chat.getState().messages;
  let found = false;
  const next = messages.map((msg) => {
    if (!msg.toolUse?.some((t) => t.toolId === id)) return msg;
    found = true;
    return {
      ...msg,
      toolUse: msg.toolUse.map((t) => (t.toolId === id ? patchedRow(t, outcome) : t)),
      // The message's raw traces, when it kept them, say the same.
      ...(msg.traces ? { traces: msg.traces.map((t) => (t.id === id ? patchedTrace(t, outcome) : t)) } : {}),
    };
  });
  if (!found) return false;
  chat.getState().setMessages(next);
  useSessionStore.getState().debouncedSave(sessionId);
  return true;
}

function patchedRow(row: ToolUseEvent, outcome: RowOutcome): ToolUseEvent {
  const meta = outcome.meta ? outcome.meta(row.meta) : row.meta;
  return {
    ...row,
    result: outcome.result,
    status: outcome.status,
    isError: outcome.isError,
    exitCode: outcome.exitCode,
    ...(meta ? { meta } : {}),
    ...(outcome.previewImage ? { previewImage: outcome.previewImage } : {}),
  };
}

function patchedTrace(trace: SkillTrace, outcome: RowOutcome): SkillTrace {
  // Answered now: no longer the call a card waits on.
  const { paused, ...rest } = trace;
  const meta = outcome.meta ? outcome.meta(rest.meta) : rest.meta;
  return {
    ...rest,
    output: outcome.result,
    status: outcome.isError ? 'error' : 'complete',
    resolved: true,
    exitCode: outcome.exitCode,
    ...(meta ? { meta } : {}),
  };
}
