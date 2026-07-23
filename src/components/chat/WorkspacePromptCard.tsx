import React, { useCallback, useState } from 'react';
import { useUIStore } from '@/stores/uiStore';

export interface WorkspacePromptCardProps {
  reason: string;
  suggested: string;
  recent: string[];
  toolUseId: string;
}

/**
 * Interactive folder picker card shown when a write tool fires
 * without a connected workspace. Displays suggested/recent paths
 * and a Browse button for the native OS folder picker.
 *
 * After the user selects a folder, connects to it and sends a
 * continuation message so the LLM re-issues the write tools.
 */
export const WorkspacePromptCard: React.FC<WorkspacePromptCardProps> = ({
  reason,
  suggested,
  recent,
}) => {
  const [isConnecting, setIsConnecting] = useState(false);
  const [connected, setConnected] = useState(false);
  const [selectedPath, setSelectedPath] = useState<string | null>(null);

  const connectAndResume = useCallback(async (path: string) => {
    setIsConnecting(true);
    setSelectedPath(path);
    try {
      const resp = await fetch('/api/workspace/connect', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path }),
      });
      if (!resp.ok) {
        const err = (await resp.json()) as { error?: string };
        console.error('Workspace connect failed:', err.error);
        setIsConnecting(false);
        return;
      }

      const data = (await resp.json()) as { writable?: boolean };
      setConnected(true);
      useUIStore.getState().setWsConnected(true, path);
      window.dispatchEvent(new CustomEvent('whisper-workspace-refresh'));
      // Soft writability hint — see _check_writable on the backend. We
      // never refuse the connection on this signal because os.access is
      // unreliable for network mounts, root, and macOS ACLs; we just tell
      // the user up front that writes might fail.
      if (data?.writable === false) {
        useUIStore.getState().addToast({
          type: 'info',
          message: `Connected to ${path}. Folder may be read-only. Writes will be confirmed on first attempt.`,
          duration: 5000,
        });
      }

      // Resume the LLM. Be explicit about what did and did not happen so
      // the model doesn't conflate "workspace connected" with "the original
      // tool already wrote the file" — the previous wording ("Please
      // proceed with creating the files") was soft enough that some
      // responses replied "Done!" without re-issuing the write tool, and
      // the file never landed on disk.
      window.dispatchEvent(new CustomEvent('whisper-submit-answer', {
        detail: {
          answer:
            `Workspace connected to ${path}. ` +
            `IMPORTANT: the original tool call did NOT execute because no workspace was connected at the time. ` +
            `Re-issue the same tool call now (with the workspace-relative path) to actually perform the action.`,
        },
      }));
    } catch (err) {
      console.error('Workspace connect failed:', err);
    } finally {
      setIsConnecting(false);
    }
  }, []);

  const handleBrowse = useCallback(async () => {
    setIsConnecting(true);
    try {
      const resp = await fetch('/api/workspace/pick-folder');
      const data = (await resp.json()) as { path?: string | null; cancelled?: boolean };
      if (data.path) {
        await connectAndResume(data.path);
      } else {
        setIsConnecting(false);
      }
    } catch (err) {
      console.error('Folder picker failed:', err);
      setIsConnecting(false);
    }
  }, [connectAndResume]);

  // Deduplicate: suggested path may also be in recent list
  const recentPaths = recent.filter(p => p !== suggested);

  return (
    <div className="ws-approval-card" style={{
      border: '1px solid var(--accent)',
      borderRadius: 8,
      padding: '12px 16px',
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" strokeWidth="2" strokeLinecap="round">
          <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>
        </svg>
        <span style={{ fontSize: '0.85em', fontWeight: 600 }}>
          Workspace needed
        </span>
      </div>

      <div style={{
        fontSize: '0.85em',
        color: 'var(--text-muted)',
        marginBottom: 12,
        lineHeight: 1.4,
      }}>
        {reason || 'No workspace connected. Select a folder to save files.'}
      </div>

      {connected && selectedPath ? (
        <div style={{
          fontSize: '0.85em',
          color: 'var(--accent)',
          padding: '6px 10px',
          background: 'var(--bg-secondary)',
          borderRadius: 6,
        }}>
          {'\u2713'} Connected to {selectedPath}
        </div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
          {/* Suggested path */}
          {suggested && (
            <button
              className="btn btn-primary btn-sm"
              onClick={() => void connectAndResume(suggested)}
              disabled={isConnecting}
              type="button"
              style={{ textAlign: 'left', fontFamily: 'var(--font-mono)', fontSize: '0.8em' }}
            >
              {suggested}
            </button>
          )}

          {/* Recent workspaces */}
          {recentPaths.slice(0, 4).map((path) => (
            <button
              key={path}
              className="btn btn-sm"
              onClick={() => void connectAndResume(path)}
              disabled={isConnecting}
              type="button"
              style={{ textAlign: 'left', fontFamily: 'var(--font-mono)', fontSize: '0.8em' }}
            >
              {path}
            </button>
          ))}

          {/* Browse button */}
          <button
            className="btn btn-sm"
            onClick={() => void handleBrowse()}
            disabled={isConnecting}
            type="button"
          >
            {isConnecting ? 'Connecting\u2026' : 'Browse\u2026'}
          </button>
        </div>
      )}
    </div>
  );
};
