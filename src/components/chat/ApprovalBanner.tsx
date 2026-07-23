import { useCallback, useState } from 'react';
import { getChatStore, useActiveChatStore } from '@/stores/sessionRuntimes';
import type { PendingApproval } from '@/stores/chatStore';
import { sendApprovalContinuation } from '@/hooks/useChatStream';
import { useUIStore } from '@/stores/uiStore';
import { executeApproval } from '@/api/approval';
import { DiffPreview } from './previews/DiffPreview';
import { CommandPreview } from './previews/CommandPreview';
import { FileListPreview } from './previews/FileListPreview';
import { TextPreview } from './previews/TextPreview';

/**
 * Generic approval banner. Renders one of four preview components based on
 * `currentApproval.preview` — no per-action conditionals. Adding a new
 * approval-required tool on the backend requires zero changes here.
 */
export function ApprovalBanner() {
  const currentApproval = useActiveChatStore((s) => s.currentApproval);
  const approvalQueue = useActiveChatStore((s) => s.approvalQueue);
  const [isProcessing, setIsProcessing] = useState(false);

  // Handlers bind the approval's OWNING session store once, up front: the
  // continuation can outlive a session switch, and resolving "active" after
  // an await would mutate whichever session the user happens to be viewing.
  const handleApprove = useCallback(async (approval: PendingApproval, approveAll: boolean) => {
    setIsProcessing(true);
    const chat = getChatStore(approval.sessionId);

    if (approveAll) {
      chat.getState().setSessionApproval(approval.category, 'allow');
    }
    chat.getState().clearCurrentApproval();

    // Single executor: backend looks up the spec and runs its registered
    // function. No per-action switch on the frontend.
    const outcome = await executeApproval({ action: approval.action, payload: approval.payload });

    if (!outcome.ok) {
      useUIStore.getState().addToast({
        type: 'error',
        message: `Approval failed: ${outcome.error ?? 'unknown error'}`,
        duration: 6000,
        key: 'approval-apply-error',
      });
    } else {
      // An action may have connected a new workspace (e.g. git_clone with
      // open=true). Switch the active workspace so the panel opens — the
      // backend already updated its config, this brings the UI in line.
      if (outcome.ws_folder_opened) {
        useUIStore.getState().setWsConnected(true, outcome.ws_folder_opened);
      }
      window.dispatchEvent(new CustomEvent('whisper-workspace-refresh'));
    }

    await sendApprovalContinuation(approval, approval.sessionId, true, undefined, outcome);

    if (!chat.getState().currentApproval) {
      chat.getState().showNextApproval();
    }
    setIsProcessing(false);
  }, []);

  const handleDeny = useCallback(async (approval: PendingApproval, denyAll: boolean) => {
    setIsProcessing(true);
    const chat = getChatStore(approval.sessionId);

    if (denyAll) {
      chat.getState().setSessionApproval(approval.category, 'deny');
    }
    chat.getState().clearCurrentApproval();

    await sendApprovalContinuation(approval, approval.sessionId, false);

    if (!chat.getState().currentApproval) {
      chat.getState().showNextApproval();
    }
    setIsProcessing(false);
  }, []);

  const handleUndo = useCallback(async (approval: PendingApproval) => {
    const path = approval.payload?.path as string | undefined;
    if (!path) return;
    try {
      const resp = await fetch('/api/workspace/undo', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path }),
      });
      const data = await resp.json().catch(() => ({} as Record<string, unknown>));
      const undone = resp.ok && (data.undone === true || data.restored === true);
      if (undone) {
        useUIStore.getState().addToast({
          type: 'success', message: `Undid changes to ${path}`, duration: 3000,
        });
        window.dispatchEvent(new CustomEvent('whisper-workspace-refresh'));
        const note = `> Note: User undid the prior write to \`${path}\`. The file has been restored to its pre-write contents.\n\n`;
        window.dispatchEvent(new CustomEvent('whisper-chat-insert', { detail: { text: note } }));
      } else {
        const reason = (typeof data.error === 'string' && data.error) || `HTTP ${resp.status}`;
        useUIStore.getState().addToast({ type: 'error', message: `Undo failed: ${reason}`, duration: 5000 });
      }
    } catch (err) {
      useUIStore.getState().addToast({
        type: 'error',
        message: `Undo failed: ${err instanceof Error ? err.message : String(err)}`,
        duration: 5000,
      });
    }
  }, []);

  if (!currentApproval) return null;

  const payload = currentApproval.payload ?? {};
  const explanationText = extractExplanation(currentApproval.explanation);
  const riskLabel = currentApproval.riskHint ?? null;

  // Pick the preview renderer by kind. New preview kinds = one new branch
  // here; new tool actions = zero changes (they pick an existing renderer).
  const PreviewBody = (() => {
    switch (currentApproval.preview) {
      case 'diff':
        return (
          <DiffPreview
            original={payload.original as string | undefined}
            content={payload.content as string | undefined}
            path={payload.path as string | undefined}
          />
        );
      case 'command':
        return (
          <CommandPreview
            command={payload.command as string | undefined}
            cwd={payload.cwd as string | undefined}
          />
        );
      case 'list':
        return <FileListPreview paths={payload.paths as string[] | undefined} />;
      case 'text':
      default:
        return <TextPreview text={currentApproval.summary} />;
    }
  })();

  const allowAllLabel = `Yes, all ${currentApproval.category}`;
  const blockLabel = `Block ${currentApproval.category}`;
  const showUndo = currentApproval.preview === 'diff' && !!payload.path;

  return (
    <div className="chat-msg-wrap assistant-wrap">
      <div className="ws-approval-card" style={{ border: '1px solid var(--accent)', borderRadius: 8, padding: '12px 16px' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
          <span style={{ fontSize: '0.85em', color: 'var(--text-muted)' }}>🔒 Approval needed</span>
          {riskLabel && (
            <span className={`risk-badge risk-${riskLabel}`} style={{
              fontSize: '0.75em', padding: '2px 6px', borderRadius: 4, textTransform: 'uppercase',
            }}>{riskLabel}</span>
          )}
          {approvalQueue.length > 0 && (
            <span style={{ fontSize: '0.75em', color: 'var(--text-muted)', marginLeft: 'auto' }}>
              {approvalQueue.length} more pending
            </span>
          )}
        </div>

        <div style={{ marginBottom: 8, fontFamily: 'var(--font-mono)', fontSize: '0.85em', wordBreak: 'break-all' }}>
          {currentApproval.summary}
        </div>

        {payload.matched_via_normalization === true && (
          <div style={{ fontSize: '0.78em', color: 'var(--text-muted)', marginBottom: 8, fontStyle: 'italic' }}>
            Matched via quote normalization: the file uses typographic quotes that differ from the requested text.
          </div>
        )}

        {explanationText && (
          <div className="exp-body" style={{ fontSize: '0.8em', color: 'var(--text-muted)', marginBottom: 8, lineHeight: 1.4 }}>
            {explanationText}
          </div>
        )}

        <div style={{ marginBottom: 12 }}>{PreviewBody}</div>

        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
          <button onClick={() => void handleApprove(currentApproval, false)} disabled={isProcessing} type="button"
            style={btnStyle('#3fb950', '#3fb950', '#fff', isProcessing)}>
            {isProcessing ? 'Running…' : '✓ Yes'}
          </button>
          <button onClick={() => void handleApprove(currentApproval, true)} disabled={isProcessing} type="button"
            title={`Allow all ${currentApproval.category} this session`}
            style={btnStyle('#3fb950', 'transparent', '#3fb950', isProcessing)}>
            ✓ {allowAllLabel}
          </button>
          <button onClick={() => void handleDeny(currentApproval, false)} disabled={isProcessing} type="button"
            style={btnStyle('#f87171', 'transparent', '#f87171', isProcessing)}>
            ✕ No
          </button>
          <button onClick={() => void handleDeny(currentApproval, true)} disabled={isProcessing} type="button"
            title={`Block all ${currentApproval.category} this session`}
            style={btnStyle('#f87171', '#f87171', '#fff', isProcessing)}>
            ✕ {blockLabel}
          </button>
          {showUndo && (
            <button onClick={() => void handleUndo(currentApproval)} disabled={isProcessing} type="button"
              title="Undo this change"
              style={{ ...btnStyle('var(--border)', 'transparent', 'var(--text-muted)', isProcessing), marginLeft: 'auto' }}>
              ↺ Undo
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

/**
 * Pull a human-readable string from the explanation field. The security
 * plugin emits a structured object {riskLevel, explanation, reasoning, risk}
 * — surface the readable fields rather than dumping the whole dict.
 */
function extractExplanation(raw: PendingApproval['explanation']): string | null {
  if (raw == null) return null;
  if (typeof raw === 'string') return raw;
  const obj = raw as Record<string, unknown>;
  const parts: string[] = [];
  for (const key of ['explanation', 'reasoning', 'risk']) {
    const v = obj[key];
    if (typeof v === 'string' && v.trim()) parts.push(v.trim());
  }
  return parts.length ? parts.join('\n\n') : null;
}

function btnStyle(border: string, background: string, color: string, processing: boolean): React.CSSProperties {
  return {
    padding: '6px 14px',
    borderRadius: 6,
    border: `1px solid ${border}`,
    background,
    color,
    fontSize: '0.82em',
    fontWeight: 500,
    cursor: processing ? 'wait' : 'pointer',
    opacity: processing ? 0.7 : 1,
  };
}
