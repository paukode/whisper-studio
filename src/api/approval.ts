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
}

/**
 * Single executor for any approval action. The backend looks up the
 * action's ApprovalSpec in its registry and runs the registered function.
 */
export function executeApproval(req: { action: string; payload: Record<string, unknown> }): Promise<ApprovalOutcome> {
  return post<ApprovalOutcome>('/api/approval/execute', req);
}
