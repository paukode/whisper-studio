import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { get, put, post, del as apiDel } from '@/api/client';
import { ApiError } from '@/types/api';
import { useUIStore } from '@/stores/uiStore';

interface MemoryFileMeta {
  filename: string;
  name: string;
  description: string;
  type: string;
  scope: 'global' | 'project';
  mtime: string;
  size: number;
}

interface MemoryTier {
  scope: 'global' | 'project';
  files: MemoryFileMeta[];
  index: string;
}

interface MemoryListing {
  tiers: MemoryTier[];
  workspace_connected: boolean;
}

interface MemoryViewerModalProps {
  isOpen: boolean;
  onClose: () => void;
}

/**
 * Two-tier memory browser backed by /api/memory: Global (cross-workspace)
 * and Project (workspace-scoped) tabs, with view/edit/delete per file and
 * project -> global promotion. Distinct from MemoryEditorModal, which edits
 * the hand-written WHISPER.md; this browses the auto-memory store the
 * extraction agent maintains.
 */
export const MemoryViewerModal: React.FC<MemoryViewerModalProps> = ({ isOpen, onClose }) => {
  const [tab, setTab] = useState<'global' | 'project'>('global');
  const [selected, setSelected] = useState<string | null>(null);
  const [editContent, setEditContent] = useState('');
  const [status, setStatus] = useState('');
  const addToast = useUIStore((s) => s.addToast);
  const queryClient = useQueryClient();

  const { data: listing, isLoading } = useQuery({
    queryKey: ['memory-listing'],
    queryFn: () => get<MemoryListing>('/api/memory'),
    enabled: isOpen,
    staleTime: 0,
  });

  const tier = useMemo(
    () => listing?.tiers.find((t) => t.scope === tab),
    [listing, tab],
  );

  // Load a file's raw content when selected
  const {
    data: fileContent,
    isError: fileError,
    dataUpdatedAt,
  } = useQuery({
    queryKey: ['memory-file', tab, selected],
    queryFn: () =>
      get<{ content: string }>(
        `/api/memory/file?scope=${tab}&filename=${encodeURIComponent(selected ?? '')}`,
      ),
    enabled: isOpen && !!selected,
    staleTime: 0,
  });

  // Seed the editor per FETCH (dataUpdatedAt), not once per file: reopening
  // the modal refetches (staleTime 0) and must replace the textarea with the
  // current on-disk body, or Save would clobber whatever the background
  // extraction agent wrote in the meantime. During-render previous-value
  // pattern, same as MemoryEditorModal.
  const [seededAt, setSeededAt] = useState(0);
  if (selected && fileContent !== undefined && dataUpdatedAt !== seededAt) {
    setSeededAt(dataUpdatedAt);
    setEditContent(fileContent.content);
  }

  const refresh = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: ['memory-listing'] });
    void queryClient.invalidateQueries({ queryKey: ['memory-file'] });
  }, [queryClient]);

  const closeFile = useCallback(() => {
    setSelected(null);
    setSeededAt(0);
    setEditContent('');
    setStatus('');
  }, []);

  // The modal stays mounted while closed (AppShell renders it with
  // isOpen=false), so drop the file selection on close: otherwise the next
  // open shows the previous file's stale body. During-render previous-value
  // pattern (repo lint bans setState-in-effect).
  const [wasOpen, setWasOpen] = useState(isOpen);
  if (wasOpen !== isOpen) {
    setWasOpen(isOpen);
    if (!isOpen) closeFile();
  }

  const handleSave = useCallback(async () => {
    if (!selected) return;
    try {
      await put('/api/memory/file', { scope: tab, filename: selected, content: editContent });
      setStatus('Saved');
      setTimeout(() => setStatus(''), 2500);
      refresh();
    } catch (err) {
      setStatus(`Save failed: ${err instanceof ApiError ? err.message : 'unknown error'}`);
    }
  }, [selected, tab, editContent, refresh]);

  const handleDelete = useCallback(
    async (filename: string) => {
      if (!confirm(`Delete memory file ${filename}?`)) return;
      try {
        await apiDel(`/api/memory/file?scope=${tab}&filename=${encodeURIComponent(filename)}`);
        if (selected === filename) closeFile();
        refresh();
      } catch (err) {
        addToast({
          type: 'error',
          message: `Delete failed: ${err instanceof ApiError ? err.message : 'unknown error'}`,
        });
      }
    },
    [tab, selected, closeFile, refresh, addToast],
  );

  const handlePromote = useCallback(
    async (filename: string) => {
      try {
        await post('/api/memory/promote', { filename });
        addToast({ type: 'success', message: `${filename} promoted to global memory` });
        if (selected === filename) closeFile();
        refresh();
      } catch (err) {
        addToast({
          type: 'error',
          message: `Promote failed: ${err instanceof ApiError ? err.message : 'unknown error'}`,
        });
      }
    },
    [selected, closeFile, refresh, addToast],
  );

  const handleOverlayClick = useCallback(
    (e: React.MouseEvent<HTMLDivElement>) => {
      if (e.target === e.currentTarget) onClose();
    },
    [onClose],
  );

  useEffect(() => {
    if (!isOpen) return;
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        onClose();
      }
    };
    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, [isOpen, onClose]);

  if (!isOpen) return null;

  const projectAvailable = listing?.workspace_connected ?? false;

  return (
    <div className="settings-overlay" id="memoryViewerOverlay" onClick={handleOverlayClick}>
      <div
        className="settings-container"
        style={{ maxWidth: 760 }}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="settings-header">
          <h2>Memory</h2>
          <button className="btn-icon settings-close" onClick={onClose} type="button">
            ×
          </button>
        </div>
        <div className="settings-body" style={{ padding: 16 }}>
          <p style={{ color: 'var(--text-muted)', fontSize: '0.85em', marginBottom: 12 }}>
            What the assistant remembers. Global persists across every chat and project;
            Project stays with the open workspace. Files are written by the background
            extraction agent and by explicit &quot;remember this&quot; requests.
          </p>

          <div style={{ display: 'flex', gap: 8, marginBottom: 12 }}>
            {(['global', 'project'] as const).map((scope) => (
              <button
                key={scope}
                type="button"
                className={`btn ${tab === scope ? 'btn-primary' : 'btn-secondary'}`}
                disabled={scope === 'project' && !projectAvailable}
                title={
                  scope === 'project' && !projectAvailable
                    ? 'Open a workspace to see project memory'
                    : undefined
                }
                onClick={() => {
                  setTab(scope);
                  closeFile();
                }}
              >
                {scope === 'global' ? 'Global' : 'Project'}
                {listing && (
                  <span style={{ opacity: 0.7, marginLeft: 6 }}>
                    {listing.tiers.find((t) => t.scope === scope)?.files.length ?? 0}
                  </span>
                )}
              </button>
            ))}
          </div>

          {isLoading ? (
            <div aria-busy="true">
              <span className="skeleton skeleton-text" style={{ width: '90%' }} />
              <span className="skeleton skeleton-text" style={{ width: '75%' }} />
            </div>
          ) : selected ? (
            <div>
              <div
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'space-between',
                  marginBottom: 8,
                }}
              >
                <strong style={{ fontFamily: 'var(--font-mono, monospace)', fontSize: '0.9em' }}>
                  {selected}
                </strong>
                <button type="button" className="btn btn-secondary" onClick={closeFile}>
                  Back to list
                </button>
              </div>
              {fileError ? (
                <p style={{ color: 'var(--danger, #d66)' }}>
                  Could not load {selected}. It may have just been deleted or renamed by the
                  extraction agent. Go back and refresh the list.
                </p>
              ) : seededAt === 0 ? (
                <div aria-busy="true">
                  <span className="skeleton skeleton-text" style={{ width: '90%' }} />
                  <span className="skeleton skeleton-text" style={{ width: '70%' }} />
                </div>
              ) : (
                <>
                  <textarea
                    value={editContent}
                    onChange={(e) => setEditContent(e.target.value)}
                    spellCheck={false}
                    style={{
                      width: '100%',
                      minHeight: 260,
                      fontFamily: 'var(--font-mono, monospace)',
                      fontSize: '0.85em',
                    }}
                  />
                  <div style={{ display: 'flex', gap: 8, alignItems: 'center', marginTop: 8 }}>
                    <button
                      type="button"
                      className="btn btn-primary"
                      onClick={() => void handleSave()}
                    >
                      Save
                    </button>
                    <button
                      type="button"
                      className="btn btn-secondary"
                      onClick={() => void handleDelete(selected)}
                    >
                      Delete
                    </button>
                    {tab === 'project' && (
                      <button
                        type="button"
                        className="btn btn-secondary"
                        title="Move to global memory so every project can use it"
                        onClick={() => void handlePromote(selected)}
                      >
                        Promote to global
                      </button>
                    )}
                    <span style={{ color: 'var(--text-muted)', fontSize: '0.85em' }}>{status}</span>
                  </div>
                </>
              )}
            </div>
          ) : (
            <div>
              {tier && tier.files.length > 0 ? (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                  {tier.files.map((f) => (
                    <div
                      key={f.filename}
                      className="memory-file-card"
                      onClick={() => setSelected(f.filename)}
                    >
                      <div className="memory-file-head">
                        <span className="toolbar-dropdown-item-name">{f.name}</span>
                        {f.type && <span className="memory-tier-badge">{f.type}</span>}
                        <span className="memory-file-filename">{f.filename}</span>
                      </div>
                      {f.description && (
                        <span className="memory-file-desc">{f.description}</span>
                      )}
                    </div>
                  ))}
                </div>
              ) : (
                <p style={{ color: 'var(--text-muted)' }}>
                  No {tab} memories yet. They accumulate automatically as you chat
                  {tab === 'global' ? ' (no workspace needed)' : ' in this workspace'}.
                </p>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
};
