import { useCallback, useState } from 'react';
import { getChatStore, useActiveChatStore } from '@/stores/sessionRuntimes';
import type { PendingApproval } from '@/stores/chatStore';
import { answerApproval } from '@/hooks/chatStream/approvalLeg';
import { DiffPreview } from './previews/DiffPreview';
import { CommandPreview } from './previews/CommandPreview';
import { TextPreview } from './previews/TextPreview';
import { ICON } from './activityIcons';

/**
 * Generic approval banner. Renders one of three preview components based on
 * `currentApproval.preview`, with no per-action conditionals. Adding a new
 * approval-required tool on the backend requires zero changes here.
 */
export function ApprovalBanner() {
  const currentApproval = useActiveChatStore((s) => s.currentApproval);
  const approvalQueue = useActiveChatStore((s) => s.approvalQueue);
  // The sessions whose answer is still running. This one banner draws the
  // card of whichever session is open (ChatPanel is not remounted on a
  // session switch), and an answer runs until the resumed turn ends, so a
  // single flag held "Running…" on every other session's card for that long.
  const [answering, setAnswering] = useState<ReadonlySet<string>>(() => new Set());

  // One handler for every button. The leg binds the approval's OWNING session
  // store up front (the continuation can outlive a session switch) and keeps
  // that session busy from the click until the resumed turn has the stream,
  // so Stop, ESC and the composer all see it working (hooks/chatStream/approvalLeg).
  const handleAnswer = useCallback(async (approval: PendingApproval, accepted: boolean, forSession: boolean) => {
    const sessionId = approval.sessionId;
    setAnswering((prev) => new Set(prev).add(sessionId));
    const chat = getChatStore(sessionId);
    try {
      await answerApproval(approval, accepted, forSession);
    } finally {
      // Always recover the card UI. Skipping this on the failure path left
      // the session marked answering, which disabled every button on its NEXT
      // approval card ("Running...") until a reload, and left queued approvals
      // permanently unshown.
      if (!chat.getState().currentApproval) {
        chat.getState().showNextApproval();
      }
      setAnswering((prev) => {
        const next = new Set(prev);
        next.delete(sessionId);
        return next;
      });
    }
  }, []);

  if (!currentApproval) return null;

  const isProcessing = answering.has(currentApproval.sessionId);
  const payload = currentApproval.payload ?? {};
  const explanationParts = extractExplanation(currentApproval.explanation);
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
            previewError={payload.command_preview_error as string | undefined}
          />
        );
      case 'text':
      default:
        return <TextPreview text={currentApproval.summary} />;
    }
  })();

  const allowAllLabel = `Yes, all ${currentApproval.category}`;
  const blockLabel = `Block ${currentApproval.category}`;
  // A hard floor (rm, a destructive GitHub call, a sandbox escalation, an MCP
  // server marked approve) is asked every time: the server checks it before
  // the session's remembered choice, so "Yes, all" and "Block" would do
  // nothing for it. The card offers only this one answer and says why.
  const offerSessionChoice = !currentApproval.alwaysAsks;

  return (
    <div className="chat-msg-wrap assistant-wrap">
      <div className="ws-approval-card ws-callout-warning" style={{ padding: '12px 16px' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
          <span className="ws-callout-label">
            {ICON.lock}Approval needed
          </span>
          {riskLabel && (
            <span className={`risk-badge risk-${riskLabel}`} style={{ fontSize: '0.75em', padding: '2px 6px' }}>{riskLabel}</span>
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

        {explanationParts.length > 0 && (
          <div className="exp-body" style={{ fontSize: '0.8em', color: 'var(--text-muted)', marginBottom: 8, lineHeight: 1.4 }}>
            {explanationParts.map((part, i) => (
              <p key={i} style={{ margin: i === 0 ? 0 : '6px 0 0' }}>{part}</p>
            ))}
          </div>
        )}

        <div style={{ marginBottom: 12 }}>{PreviewBody}</div>

        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
          <button onClick={() => void handleAnswer(currentApproval, true, false)} disabled={isProcessing} type="button"
            style={btnStyle('var(--success)', 'var(--success)', 'var(--on-success)', isProcessing)}>
            {isProcessing ? 'Running…' : <>{ICON.check}Yes</>}
          </button>
          {offerSessionChoice && (
            <button onClick={() => void handleAnswer(currentApproval, true, true)} disabled={isProcessing} type="button"
              title={`Allow all ${currentApproval.category} this session`}
              style={btnStyle('var(--success)', 'transparent', 'var(--success)', isProcessing)}>
              {ICON.check}{allowAllLabel}
            </button>
          )}
          <button onClick={() => void handleAnswer(currentApproval, false, false)} disabled={isProcessing} type="button"
            style={btnStyle('var(--danger)', 'transparent', 'var(--danger)', isProcessing)}>
            {ICON.x}No
          </button>
          {offerSessionChoice ? (
            <button onClick={() => void handleAnswer(currentApproval, false, true)} disabled={isProcessing} type="button"
              title={`Block all ${currentApproval.category} this session`}
              style={btnStyle('var(--danger)', 'var(--danger)', '#fff', isProcessing)}>
              {ICON.x}{blockLabel}
            </button>
          ) : (
            <span style={{ fontSize: '0.78em', color: 'var(--text-muted)' }}>
              This action always asks.
            </span>
          )}
        </div>
      </div>
    </div>
  );
}

/**
 * Pull the human-readable paragraphs from the explanation field. The security
 * plugin emits a structured object {riskLevel, explanation, reasoning, risk}:
 * each readable field is its own paragraph, never one run-on line.
 */
function extractExplanation(raw: PendingApproval['explanation']): string[] {
  if (raw == null) return [];
  if (typeof raw === 'string') {
    return raw.split(/\n\s*\n/).map((p) => p.trim()).filter(Boolean);
  }
  const obj = raw as Record<string, unknown>;
  const parts: string[] = [];
  for (const key of ['explanation', 'reasoning', 'risk']) {
    const v = obj[key];
    if (typeof v === 'string' && v.trim()) parts.push(v.trim());
  }
  return parts;
}

function btnStyle(border: string, background: string, color: string, processing: boolean): React.CSSProperties {
  return {
    display: 'inline-flex',
    alignItems: 'center',
    gap: 6,
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
