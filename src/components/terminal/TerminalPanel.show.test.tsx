/**
 * The assistant runs a command in a visible terminal (server/terminal.py
 * sends terminal_show): the panel opens on that terminal's tab, adding a tab
 * for a terminal it does not show yet, and never starts a shell of its own
 * for it. The panel stays live, so AppShell keeps it (and its shells)
 * mounted once the request clears.
 */
import { act, render } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { forwardRef } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const createTerminalSession = vi.hoisted(() => vi.fn());

vi.mock('@/api/terminal', () => ({
  createTerminalSession,
  deleteTerminalSession: vi.fn().mockResolvedValue(undefined),
}));
vi.mock('@/api/git', () => ({
  getGitBranch: vi.fn().mockResolvedValue({ branch: 'main' }),
  getGitWorktrees: vi.fn().mockResolvedValue({ worktrees: [] }),
  addGitWorktree: vi.fn(),
  removeGitWorktree: vi.fn(),
  getWorktreeSession: vi.fn().mockResolvedValue(null),
}));
vi.mock('@/hooks/useGitChanged', () => ({ useGitChanged: () => {} }));
vi.mock('@/hooks/useTerminal', () => ({ measureCellGrid: () => ({ cols: 80, rows: 24 }) }));
vi.mock('./TerminalTab', () => ({
  TerminalTab: forwardRef<unknown, { sessionId: string; isActive: boolean }>(function TerminalTab(
    { sessionId, isActive },
    _ref,
  ) {
    return <div data-testid={`tab-${sessionId}`} data-active={String(isActive)} />;
  }),
}));

import { TerminalPanel } from './TerminalPanel';
import { useUIStore } from '@/stores/uiStore';

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <TerminalPanel />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  createTerminalSession.mockReset();
  useUIStore.setState({ terminalShow: null, terminalLive: false });
});
afterEach(() => vi.clearAllMocks());

describe('TerminalPanel and terminal_show', () => {
  it('opens on the terminal the assistant uses, without a shell of its own', async () => {
    const view = mount();
    expect(view.container.querySelector('.ws-terminal-panel')).toBeNull();
    await act(async () => {
      useUIStore.getState().showTerminal('p1', '/w');
    });
    expect(view.container.querySelector('.ws-terminal-panel')).not.toBeNull();
    expect(view.getByTestId('tab-p1').dataset.active).toBe('true');
    expect(createTerminalSession).not.toHaveBeenCalled();
    expect(useUIStore.getState().terminalLive).toBe(true);
    expect(useUIStore.getState().terminalShow).toBeNull();
  });

  it('handles the request that mounted it, and switches tabs on the next', async () => {
    useUIStore.getState().showTerminal('p1', '/w');
    const view = mount();
    await act(async () => {
      await Promise.resolve();
    });
    expect(view.getByTestId('tab-p1').dataset.active).toBe('true');
    await act(async () => {
      useUIStore.getState().showTerminal('p2', '/w');
    });
    expect(view.getByTestId('tab-p2').dataset.active).toBe('true');
    expect(view.getByTestId('tab-p1').dataset.active).toBe('false');
    await act(async () => {
      useUIStore.getState().showTerminal('p1', '/w');
    });
    expect(view.getByTestId('tab-p1').dataset.active).toBe('true');
    expect(view.getAllByTestId(/^tab-/)).toHaveLength(2);
  });
});
