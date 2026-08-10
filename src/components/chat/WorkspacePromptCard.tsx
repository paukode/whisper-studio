import React, { useCallback, useState } from 'react';
import { useUIStore } from '@/stores/uiStore';

export interface WorkspacePromptCardProps {
  reason: string;
  suggested: string;
  recent: string[];
  /** Real tool_use_id of the paused tool call (server/tool_executor.py
   *  merges this into the SSE payload). Resuming MUST answer this exact id
   *  via the answers-array continuation path — a bare answer string is
   *  treated as a brand-new chat message, not a continuation, and never
   *  reaches the paused turn. */
  toolUseId: string;
}

/** Dispatch a resume for a paused tool_use block via the SAME mechanism
 *  UserQuestionCard already uses successfully: an `answers` array
 *  ({tool_use_id, content}), which useComposerChatEvents routes through
 *  chatStream.send('', {approvedToolResult: ...}) — the only path that
 *  reaches server/chat/routes.py's approved_tool_result branch and restores
 *  the stashed paused-turn state. A bare `answer` string is treated as a
 *  brand new chat message instead, which never resumes anything. */
function dispatchResume(toolUseId: string, content: string): void {
  window.dispatchEvent(new CustomEvent('whisper-submit-answer', {
    detail: { answers: [{ tool_use_id: toolUseId, content }] },
  }));
}

/**
 * Interactive card shown when a workspace-write tool pauses mid-turn because
 * it targets a connected workspace that doesn't exist or a path outside it
 * ('no_workspace' / 'outside_workspace'): a folder picker that CONNECTS the
 * chosen folder as the persistent workspace, then resumes so the LLM
 * re-issues the original write.
 *
 * One-off "save this file somewhere" requests never reach this card: the
 * file-saving tools resolve the destination themselves (the user's own
 * words, or a Documents default) and go straight to the approval card.
 */
export const WorkspacePromptCard: React.FC<WorkspacePromptCardProps> = ({
  reason,
  suggested,
  recent,
  toolUseId,
}) => {
  const [isBusy, setIsBusy] = useState(false);
  const [resolved, setResolved] = useState(false);
  const [selectedPath, setSelectedPath] = useState<string | null>(null);

  const connectAndResume = useCallback(async (path: string) => {
    setIsBusy(true);
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
        setIsBusy(false);
        return;
      }

      const data = (await resp.json()) as { writable?: boolean };
      setResolved(true);
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
      // tool already wrote the file" — soft wording ("Please proceed with
      // creating the files") let some responses reply "Done!" without
      // re-issuing the write tool, and the file never landed on disk.
      dispatchResume(
        toolUseId,
        `Workspace connected to ${path}. ` +
          `IMPORTANT: the original tool call did NOT execute because no workspace was connected at the time. ` +
          `Re-issue the same tool call now (with the workspace-relative path) to actually perform the action.`,
      );
    } catch (err) {
      console.error('Workspace connect failed:', err);
    } finally {
      setIsBusy(false);
    }
  }, [toolUseId]);

  const handleBrowse = useCallback(async () => {
    setIsBusy(true);
    try {
      const resp = await fetch('/api/workspace/pick-folder');
      const data = (await resp.json()) as { path?: string | null; cancelled?: boolean };
      if (data.path) {
        await connectAndResume(data.path);
      } else {
        setIsBusy(false);
      }
    } catch (err) {
      console.error('Folder picker failed:', err);
      setIsBusy(false);
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

      {resolved && selectedPath ? (
        <div style={{
          fontSize: '0.85em',
          color: 'var(--accent)',
          padding: '6px 10px',
          background: 'var(--bg-secondary)',
          borderRadius: 6,
        }}>
          {'✓'} Connected to {selectedPath}
        </div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
          {/* Suggested path */}
          {suggested && (
            <button
              className="btn btn-primary btn-sm"
              onClick={() => void connectAndResume(suggested)}
              disabled={isBusy}
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
              disabled={isBusy}
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
            disabled={isBusy}
            type="button"
          >
            {isBusy ? 'Connecting…' : 'Browse…'}
          </button>
        </div>
      )}
    </div>
  );
};
