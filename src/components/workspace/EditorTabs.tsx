import React, { useCallback, useEffect } from 'react';
import { useWorkspaceStore } from '@/stores/workspaceStore';
import { getFileIcon } from '@/utils/fileIcons';

/**
 * Tab bar matching the original _ideRenderTabs():
 *   icon | name | dirty dot | diff/src toggle (if dirty) | close button
 * Plus Ctrl+Tab cycling.
 */
export const EditorTabs: React.FC = () => {
  const editorTabs = useWorkspaceStore((s) => s.editorTabs);
  const activeTabPath = useWorkspaceStore((s) => s.activeTabPath);
  const setActiveTab = useWorkspaceStore((s) => s.setActiveTab);
  const confirmCloseTab = useWorkspaceStore((s) => s.confirmCloseTab);
  const toggleDiffMode = useWorkspaceStore((s) => s.toggleDiffMode);

  const handleTabClick = useCallback(
    (path: string) => {
      setActiveTab(path);
    },
    [setActiveTab],
  );

  const handleTabClose = useCallback(
    (e: React.MouseEvent, path: string) => {
      e.stopPropagation();
      // Prompts to discard first when the tab has unsaved edits.
      void confirmCloseTab(path);
    },
    [confirmCloseTab],
  );

  const handleDiffToggle = useCallback(
    (e: React.MouseEvent, path: string) => {
      e.stopPropagation();
      toggleDiffMode(path);
    },
    [toggleDiffMode],
  );

  // Ctrl+Tab cycling through open tabs
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.ctrlKey && e.key === 'Tab') {
        e.preventDefault();
        if (editorTabs.length <= 1) return;

        const currentIndex = editorTabs.findIndex((t) => t.path === activeTabPath);
        const nextIndex = e.shiftKey
          ? (currentIndex - 1 + editorTabs.length) % editorTabs.length
          : (currentIndex + 1) % editorTabs.length;
        setActiveTab(editorTabs[nextIndex].path);
      }
    };

    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [editorTabs, activeTabPath, setActiveTab]);

  if (editorTabs.length === 0) return null;

  return (
    <div className="ws-tab-bar" id="wsTabBar" role="tablist" aria-label="Open files">
      {editorTabs.map((tab) => {
        // Diff tabs use a synthetic "<left> ↔ <right>" path key — label them
        // with both basenames; everything else is a real file path.
        const baseName = (p: string) => p.split('/').pop() || p;
        const fileName = tab.viewerType === 'diff' && tab.comparePath
          ? `${baseName(tab.path.split(' ↔ ')[0])} ↔ ${baseName(tab.comparePath)}`
          : tab.path.split('/').pop() ?? tab.path;
        const isActive = tab.path === activeTabPath;
        const icon = tab.viewerType === 'diff' ? '↔' : getFileIcon(tab.path);

        return (
          <div
            key={tab.path}
            className={`ws-tab${isActive ? ' active' : ''}`}
            role="tab"
            aria-selected={isActive}
            tabIndex={isActive ? 0 : -1}
            data-path={tab.path}
            onClick={() => handleTabClick(tab.path)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault();
                handleTabClick(tab.path);
              }
            }}
            title={tab.path}
          >
            <span className="ws-tab-icon">{icon}</span>
            <span className="ws-tab-name">{fileName}</span>
            {tab.isDirty && (
              <>
                <span className="ws-tab-dirty" title="Modified" />
                <button
                  className="ws-tab-diff-btn"
                  onClick={(e) => handleDiffToggle(e, tab.path)}
                  title={tab.diffMode ? 'Show source' : 'Show diff'}
                  type="button"
                >
                  {tab.diffMode ? 'src' : 'diff'}
                </button>
              </>
            )}
            <button
              className="ws-tab-close"
              onClick={(e) => handleTabClose(e, tab.path)}
              aria-label={`Close ${fileName}`}
              type="button"
            >
              ×
            </button>
          </div>
        );
      })}
    </div>
  );
};
