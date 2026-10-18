/** Registry row shape returned by /api/background-tasks (server/tasks/routes.py). */
export type BackgroundTaskKind = 'shell' | 'agent' | 'workflow' | 'ci';
export type BackgroundTaskStatus = 'running' | 'completed' | 'failed' | 'stopped' | 'interrupted';

export interface BackgroundTaskInfo {
  task_id: string;
  kind: BackgroundTaskKind;
  session_id: string;
  title: string;
  command?: string | null;
  status: BackgroundTaskStatus;
  exit_code?: number | null;
  /** Process-group leader of a shell task. */
  pid?: number | null;
  output_path?: string | null;
  result_text?: string | null;
  meta?: Record<string, unknown>;
  created_at: string;
  updated_at: string;
  finished_at?: string | null;
}

/** One lifecycle frame on the app's event channel (server/tasks/events.py
 *  emit_task_event): a task started or reached a terminal state. Live only;
 *  the registry row is the durable record. */
export interface TaskEventPayload {
  event_type: 'task_started' | 'task_completed' | 'task_failed' | 'task_stopped';
  task_id: string;
  kind: BackgroundTaskKind;
  title: string;
  status: string;
  exit_code?: number | null;
  duration_ms?: number | null;
  result_tail?: string;
  timestamp: string;
}
