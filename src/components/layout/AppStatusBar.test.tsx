/**
 * AppStatusBar renders the glanceable workspace state from existing stores.
 * git status and its SSE are mocked; the stores are driven directly.
 */
import { render } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { describe, it, expect, beforeEach, vi } from 'vitest';

const h = vi.hoisted(() => ({
  git: null as null | {
    branch: string;
    clean: boolean;
    changed: number;
    untracked: number;
    ahead: number;
    behind: number;
  },
}));

vi.mock('@/hooks/useGitStatusBar', () => ({
  useGitStatusBar: () => h.git,
}));

import { AppStatusBar } from './AppStatusBar';
import { useSettingsStore } from '@/stores/settingsStore';
import { useBackgroundTaskStore } from '@/stores/backgroundTaskStore';
import { getChatStore } from '@/stores/sessionRuntimes';
import { useSessionStore } from '@/stores/sessionStore';

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

const SID = 'asb-test-session';

function primeActiveSession() {
  // getChatStore lazily creates the runtime; setting currentSessionId makes
  // useActiveChatStore bind to it.
  const store = getChatStore(SID);
  useSessionStore.setState({ currentSessionId: SID });
  return store;
}

describe('AppStatusBar', () => {
  beforeEach(() => {
    h.git = null;
    useSettingsStore.setState({
      selectedModel: 'opus4.8',
      models: [{ key: 'opus4.8', name: 'Claude Opus 4.8' }],
      effortLevel: 'high',
    });
    useBackgroundTaskStore.setState({ tasks: {}, runningCount: 0, panelOpen: false });
  });

  it('shows the model label and effort', () => {
    primeActiveSession();
    const { getByText } = render(<AppStatusBar />, { wrapper });
    expect(getByText('Claude Opus 4.8')).toBeTruthy();
    expect(getByText('high')).toBeTruthy();
  });

  it('renders git branch with dirty count and ahead/behind', () => {
    h.git = { branch: 'feature/x', clean: false, changed: 2, untracked: 1, ahead: 3, behind: 1 };
    primeActiveSession();
    const { getByText, container } = render(<AppStatusBar />, { wrapper });
    expect(getByText('feature/x')).toBeTruthy();
    expect(getByText('±3')).toBeTruthy(); // 2 changed + 1 untracked
    expect(getByText('↑3')).toBeTruthy();
    expect(getByText('↓1')).toBeTruthy();
    expect(container.querySelector('.asb-git.dirty')).toBeTruthy();
  });

  it('renders a context meter from live usage', () => {
    const store = primeActiveSession();
    store.getState().setUsage(1000, 200, 0.05, 100_000, 200_000);
    const { getByText, container } = render(<AppStatusBar />, { wrapper });
    expect(getByText('50%')).toBeTruthy();
    const fill = container.querySelector('.asb-ctx-fill') as HTMLElement;
    expect(fill.style.width).toBe('50%');
  });

  it('marks the context meter hot at >=80%', () => {
    const store = primeActiveSession();
    store.getState().setUsage(1, 1, 0.01, 170_000, 200_000);
    const { container } = render(<AppStatusBar />, { wrapper });
    expect(container.querySelector('.asb-ctx-fill.hot')).toBeTruthy();
  });

  it('shows a background-task pill that opens the panel', () => {
    primeActiveSession();
    useBackgroundTaskStore.setState({ runningCount: 2 });
    const setPanelOpen = vi.fn();
    useBackgroundTaskStore.setState({ setPanelOpen });
    const { getByText } = render(<AppStatusBar />, { wrapper });
    const pill = getByText('2 tasks');
    expect(pill).toBeTruthy();
    pill.click();
    expect(setPanelOpen).toHaveBeenCalledWith(true);
  });

  it('hides the git segment when no workspace / not a repo', () => {
    h.git = null;
    primeActiveSession();
    const { container } = render(<AppStatusBar />, { wrapper });
    expect(container.querySelector('.asb-git')).toBeNull();
  });
});
