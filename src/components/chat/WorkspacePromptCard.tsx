import React, { useCallback, useEffect, useState } from 'react';
import { useUIStore } from '@/stores/uiStore';

export interface WorkspacePromptCardProps {
  reason: string;
  suggested: string;
  recent: string[];
  /** The root the containment check actually used, or null when nothing was
   *  connected. Shown verbatim: when it disagrees with the folder the panel is
   *  rendering, that disagreement IS the answer to "why am I being asked?". */
  workspace?: string | null;
  /** Absolute path the check rejected (outside_workspace only). */
  attempted?: string | null;
  /** Tool that paused, for a sentence that names what actually happened. */
  toolName?: string;
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
 *
 * The card used to render the bare `reason` slug ("outside_workspace") as its
 * whole explanation, which reads as a bug report rather than a question — and
 * offered no way out except connecting SOMETHING, even in the common case
 * where the workspace was right and only the path was wrong.
 */
export const WorkspacePromptCard: React.FC<WorkspacePromptCardProps> = ({
  reason,
  suggested,
  recent,
  workspace,
  attempted,
  toolName,
  toolUseId,
}) => {
  const [isBusy, setIsBusy] = useState(false);
  const [resolved, setResolved] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const outside = reason === 'outside_workspace';

  // Re-read the authoritative workspace the moment this card appears. The
  // panel only reconciles on mount and on window focus, so a root that moved
  // mid-turn (another process writing the shared workspace_config.json) leaves
  // the tree rendering a folder the server no longer has — which is exactly
  // how this card ends up looking like it fired for no reason. The status
  // endpoint is authoritative in both directions, so this can never push a
  // wrong value into the store.
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const resp = await fetch('/api/workspace/status');
        if (!resp.ok || cancelled) return;
        const data = (await resp.json()) as { connected?: boolean; path?: string };
        if (cancelled) return;
        const truePath = data.connected && data.path ? data.path : '';
        if (truePath === useUIStore.getState().wsPath) return;
        useUIStore.getState().setWsConnected(Boolean(truePath), truePath || undefined);
        window.dispatchEvent(new CustomEvent('whisper-workspace-refresh'));
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
          `IMPORTANT: the original tool call did NOT execute: ` +
          (outside
            ? `its path was outside the workspace connected at the time. `
            : `no workspace was connected at the time. `) +
          `Re-issue the same tool call now (with the workspace-relative path) to actually perform the action.`,
      );
    } catch (err) {
      console.error('Workspace connect failed:', err);
      setError('Could not reach the backend.');
      setIsBusy(false);
    }
  }, [toolUseId, outside]);

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

  /** Resolve the pause WITHOUT touching the workspace. For outside_workspace
   *  this is usually the right answer: the workspace was fine and the model
   *  simply aimed at the wrong path, so switching roots to satisfy one bad
   *  path is the more destructive option. Without this the turn could only be
   *  unstuck by connecting some folder. */
  const handleKeep = useCallback(() => {
    setIsBusy(true);
    setResolved(workspace || '');
    dispatchResume(
      toolUseId,
      workspace
        ? `The user declined to change the workspace. It is still ${workspace}. ` +
            `The original tool call did NOT execute because its path fell outside that workspace. ` +
            `Retry with a path inside ${workspace}, or explain why the write has to happen elsewhere.`
        : `The user declined to connect a workspace. The original tool call did NOT execute. ` +
            `Do not retry it as-is. Say what you needed and let the user decide.`,
    );
  }, [toolUseId, workspace]);

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
          {outside ? 'Path outside the workspace' : 'Workspace needed'}
        </span>
      </div>

      <div style={{
        fontSize: '0.85em',
        color: 'var(--text-muted)',
        marginBottom: 12,
        lineHeight: 1.5,
      }}>
        {outside ? (
          <>
            <div>
              {toolName ? <code>{toolName}</code> : 'A tool'} tried to reach a path outside the
              connected workspace, so nothing was written.
            </div>
            {workspace && (
              <div style={{ marginTop: 6 }}>
                Connected workspace:{' '}
                <code style={{ fontFamily: 'var(--font-mono)' }}>{workspace}</code>
              </div>
            )}
            {attempted && (
              <div>
                Attempted path:{' '}
                <code style={{ fontFamily: 'var(--font-mono)' }}>{attempted}</code>
              </div>
            )}
          </>
        ) : (
          <>
            No workspace is connected, so {toolName ? <code>{toolName}</code> : 'the tool'} has
            nowhere to write. Pick a folder to connect.
          </>
        )}
      </div>

      {resolved !== null ? (
        <div style={{
          fontSize: '0.85em',
          color: 'var(--accent)',
          padding: '6px 10px',
          background: 'var(--bg-secondary)',
          borderRadius: 6,
        }}>
          {resolved
            ? `✓ Connected to ${resolved}`
            : '✓ Workspace left unchanged'}
        </div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
          {/* Resolve without switching roots — the likely answer whenever a
              workspace is already connected. */}
          {outside && (
            <button
              className="btn btn-primary btn-sm"
              onClick={handleKeep}
              disabled={isBusy}
              type="button"
              style={{ textAlign: 'left' }}
            >
              Keep this workspace and retry inside it
            </button>
          )}

          {/* Suggested path */}
          {suggested && (
            <button
              className={outside ? 'btn btn-sm' : 'btn btn-primary btn-sm'}
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

          {!outside && (
            <button
              className="btn btn-sm"
              onClick={handleKeep}
              disabled={isBusy}
              type="button"
            >
              Not now
            </button>
          )}

          {error && (
            <div style={{ fontSize: '0.8em', color: 'var(--accent-record)' }}>{error}</div>
          )}
        </div>
      )}
    </div>
  );
};
