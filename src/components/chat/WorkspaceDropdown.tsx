import React from 'react';

interface WorkspaceDropdownProps {
  connected: boolean;
  open: boolean;
  recentPaths: string[];
  onToggle: () => void;
  onBrowse: () => void;
  onConnect: (path: string) => void;
}

/**
 * Workspace connect / recents dropdown for the chat toolbar. Extracted from
 * ChatInput.tsx to keep that file under the size budget. Presentational —
 * connecting and the native folder browser stay in the parent via callbacks.
 */
export const WorkspaceDropdown: React.FC<WorkspaceDropdownProps> = ({
  connected, open, recentPaths, onToggle, onBrowse, onConnect,
}) => (
  <div className="toolbar-dropdown-wrap">
    <button
      type="button"
      className={`toolbar-btn${connected ? ' active' : ''}`}
      id="toolbarWorkspaceBtn"
      title="Connect workspace"
      onClick={onToggle}
    >
      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>
      </svg>
      <span id="toolbarWsLabel">Workspace</span>
    </button>
    <div className="toolbar-dropdown" style={{ display: open ? 'block' : 'none' }}>
      <div className="toolbar-dropdown-item toolbar-dropdown-manage" data-testid="workspace-option-browse" onClick={onBrowse}>
        <span className="toolbar-dropdown-item-name">Browse…</span>
        <span className="toolbar-dropdown-item-desc">Folder browser with recents and sorting</span>
      </div>
      {recentPaths.length > 0 && (
        <>
          <div className="toolbar-dropdown-header">Recent</div>
          {recentPaths.map((p) => {
            const name = p.split('/').filter(Boolean).pop() || p;
            return (
              <div key={p} className="toolbar-dropdown-item" title={p} data-testid={`workspace-option-${p}`} onClick={() => onConnect(p)}>
                <span className="toolbar-dropdown-item-name">{name}</span>
                <span className="toolbar-dropdown-item-desc">{p}</span>
              </div>
            );
          })}
        </>
      )}
      {recentPaths.length === 0 && (
        <div className="toolbar-dropdown-empty">No recent workspaces</div>
      )}
    </div>
  </div>
);
