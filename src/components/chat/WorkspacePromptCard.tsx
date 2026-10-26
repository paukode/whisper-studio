import React, { useCallback, useEffect, useState } from 'react';
import { useUIStore } from '@/stores/uiStore';
import { settleWorkspaceOps, syncWorkspaceStatus } from '@/services/workspaceConnection';
import { ICON } from '@/components/chat/activityIcons';

export interface WorkspacePromptCardProps {
  suggested: string;
  recent: string[];
  /** Tool that paused, for a sentence that names what actually happened. */
  toolName?: string;
  /** Real tool_use_id of the paused tool call (server/tool_executor.py
   *  merges this into the SSE payload). Resuming MUST answer this exact id
   *  via the answers-array continuation path: a bare answer string is
   *  treated as a brand-new chat message, not a continuation, and never
   *  reaches the paused turn. */
  toolUseId: string;
}

/** Dispatch a resume for a paused tool_use block via the SAME mechanism
 *  UserQuestionCard already uses successfully: an `answers` array
 *  ({tool_use_id, content}), which useComposerChatEvents routes through
 *  chatStream.send('', {approvedToolResult: ...}), the only path that
 *  reaches server/chat/routes.py's approved_tool_result branch and restores
 *  the stashed paused-turn state. A bare `answer` string is treated as a
 *  brand new chat message instead, which never resumes anything. */
function dispatchResume(toolUseId: string, content: string): void {
  window.dispatchEvent(new CustomEvent('whisper-submit-answer', {
    detail: { answers: [{ tool_use_id: toolUseId, content }] },
  }));
}

/**
 * Interactive card shown when a tool that modifies or deletes an existing
 * file pauses mid-turn because no workspace is connected: a folder picker
 * that CONNECTS the chosen folder as the persistent workspace, then resumes
 * so the LLM re-issues the original call.
 *
 * This is the only situation that reaches the card. A path outside a
 * connected workspace never does: the workspace was fine and the path was
 * not, so the backend answers with a plain refusal (or, for a new file at an
 * absolute path, the ordinary save approval card) instead of offering to
 * switch roots. One-off "save this file somewhere" requests never reach it
 * either: the file-saving tools resolve the destination themselves (the
 * user's own words, or a Documents default) and go straight to the approval
 * card.
 */
export const WorkspacePromptCard: React.FC<WorkspacePromptCardProps> = ({
  suggested,
  recent,
  toolName,
  toolUseId,
}) => {
  const [isBusy, setIsBusy] = useState(false);
  const [resolved, setResolved] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Re-read the authoritative workspace the moment this card appears. The
  // panel only reconciles on mount and on window focus, so a root that moved
  // mid-turn (another process writing the shared workspace_config.json) leaves
  // the tree rendering a folder the server no longer has, which is exactly
  // how this card ends up looking like it fired for no reason. The status
  // endpoint is authoritative in both directions, and an answer that a
  // connect or disconnect overtook is dropped, so this can never push a
  // wrong value into the store.
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const outcome = await syncWorkspaceStatus();
        if (outcome === 'applied' && !cancelled) {
          window.dispatchEvent(new CustomEvent('whisper-workspace-refresh'));
        }
      } catch {
        // Unreachable backend: leave the panel exactly as it is (PR #71).
      }
    })();
    return () => { cancelled = true; };
  }, []);

  const connectAndResume = useCallback(async (path: string) => {
    setIsBusy(true);
    setError(null);
    try {
      // A disconnect clicked just before must reach the server first.
      await settleWorkspaceOps();
      const resp = await fetch('/api/workspace/connect', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path }),
      });
      if (!resp.ok) {
        const err = (await resp.json().catch(() => ({}))) as { error?: string };
        // Surface it. Connecting can genuinely fail (the folder was deleted,
        // the volume is gone) and a console-only error left the button
        // un-busying with no visible effect, which reads as a dead button.
        setError(err.error || `Could not connect ${path}.`);
        setIsBusy(false);
        return;
      }

      const data = (await resp.json()) as { writable?: boolean };
      setResolved(path);
      useUIStore.getState().setWsConnected(true, path);
      window.dispatchEvent(new CustomEvent('whisper-workspace-refresh'));
      // Soft writability hint (see _check_writable on the backend). We
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
      // tool already wrote the file": soft wording ("Please proceed with
      // creating the files") let some responses reply "Done!" without
      // re-issuing the write tool, and the file never landed on disk.
      dispatchResume(
        toolUseId,
        `Workspace connected to ${path}. ` +
          `IMPORTANT: the original tool call did NOT execute: ` +
          `no workspace was connected at the time. ` +
          `Re-issue the same tool call now (with the workspace-relative path) to actually perform the action.`,
      );
    } catch (err) {
      console.error('Workspace connect failed:', err);
      setError('Could not reach the backend.');
      setIsBusy(false);
    }
  }, [toolUseId]);

  const handleBrowse = useCallback(async () => {
    setIsBusy(true);
    setError(null);
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
      setError('Could not open the folder picker.');
      setIsBusy(false);
    }
  }, [connectAndResume]);

  /** Resolve the pause WITHOUT connecting anything. Without this the turn
   *  could only be unstuck by connecting some folder. */
  const handleDecline = useCallback(() => {
    setIsBusy(true);
    setResolved('');
    dispatchResume(
      toolUseId,
      `The user declined to connect a workspace. The original tool call did NOT execute. ` +
        `Do not retry it as-is. Say what you needed and let the user decide.`,
    );
  }, [toolUseId]);

  const recentPaths = recent.filter(p => p !== suggested);

  return (
    <div className="ws-approval-card ws-callout-info" style={{ padding: '12px 16px' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
        <span className="ws-callout-label">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
            <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>
          </svg>
          Workspace needed
        </span>
      </div>

      <div style={{
        fontSize: '0.85em',
        color: 'var(--text-muted)',
        marginBottom: 12,
        lineHeight: 1.5,
      }}>
        No workspace is connected, so {toolName ? <code>{toolName}</code> : 'the tool'} has
        nowhere to write. Pick a folder to connect.
      </div>

      {resolved !== null ? (
        <div style={{
          display: 'flex',
          alignItems: 'center',
          gap: 6,
          fontSize: '0.85em',
          color: 'var(--accent)',
          padding: '6px 10px',
          background: 'var(--bg-secondary)',
          borderRadius: 6,
        }}>
          {ICON.check}
          {resolved ? `Connected to ${resolved}` : 'Workspace left unchanged'}
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

          <button
            className="btn btn-sm"
            onClick={handleDecline}
            disabled={isBusy}
            type="button"
          >
            Not now
          </button>

          {error && (
            <div style={{ fontSize: '0.8em', color: 'var(--accent-record)' }}>{error}</div>
          )}
        </div>
      )}
    </div>
  );
};
