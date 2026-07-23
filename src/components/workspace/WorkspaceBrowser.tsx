import React from 'react';
import { SORT_LABELS, formatMtime, type BrowseEntry, type SortMode } from './workspaceConnectHelpers';

/** In-page folder browser panel: up-nav + sort + a folders/files listing.
 *  Purely presentational — the dialog owns the browse state and passes the
 *  sorted lists plus navigation callbacks in. */
export interface WorkspaceBrowserProps {
  current: string;
  parent: string | null;
  entries: BrowseEntry[];
  files: BrowseEntry[];
  fileTotal: number;
  loading: boolean;
  sortMode: SortMode;
  onSort: (mode: SortMode) => void;
  onUp: () => void;
  onBrowseDir: (dir: string) => void;
}

export const WorkspaceBrowser: React.FC<WorkspaceBrowserProps> = ({
  current,
  parent,
  entries,
  files,
  fileTotal,
  loading,
  sortMode,
  onSort,
  onUp,
  onBrowseDir,
}) => {
  return (
    <div className="ws-browser">
      <div className="ws-browser-header">
        <button className="btn-icon ws-browser-up" type="button" onClick={onUp} disabled={!parent}>
          ← Up
        </button>
        <span className="ws-browser-current">{current}</span>
        <select
          className="ws-browser-sort"
          title="Sort folders"
          value={sortMode}
          onChange={(e) => onSort(e.target.value as SortMode)}
        >
          {(Object.keys(SORT_LABELS) as SortMode[]).map((m) => (
            <option key={m} value={m}>
              {SORT_LABELS[m]}
            </option>
          ))}
        </select>
      </div>
      <div className="ws-browser-list">
        {loading ? (
          <div className="ws-browser-item" aria-busy="true">
            <span className="skeleton skeleton-text" style={{ width: '80%' }} />
          </div>
        ) : entries.length === 0 && files.length === 0 ? (
          <div className="ws-browser-empty">This folder is empty</div>
        ) : (
          <>
            {entries.length > 0 && (
              <>
                <div className="ws-browser-group">Folders</div>
                {entries.map((entry) => (
                  <div
                    key={`d-${entry.name}`}
                    className="ws-browser-item"
                    onClick={() => onBrowseDir(entry.name)}
                  >
                    <svg
                      width="13"
                      height="13"
                      viewBox="0 0 24 24"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="2"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                      aria-hidden="true"
                    >
                      <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" />
                    </svg>
                    <span className="ws-browser-item-name">{entry.name}</span>
                    {entry.mtime > 0 && (
                      <span className="ws-browser-item-date">{formatMtime(entry.mtime)}</span>
                    )}
                    <svg
                      className="ws-browser-item-chev"
                      width="13"
                      height="13"
                      viewBox="0 0 24 24"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="2"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                      aria-hidden="true"
                    >
                      <polyline points="9 18 15 12 9 6" />
                    </svg>
                  </div>
                ))}
              </>
            )}
            {files.length > 0 && (
              <>
                <div className="ws-browser-group">
                  Files
                  {fileTotal > files.length && (
                    <span className="ws-browser-group-note">
                      showing {files.length} of {fileTotal}
                    </span>
                  )}
                </div>
                {files.map((file) => (
                  <div key={`f-${file.name}`} className="ws-browser-item is-file">
                    <svg
                      width="13"
                      height="13"
                      viewBox="0 0 24 24"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="2"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                      aria-hidden="true"
                    >
                      <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
                      <polyline points="14 2 14 8 20 8" />
                    </svg>
                    <span className="ws-browser-item-name">{file.name}</span>
                    {file.mtime > 0 && (
                      <span className="ws-browser-item-date">{formatMtime(file.mtime)}</span>
                    )}
                  </div>
                ))}
              </>
            )}
          </>
        )}
      </div>
    </div>
  );
};
