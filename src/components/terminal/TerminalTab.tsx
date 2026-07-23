import { forwardRef, useEffect, useImperativeHandle } from 'react';
import { useTerminal } from '@/hooks/useTerminal';

export interface TerminalTabProps {
  sessionId: string;
  isActive: boolean;
  initialCols: number;
  initialRows: number;
}

export interface TerminalTabHandle {
  refit: () => void;
}

export const TerminalTab = forwardRef<TerminalTabHandle, TerminalTabProps>(function TerminalTab(
  { sessionId, isActive, initialCols, initialRows },
  ref,
) {
  const { terminalRef, refit } = useTerminal({ sessionId, initialCols, initialRows });

  useImperativeHandle(ref, () => ({ refit }), [refit]);

  // Hidden tabs are display:none and unmeasurable, so they keep stale
  // dims while the panel changes size. Catch up the moment this tab
  // becomes visible; refit() rAFs internally and no-ops when unchanged.
  useEffect(() => {
    if (isActive) refit();
  }, [isActive, refit]);

  return (
    <div
      className={`ws-terminal-instance${isActive ? ' active' : ''}`}
      role="tabpanel"
      aria-hidden={!isActive}
    >
      <div ref={terminalRef} style={{ position: 'absolute', inset: 0 }} />
    </div>
  );
});
