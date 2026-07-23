/** Registry row shape returned by /api/background-tasks (server/tasks/routes.py). */
export interface BackgroundTaskInfo {
  task_id: string;
  kind: 'shell' | 'agent' | 'workflow';
  session_id: string;
  title: string;
  command?: string | null;
  status: 'running' | 'completed' | 'failed' | 'stopped' | 'interrupted';
  exit_code?: number | null;
  output_path?: string | null;
  result_text?: string | null;
  meta?: Record<string, unknown>;
  created_at: string;
  updated_at: string;
  finished_at?: string | null;
}
