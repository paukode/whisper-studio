import React, { useEffect } from 'react';
import AppProviders from '@/providers/AppProviders';
import AppShell from '@/components/layout/AppShell';
import { ErrorBoundary } from '@/components/common/ErrorBoundary';
import { useBroadcastChannel } from '@/hooks/useBroadcastChannel';
import { useUIStore } from '@/stores/uiStore';

/**
 * Inner component that uses hooks for tab detection.
 */
const AppInner: React.FC = () => {
  const { isOtherTabOpen } = useBroadcastChannel();

  useEffect(() => {
    if (isOtherTabOpen) {
      // Two tabs share one backend session state; recording and live SSE
      // streams land in whichever tab owns them. Warn visibly, not just in
      // the console.
      useUIStore.getState().addToast({
        type: 'warning',
        message: 'Whisper Studio is already open in another tab. Two tabs can fight over recordings and live sessions.',
        key: 'multi-tab-warning',
        duration: 8000,
      });
    }
  }, [isOtherTabOpen]);

  return <AppShell />;
};

/**
 * Root application component.
 *
 * Composes AppProviders → ErrorBoundary → AppInner (with tab detection).
 * Config loading and session initialization happen inside AppShell.
 */
const App: React.FC = () => (
  <AppProviders>
    <ErrorBoundary label="Application">
      <AppInner />
    </ErrorBoundary>
  </AppProviders>
);

export default App;
