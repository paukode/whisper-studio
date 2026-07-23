import React, { useEffect } from 'react';
import AppProviders from '@/providers/AppProviders';
import AppShell from '@/components/layout/AppShell';
import { ErrorBoundary } from '@/components/common/ErrorBoundary';
import { useBroadcastChannel } from '@/hooks/useBroadcastChannel';

/**
 * Inner component that uses hooks for tab detection.
 */
const AppInner: React.FC = () => {
  const { isOtherTabOpen } = useBroadcastChannel();

  useEffect(() => {
    if (isOtherTabOpen) {
      console.warn('Another Whisper Studio tab is already open.');
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
