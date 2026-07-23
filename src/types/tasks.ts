export type TaskStatus = 'pending' | 'in_progress' | 'completed';

export interface Task {
  id: string;
  title: string;
  content?: string;
  activeForm?: string;
  status: TaskStatus;
  sessionId?: string;
}
