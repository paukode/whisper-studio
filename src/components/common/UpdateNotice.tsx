import { useCallback, useEffect, useState } from 'react';
import { getUpdateCheck, type UpdateCheck } from '@/api/updateCheck';
import { STORAGE_KEYS } from '@/utils/storageKeys';
import { useUIStore } from '@/stores/uiStore';

/** The Mac app's Check for Updates menu item dispatches this on the window
 *  (macapp/shell/main.swift checkForUpdates). */
export const CHECK_FOR_UPDATES_EVENT = 'ws-check-for-updates';

/**
 * Update notice: a slim callout under the header when the installed app is
 * older than the newest GitHub release (server/update_check.py decides; a
 * dev build ahead of the newest tag never sees it). Update opens the release
 * page: a target=_blank link, which the Mac app's WKWebView hands to the
 * default browser (macapp/shell/main.swift createWebViewWith) and a browser
 * opens in a new tab. A source install also gets the commands to update.
 * Dismiss hides the notice for that one version only; a newer release shows
 * it again. The notice only reports: it never changes permissions or
 * behaviour, and a failed check shows nothing.
 *
 * Check for Updates (the Mac app menu) asks again at once, past the backend's
 * cache: a newer release shows the notice even if that version was
 * dismissed, and otherwise a toast says the app is up to date or that the
 * check could not reach GitHub.
 */

function readDismissed(): string {
  try {
    return localStorage.getItem(STORAGE_KEYS.UPDATE_DISMISSED) ?? '';
  } catch {
    return '';
  }
}

function writeDismissed(version: string): void {
  try {
    localStorage.setItem(STORAGE_KEYS.UPDATE_DISMISSED, version);
  } catch {
    // Storage blocked: the notice still hides for this page load.
  }
}

export const UpdateNotice: React.FC = () => {
  const [check, setCheck] = useState<UpdateCheck | null>(null);
  const [dismissed, setDismissed] = useState<string>(() => readDismissed());

  useEffect(() => {
    let live = true;
    getUpdateCheck()
      .then((data) => {
        if (live) setCheck(data ?? null);
      })
      .catch(() => {
        // No network or an old backend: no notice.
      });
    return () => {
      live = false;
    };
  }, []);

  useEffect(() => {
    const onCheck = () => {
      const { addToast } = useUIStore.getState();
      getUpdateCheck(true)
        .then((data) => {
          setCheck(data ?? null);
          if (data?.update_available && data.latest) {
            setDismissed('');
            return;
          }
          if (data?.latest) {
            addToast({ type: 'success', message: `Whisper Studio is up to date (v${data.current ?? data.latest.version}).` });
          } else {
            addToast({ type: 'warning', message: 'Could not check for updates. Check the network connection, then try again.' });
          }
        })
        .catch(() => {
          addToast({ type: 'warning', message: 'Could not check for updates. Check the network connection, then try again.' });
        });
    };
    window.addEventListener(CHECK_FOR_UPDATES_EVENT, onCheck);
    return () => window.removeEventListener(CHECK_FOR_UPDATES_EVENT, onCheck);
  }, []);

  const latest = check?.latest;
  const version = latest?.version ?? '';

  const dismiss = useCallback(() => {
    writeDismissed(version);
    setDismissed(version);
  }, [version]);

  if (!check?.update_available || !latest || !latest.url || dismissed === version) return null;

  const commands = check.install === 'source' ? (check.update_commands ?? []) : [];

  return (
    <div className="ws-approval-card ws-callout-info update-notice" role="status" data-testid="update-notice">
      <div className="update-notice__row">
        <span className="ws-callout-label">Update</span>
        <span className="update-notice__text">
          A newer version of Whisper Studio is available (v{version}).
          {check.current ? <span className="update-notice__current"> You have v{check.current}.</span> : null}
        </span>
        <a
          className="btn btn-primary btn-sm update-notice__update"
          href={latest.url}
          target="_blank"
          rel="noopener noreferrer"
        >
          Update
        </a>
        <button type="button" className="btn btn-secondary btn-sm" onClick={dismiss}>
          Dismiss
        </button>
      </div>
      {commands.length > 0 && (
        <div className="update-notice__commands">
          <span>To update this source install, run these commands in the project folder:</span>
          <code>{commands.join(' && ')}</code>
        </div>
      )}
    </div>
  );
};
