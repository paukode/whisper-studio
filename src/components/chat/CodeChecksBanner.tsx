import { useCallback, useState } from 'react';
import { post } from '@/api/client';
import { sendChatTurn } from '@/hooks/useChatStream';
import { getChatStore, useActiveChatStore } from '@/stores/sessionRuntimes';
import { useSessionStore } from '@/stores/sessionStore';
import { useUIStore } from '@/stores/uiStore';
import { CodeChecksResponseSchema, type CodeChecksResponse } from '@/types/schemas/codeTools.schema';
import { ICON } from './activityIcons';

const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? '' : 's'}`;

/**
 * Asks whether to run the code checks (ruff, ESLint) on the code files the
 * last turn changed. Shown only while Settings > Code tools has the checks
 * switched on, and only after a turn that changed code.
 *
 * A yes runs the tools in the app, outside the model. A clean run ends here
 * with a toast and costs no tokens. Findings are sent to the assistant as the
 * user's next message, so the fix is an ordinary turn.
 */
export function CodeChecksBanner() {
  const offer = useActiveChatStore((s) => s.codeChecksOffer);
  const isStreaming = useActiveChatStore((s) => s.isStreaming);
  // Keyed by session, like ApprovalBanner's answering set: this one banner
  // draws every session's offer.
  const [running, setRunning] = useState<ReadonlySet<string>>(() => new Set());

  const handleRun = useCallback(async () => {
    if (!offer) return;
    const { sessionId, files } = offer;
    const chat = getChatStore(sessionId);
    const toast = useUIStore.getState().addToast;
    setRunning((prev) => new Set(prev).add(sessionId));
    try {
      const res = await post<CodeChecksResponse>(
        '/api/code-tools/check',
        { files },
        { schema: CodeChecksResponseSchema },
      );
      const unchecked = res.results.filter((r) => r.issues === null);
      const notChecked = unchecked.length
        ? ` ${plural(unchecked.length, 'file')} could not be checked: ${unchecked[0].text}`
        : '';
      if (res.issues === 0) {
        chat.getState().setCodeChecksOffer(null);
        const checked = res.results.length - unchecked.length;
        toast({
          type: unchecked.length ? 'info' : 'success',
          message: `Code checks: no issues in ${plural(checked, 'file')}.${notChecked}`,
          duration: unchecked.length ? 8000 : 4000,
          key: 'code-checks-result',
        });
        return;
      }
      // The findings go to the session that owns them. The send works on the
      // session in view, so a user who moved on keeps the offer to run again.
      if (useSessionStore.getState().currentSessionId !== sessionId) {
        toast({
          type: 'info',
          message: `Code checks found ${plural(res.issues, 'issue')}. Open that session and run the checks again to send them to the assistant.`,
          duration: 8000,
          key: 'code-checks-result',
        });
        return;
      }
      chat.getState().setCodeChecksOffer(null);
      await sendChatTurn(res.report);
    } catch (err) {
      toast({
        type: 'error',
        message: `The code checks could not run: ${err instanceof Error ? err.message : String(err)}`,
        duration: 5000,
        key: 'code-checks-error',
      });
    } finally {
      setRunning((prev) => {
        const next = new Set(prev);
        next.delete(sessionId);
        return next;
      });
    }
  }, [offer]);

  if (!offer || isStreaming) return null;

  const isRunning = running.has(offer.sessionId);

  return (
    <div className="chat-msg-wrap assistant-wrap">
      <div
        className="ws-approval-card ws-callout-info"
        style={{ padding: '12px 16px' }}
        data-testid="code-checks-banner"
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
          <span className="ws-callout-label">
            {ICON.shield}
            Code checks
          </span>
        </div>
        <div style={{ marginBottom: 12, fontSize: '0.85em', lineHeight: 1.4 }}>
          This turn changed {plural(offer.files.length, 'code file')}. Run ruff and ESLint on{' '}
          {offer.files.length === 1 ? 'it' : 'them'}? Any issues go back to the assistant to fix.
        </div>
        <div style={{ display: 'flex', gap: 8 }}>
          <button
            type="button"
            className="btn btn-primary btn-sm"
            onClick={() => void handleRun()}
            disabled={isRunning}
          >
            {isRunning ? 'Running…' : 'Run checks'}
          </button>
          <button
            type="button"
            className="btn btn-sm"
            onClick={() => getChatStore(offer.sessionId).getState().setCodeChecksOffer(null)}
            disabled={isRunning}
          >
            Skip
          </button>
        </div>
      </div>
    </div>
  );
}
