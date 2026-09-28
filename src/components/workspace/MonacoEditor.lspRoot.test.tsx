import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render } from '@testing-library/react';

const lsp = vi.hoisted(() => ({
  useLsp: vi.fn(() => ({ status: 'off', active: false, failure: null })),
}));
vi.mock('@/hooks/useLsp', () => lsp);
vi.mock('@/hooks/useMonaco', () => ({ useMonaco: () => ({ setEditorTheme: () => {} }) }));
vi.mock('@/providers/ThemeProvider', () => ({ useTheme: () => ({ resolvedTheme: 'dark' }) }));
vi.mock('@monaco-editor/react', () => ({ default: () => null }));

import { MonacoEditor } from './MonacoEditor';
import { useUIStore } from '@/stores/uiStore';

const enabledFor = (workspaceRoot: string | null | undefined) => {
  lsp.useLsp.mockClear();
  render(
    <MonacoEditor filePath="app.py" content="" language="python" workspaceRoot={workspaceRoot} />,
  );
  const calls = lsp.useLsp.mock.calls as unknown as [{ enabled: boolean }][];
  return calls[calls.length - 1][0].enabled;
};

describe('the editor language server follows the tab root', () => {
  beforeEach(() => useUIStore.getState().setWsConnected(true, '/ws/B'));

  it('runs for a tab of the connected root', () => {
    expect(enabledFor('/ws/B')).toBe(true);
  });

  it('never analyses a tab kept from another root as this workspace file', () => {
    expect(enabledFor('/ws/A')).toBe(false);
  });

  it('stays off for a tab opened with no workspace connected', () => {
    expect(enabledFor(null)).toBe(false);
  });
});
