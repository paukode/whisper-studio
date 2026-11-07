/** REST client for the update check (server/update_check.py). */
import { get } from '@/api/client';

export interface LatestRelease {
  tag: string;
  version: string;
  url: string;
  /** The release's DMG (server/update_check.py _dmg_url); "" when it has none. */
  download_url?: string;
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

/** `fresh` skips the backend's cache: a person asked (Check for Updates). */
export function getUpdateCheck(fresh = false): Promise<UpdateCheck> {
  return get<UpdateCheck>(fresh ? '/api/update-check?fresh=true' : '/api/update-check');
}
