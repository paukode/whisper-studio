import { post } from './client';

/**
 * What actually happened when an approved action ran.
 *
 * THE definition — the approval card and the continuation leg
 * (sendApprovalContinuation, which turns this into the model's tool_result)
 * both import it from here. A second, narrower copy used to live in sseStream;
 * the two only ever type-checked because they were structurally compatible, and
 * `ws_folder_opened` was invisible to everything that used the narrow one.
 */
export interface ApprovalOutcome {
  ok: boolean;
  output?: string;
  error?: string;
  /**
   * Set when the approved action connected a new workspace (e.g. git_clone
   * with open=true). The frontend switches the active workspace to this path.
   */
  ws_folder_opened?: string | null;
  /** The user's Stop ended the action before it finished (an approved shell
   *  command; git and other actions are left to finish). */
  stopped?: boolean;
  /** A file write's diff stats (server/approval/executors.py _do_write), the
   *  same numbers the inline path puts in its skill_result meta; null for
   *  every other action. */
  stats?: ApprovalStats | null;
  /** A finished command's exit status (ws_run_command, aws_cli), the one
   *  the inline path's skill_result frame carries; null for every other
   *  action, and for a command that went to the background or was stopped. */
  exit_code?: number | null;
  /** What only the model reads after `output`: the project's formatter and
   *  linter after a code file write (server/code_tools/after_write.py). The
   *  continuation appends it to the tool_result; the activity row never
   *  shows it. */
  model_note?: string | null;
}

/** What a write changed: lines added and removed against the file before it
 *  (null when that file could not be read), and the lines and bytes of the
 *  file on disk after it. `approximate` says the exact diff was over its
 *  bounds and the counts are an estimate; `stored` says the edit was kept
 *  for the diff dialog (GET /api/workspace/edits/{id}). */
export interface ApprovalStats {
  added?: number | null;
  removed?: number | null;
  lines?: number | null;
  bytes?: number | null;
  approximate?: boolean;
  stored?: boolean;
}

/**
 * Run the action of the approval card the user said Yes to. The server
 * keeps every card it showed (server/approval/cards.py) and runs the action,
 * payload and sandbox scope it recorded for `card_id`, at most once; a card
 * it no longer knows, or one already answered, comes back as a failed
 * outcome saying so.
 */
export function executeApproval(cardId: string): Promise<ApprovalOutcome> {
  return post<ApprovalOutcome>('/api/approval/execute', { card_id: cardId });
}
