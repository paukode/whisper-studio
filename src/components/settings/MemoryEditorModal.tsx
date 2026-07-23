import React, { useCallback, useEffect, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { get, put, del as apiDel } from '@/api/client';
import { ApiError } from '@/types/api';

interface MemoryEditorModalProps {
  isOpen: boolean;
  onClose: () => void;
}

/**
 * Memory (WHISPER.md) editor modal matching the vanilla HTML structure.
 * Auto-loaded as system context for every chat in the workspace.
 */
export const MemoryEditorModal: React.FC<MemoryEditorModalProps> = ({ isOpen, onClose }) => {
  const [content, setContent] = useState('');
  const [saveStatus, setSaveStatus] = useState('');

  // Load WHISPER.md via react-query when the modal opens (no setState-in-effect).
  // Backend mounts the router at /api/whisper-md. staleTime:0 so each open
  // re-fetches the current file.
  const { data: loaded, isLoading, isError, error: loadError, dataUpdatedAt } = useQuery({
    queryKey: ['whisper-md'],
    queryFn: async (): Promise<string> => {
      const data = await get<{ content: string } | string>('/api/whisper-md');
      return typeof data === 'string' ? data : (data.content ?? '');
    },
    enabled: isOpen,
    staleTime: 0,
  });

  // Seed the editable textarea on each fresh load. During-render previous-value
  // pattern keyed on dataUpdatedAt (changes per successful fetch) so it re-seeds
  // on reopen but not on every keystroke.
  const [seededAt, setSeededAt] = useState(0);
  if (loaded !== undefined && dataUpdatedAt !== seededAt) {
    setSeededAt(dataUpdatedAt);
    setContent(loaded);
  }

  const loading = isOpen && isLoading;
  const statusText = saveStatus
    || (isError ? `Load failed: ${loadError instanceof ApiError ? loadError.message : 'Could not load'}` : '');

  const handleSave = useCallback(async () => {
    setSaveStatus('');
    try {
      await put('/api/whisper-md', { content });
      setSaveStatus('Saved!');
      setTimeout(() => setSaveStatus(''), 3000);
    } catch (err) {
      // Surface the real reason. The backend returns 400 with
      // {error: "No workspace connected"} when no workspace is open,
      // and 500 with {error: <strerror>} on filesystem failures.
      // ApiError.message already extracts those bodies via the
      // client's parseErrorMessage helper.
      const msg = err instanceof ApiError ? err.message : 'unknown error';
      setSaveStatus(`Save failed: ${msg}`);
    }
  }, [content]);

  const handleDelete = useCallback(async () => {
    if (!confirm('Delete WHISPER.md? This removes all project memory.')) return;
    try {
      await apiDel('/api/whisper-md');
      setContent('');
      setSaveStatus('Deleted');
      setTimeout(() => setSaveStatus(''), 3000);
    } catch (err) {
      const msg = err instanceof ApiError ? err.message : 'unknown error';
      setSaveStatus(`Delete failed: ${msg}`);
    }
  }, []);

  const handleOverlayClick = useCallback((e: React.MouseEvent<HTMLDivElement>) => {
    if (e.target === e.currentTarget) onClose();
  }, [onClose]);

  useEffect(() => {
    if (!isOpen) return;
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') { e.preventDefault(); onClose(); }
    };
    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, [isOpen, onClose]);

  if (!isOpen) return null;

  return (
    <div className="settings-overlay" id="memoryOverlay" onClick={handleOverlayClick}>
      <div className="settings-container" style={{ maxWidth: 640 }} onClick={(e) => e.stopPropagation()}>
        <div className="settings-header">
          <h2>Project Memory (WHISPER.md)</h2>
          <button className="btn-icon settings-close" id="memoryClose" onClick={onClose} type="button">×</button>
        </div>
        <div className="settings-body" style={{ padding: 16 }}>
          <p style={{ color: 'var(--text-muted)', fontSize: '0.85em', marginBottom: 12 }}>
            Auto-loaded as system context for every chat in this workspace.
            Give Claude persistent project-specific instructions.
          </p>
          {loading ? (
            <div aria-busy="true">
              <span className="skeleton skeleton-text" style={{ width: '90%' }} />
              <span className="skeleton skeleton-text" style={{ width: '75%' }} />
              <span className="skeleton skeleton-text" style={{ width: '82%' }} />
            </div>
          ) : (
            <textarea
              id="memoryEditor"
              style={{
                height: 300,
                width: '100%',
                fontFamily: 'var(--font-mono)',
                fontSize: '0.85em',
                background: 'var(--bg-secondary)',
                color: 'var(--text-primary)',
                border: '1px solid var(--border)',
                borderRadius: 6,
                padding: 12,
                resize: 'vertical',
              }}
              placeholder={'# Project Memory\n\n## Tech Stack\n- Python + FastAPI\n\n## Conventions\n- Always use type hints'}
              value={content}
              onChange={(e) => setContent(e.target.value)}
            />
          )}
          <div style={{ marginTop: 12, display: 'flex', gap: 8, alignItems: 'center' }}>
            <button className="btn btn-primary btn-sm" id="memorySaveBtn" onClick={() => void handleSave()} type="button">
              Save WHISPER.md
            </button>
            <button className="btn btn-sm" id="memoryDeleteBtn" onClick={() => void handleDelete()} type="button">
              Delete
            </button>
            <span className="settings-hint" id="memorySaveStatus" style={{ marginLeft: 8 }}>{statusText}</span>
          </div>
        </div>
      </div>
    </div>
  );
};
