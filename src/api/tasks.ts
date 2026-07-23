import { get } from './client';
import type { SessionTask } from '@/stores/taskStore';
import { normalizeTasks } from '@/stores/taskStore';

/** Fetch a session's authoritative task list (server/tasks_tracker.py). Used to
 *  hydrate the task store on session load/switch, since the live `todo_update`
 *  SSE events don't replay for a restored conversation. */
export async function fetchSessionTasks(sessionId: string): Promise<SessionTask[]> {
  const data = await get<{ tasks?: unknown }>(`/api/tasks/${encodeURIComponent(sessionId)}`);
  return normalizeTasks(data);
}
