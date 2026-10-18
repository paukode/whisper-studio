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
 * Single executor for any approval action. The backend looks up the
 * action's ApprovalSpec in its registry and runs the registered function.
 * `tool_use_id` is the paused call's id: a write keeps its edit under it.
 */
export function executeApproval(req: {
  action: string;
  payload: Record<string, unknown>;
  tool_use_id?: string;
}): Promise<ApprovalOutcome> {
  return post<ApprovalOutcome>('/api/approval/execute', req);
}
