/** REST client for the update check (server/update_check.py). */
import { get } from '@/api/client';

export interface LatestRelease {
  tag: string;
  version: string;
  url: string;
  published_at: string;
}

export interface UpdateCheck {
  current: string | null;
  install: 'app' | 'source';
  latest: LatestRelease | null;
  update_available: boolean;
  /** Shell commands that update a setup.sh source install; empty for the app. */
  update_commands: string[];
}

export function getUpdateCheck(): Promise<UpdateCheck> {
  return get<UpdateCheck>('/api/update-check');
}
