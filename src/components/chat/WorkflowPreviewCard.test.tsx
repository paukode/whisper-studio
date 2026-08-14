import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, act } from '@testing-library/react';

const { launchRunMock } = vi.hoisted(() => ({
  launchRunMock: vi.fn().mockResolvedValue({ run_id: 'r1', status: 'running', trusted_as: null }),
}));
vi.mock('@/api/workflows', () => ({ launchRun: launchRunMock }));
vi.mock('@/components/chat/WorkflowRunCard', () => ({ WorkflowRunCard: () => null }));

import { WorkflowPreviewCard } from './WorkflowPreviewCard';
import { useSessionStore } from '@/stores/sessionStore';

function basePreview(overrides: Partial<Parameters<typeof WorkflowPreviewCard>[0]['preview']> = {}) {
  return {
    script: "export const meta = { name: 't', description: 'd', phases: ['a'] }\nreturn 1\n",
    name: 't',
    phases: ['a'],
    budget_tokens: 600000,
    model_id: 'global.anthropic.claude-opus-5',
    effort_label: 'ultracode',
    ...overrides,
  };
}

describe('WorkflowPreviewCard workspace_path wiring', () => {
  beforeEach(() => {
    launchRunMock.mockClear();
    useSessionStore.setState({ currentSessionId: 'sess-1' });
    localStorage.clear();
  });

  it('sends the resolved workspace_path when the preview pinned a folder', async () => {
    render(<WorkflowPreviewCard preview={basePreview({ workspace_path: '/Users/me/project1' })} />);
    await act(async () => {
      fireEvent.click(screen.getByText('Approve & run'));
      await Promise.resolve();
    });
    expect(launchRunMock).toHaveBeenCalledWith(
      expect.objectContaining({ workspace_path: '/Users/me/project1' }),
    );
  });

  it('sends an EXPLICIT empty string, not a dropped key, when the preview had nothing to pin', async () => {
    // The exact regression: preview.workspace_path === "" (the tool genuinely
    // resolved to nothing pinned) must NOT collapse to `undefined` — an
    // omitted key tells the server "no card was involved, snapshot whatever
    // is connected now", silently pinning the run to a folder the card never
    // showed the user.
    render(<WorkflowPreviewCard preview={basePreview({ workspace_path: '' })} />);
    await act(async () => {
      fireEvent.click(screen.getByText('Approve & run'));
      await Promise.resolve();
    });
    const call = launchRunMock.mock.calls[0][0];
    expect('workspace_path' in call).toBe(true);
    expect(call.workspace_path).toBe('');
  });

  it('omits the key entirely for a legacy preview with no workspace_path field at all', async () => {
    const legacyPreview = basePreview();
    delete (legacyPreview as { workspace_path?: string }).workspace_path;
    render(<WorkflowPreviewCard preview={legacyPreview} />);
    await act(async () => {
      fireEvent.click(screen.getByText('Approve & run'));
      await Promise.resolve();
    });
    const call = launchRunMock.mock.calls[0][0];
    expect(call.workspace_path).toBeUndefined();
  });
});
