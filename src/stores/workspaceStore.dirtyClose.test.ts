import { describe, it, expect, vi, beforeEach } from 'vitest';
import type { EditorTab } from '@/types/workspace';

// confirmCloseTab prompts via dialogConfirm (from uiStore) only when the tab
// has unsaved edits, then closes on confirm. Mock the dialog so the store logic
// can be exercised headlessly.
const { dialogConfirmMock } = vi.hoisted(() => ({ dialogConfirmMock: vi.fn() }));
vi.mock('./uiStore', () => ({ dialogConfirm: dialogConfirmMock }));

import { useWorkspaceStore } from './workspaceStore';

function tab(path: string, isDirty: boolean): EditorTab {
  return {
    path,
    language: 'plaintext',
    content: isDirty ? 'edited' : 'saved',
    originalContent: 'saved',
    isDirty,
  };
}

beforeEach(() => {
  dialogConfirmMock.mockReset();
  useWorkspaceStore.setState({ fileTree: [], editorTabs: [], activeTabPath: null });
});

describe('workspaceStore.confirmCloseTab', () => {
  it('closes a clean tab immediately, without prompting', async () => {
    useWorkspaceStore.setState({
      editorTabs: [tab('/w/clean.ts', false)],
      activeTabPath: '/w/clean.ts',
    });

    await useWorkspaceStore.getState().confirmCloseTab('/w/clean.ts');

    expect(dialogConfirmMock).not.toHaveBeenCalled();
    expect(useWorkspaceStore.getState().editorTabs).toHaveLength(0);
    expect(useWorkspaceStore.getState().activeTabPath).toBeNull();
  });

  it('prompts for a dirty tab and closes it once the user confirms', async () => {
    dialogConfirmMock.mockResolvedValue(true);
    useWorkspaceStore.setState({
      editorTabs: [tab('/w/dirty.ts', true)],
      activeTabPath: '/w/dirty.ts',
    });

    await useWorkspaceStore.getState().confirmCloseTab('/w/dirty.ts');

    expect(dialogConfirmMock).toHaveBeenCalledTimes(1);
    expect(dialogConfirmMock).toHaveBeenCalledWith(
      expect.objectContaining({ danger: true, confirmText: 'Discard' }),
    );
    expect(useWorkspaceStore.getState().editorTabs).toHaveLength(0);
  });

  it('prompts for a dirty tab and keeps it when the user cancels', async () => {
    // dialogConfirm resolves null on cancel.
    dialogConfirmMock.mockResolvedValue(null);
    useWorkspaceStore.setState({
      editorTabs: [tab('/w/dirty.ts', true)],
      activeTabPath: '/w/dirty.ts',
    });

    await useWorkspaceStore.getState().confirmCloseTab('/w/dirty.ts');

    expect(dialogConfirmMock).toHaveBeenCalledTimes(1);
    expect(useWorkspaceStore.getState().editorTabs).toHaveLength(1);
    expect(useWorkspaceStore.getState().editorTabs[0].path).toBe('/w/dirty.ts');
  });

  it('puts the file basename in the discard prompt', async () => {
    dialogConfirmMock.mockResolvedValue(true);
    useWorkspaceStore.setState({
      editorTabs: [tab('/deep/nested/report.md', true)],
      activeTabPath: '/deep/nested/report.md',
    });

    await useWorkspaceStore.getState().confirmCloseTab('/deep/nested/report.md');

    expect(dialogConfirmMock).toHaveBeenCalledWith(
      expect.objectContaining({ message: expect.stringContaining('report.md') }),
    );
  });

  it('is a no-op for an unknown path (nothing to prompt or close)', async () => {
    useWorkspaceStore.setState({
      editorTabs: [tab('/w/a.ts', false)],
      activeTabPath: '/w/a.ts',
    });

    await useWorkspaceStore.getState().confirmCloseTab('/w/missing.ts');

    expect(dialogConfirmMock).not.toHaveBeenCalled();
    expect(useWorkspaceStore.getState().editorTabs).toHaveLength(1);
  });
});
