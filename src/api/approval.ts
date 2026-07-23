import { post } from './client';

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
 * Replaces the per-action switch that lived in the old executeWsApproval.
 */
export function executeApproval(req: { action: string; payload: Record<string, unknown> }): Promise<ApprovalOutcome> {
  return post<ApprovalOutcome>('/api/approval/execute', req);
}
