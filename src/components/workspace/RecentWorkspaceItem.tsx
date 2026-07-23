import React from 'react';
import { useUIStore } from '@/stores/uiStore';
import type { IndexStatus, IndexSettings } from '@/api/workspace';
import { IndexSettingsPanel } from './IndexSettingsPanel';
import { formatRelative } from './workspaceConnectHelpers';

/** One dense recent-workspace row (name · path · status badge · actions + the
 *  expandable ⋯ settings menu), shared by the Indexed and Recent groups so the
 *  badge / remove / menu logic lives in one place. */
export interface RecentWorkspaceItemProps {
  wsPath: string;
  status: IndexStatus | undefined;
  stopping: boolean;
  settings: IndexSettings | undefined;
  menuOpen: boolean;
  agentSupported: boolean;
  onSelect: () => void;
  onRemoveRecent: (e: React.MouseEvent) => void;
  onToggleMenu: (e: React.MouseEvent) => void;
  onStop: (e: React.MouseEvent) => void;
  onReindex: (e: React.MouseEvent) => void;
  onRemoveIndex: (e: React.MouseEvent) => void;
  onChange: React.ComponentProps<typeof IndexSettingsPanel>['onChange'];
  onEngineChange: React.ComponentProps<typeof IndexSettingsPanel>['onEngineChange'];
}

export const RecentWorkspaceItem: React.FC<RecentWorkspaceItemProps> = ({
  wsPath,
  status: st,
  stopping,
  settings: s,
  menuOpen,
  agentSupported,
  onSelect,
  onRemoveRecent,
  onToggleMenu,
  onStop,
  onReindex,
  onRemoveIndex,
  onChange,
  onEngineChange,
}) => {
  const name = wsPath.split('/').filter(Boolean).pop() || wsPath;
  const building = !!st?.building;
  const indexed = !!st?.indexed;
  const isStopping = building && stopping;
  let badge = 'Not indexed';
  if (building) {
    if (isStopping) {
      badge = 'Stopping…';
    } else {
      const p = st?.progress;
      badge = p && p.total ? `Indexing ${Math.round((p.done / p.total) * 100)}%` : 'Indexing…';
    }
  } else if (indexed) {
    badge = st?.last_indexed_at ? `Indexed ${formatRelative(st.last_indexed_at)}` : 'Indexed';
  }
  return (
    <div className="ws-recent-item-wrap">
      <div className="ws-recent-item" onClick={onSelect}>
        <div className="ws-recent-main">
          <span className="ws-recent-name">{name}</span>
          <span className="ws-recent-path">{wsPath}</span>
        </div>
        <span
          className={`ws-recent-status${indexed ? ' is-indexed' : ''}${building ? ' is-building' : ''}`}
        >
          {badge}
        </span>
        {!indexed && !building && (
          <button
            type="button"
            className="ws-recent-remove"
            title="Remove from recent workspaces"
            aria-label="Remove from recent workspaces"
            onClick={onRemoveRecent}
          >
            <svg
              width="13"
              height="13"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2.2"
              strokeLinecap="round"
            >
              <line x1="6" y1="6" x2="18" y2="18" />
              <line x1="18" y1="6" x2="6" y2="18" />
            </svg>
          </button>
        )}
        <button
          type="button"
          className={`ws-recent-menu-btn${menuOpen ? ' is-open' : ''}`}
          title="Folder options"
          aria-label="Folder options"
          onClick={onToggleMenu}
        >
          <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
            <circle cx="5" cy="12" r="1.7" />
            <circle cx="12" cy="12" r="1.7" />
            <circle cx="19" cy="12" r="1.7" />
          </svg>
        </button>
      </div>
      {menuOpen && (
        <div className="ws-recent-menu" onClick={(e) => e.stopPropagation()}>
          {building ? (
            <button
              type="button"
              className="ws-menu-item danger"
              disabled={isStopping}
              onClick={onStop}
            >
              {isStopping ? 'Stopping…' : 'Stop indexing'}
            </button>
          ) : (
            <button type="button" className="ws-menu-item" onClick={onReindex}>
              {indexed ? 'Reindex now' : 'Index this folder'}
            </button>
          )}
          {!s && <div className="ws-menu-loading">Loading settings…</div>}
          {s && (
            <>
              {!indexed && !building && (
                <div className="ws-menu-hint">These apply when you index this folder.</div>
              )}
              <IndexSettingsPanel
                settings={s}
                agentSupported={agentSupported}
                onChange={onChange}
                onEngineChange={onEngineChange}
              />
              {indexed && !building && (
                <button
                  type="button"
                  className="ws-menu-item"
                  onClick={(e) => {
                    e.stopPropagation();
                    useUIStore.getState().openIndexGraph(wsPath);
                  }}
                >
                  View relationship graph
                </button>
              )}
              {indexed && !building && (
                <button type="button" className="ws-menu-item danger" onClick={onRemoveIndex}>
                  Remove index
                </button>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
};
