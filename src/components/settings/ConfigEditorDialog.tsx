import React, { useCallback, useEffect, useState } from 'react';
import { createPortal } from 'react-dom';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { get, put } from '@/api/client';
import { ApiError } from '@/types/api';
import { useUIStore } from '@/stores/uiStore';

/**
 * In-app config.json editor (Settings > Models "config.json" link).
 *
 * Deliberately a plain monospace textarea, not the workspace MonacoEditor:
 * that component is coupled to workspace state (LSP client keyed on the
 * connected workspace, per-tab view-state cache, uiStore ws fields) and
 * config.json lives outside any workspace. The server validates on save —
 * JSON syntax (with line/column) plus structural rules — and returns every
 * problem at once as newline-separated messages, rendered as a monospace
 * list inside the dialog.
 */

interface RawConfig {
  text: string;
  path: string;
}

interface SaveRawConfigResult {
  ok: boolean;
  path: string;
  /** Local-model registry keys after the save — confirms a new model landed. */
  local_models: string[];
}

export const fetchRawConfig = () => get<RawConfig>('/api/config/raw');
export const saveRawConfig = (text: string) =>
  put<SaveRawConfigResult>('/api/config/raw', { text });

export const ConfigEditorDialog: React.FC<{ onClose: () => void }> = ({ onClose }) => {
  const queryClient = useQueryClient();
  const addToast = useUIStore((s) => s.addToast);
  const [text, setText] = useState('');
  const [errors, setErrors] = useState<string[]>([]);
  const [saving, setSaving] = useState(false);

  const { data, isLoading, isError, dataUpdatedAt } = useQuery({
    queryKey: ['config-raw'],
    queryFn: fetchRawConfig,
    staleTime: 0,
    gcTime: 0,
    retry: false,
  });

  // Seed the textarea once per successful fetch (previous-value pattern, so
  // typing never fights the query cache).
  const [seededAt, setSeededAt] = useState(0);
  if (data !== undefined && dataUpdatedAt !== seededAt) {
    setSeededAt(dataUpdatedAt);
    setText(data.text);
  }

  const handleSave = useCallback(async () => {
    setSaving(true);
    setErrors([]);
    try {
      await saveRawConfig(text);
      // New/removed local models must appear immediately: the models catalog,
      // the status poll, and the engine pickers all derive from config.
      void queryClient.invalidateQueries({ queryKey: ['models-manager-catalog'] });
      void queryClient.invalidateQueries({ queryKey: ['models-manager-status'] });
      void queryClient.invalidateQueries({ queryKey: ['index-engines'] });
      addToast({ type: 'success', message: 'config.json saved' });
      onClose();
    } catch (e) {
      const message = e instanceof ApiError ? e.message : 'Could not save config.json.';
      // The server packs every validation problem into one newline-separated
      // detail string; render them as a list.
      setErrors(message.split('\n').filter((line) => line.trim().length > 0));
    } finally {
      setSaving(false);
    }
  }, [text, queryClient, addToast, onClose]);

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        onClose();
      }
    };
    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, [onClose]);

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label="Edit config.json"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
      style={{
        position: 'fixed',
        inset: 0,
        background: 'rgba(0,0,0,0.45)',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        zIndex: 1100,
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          background: 'var(--surface-1, #1e1e1e)',
          color: 'var(--text-primary, #eee)',
          borderRadius: 12,
          width: 'min(680px, 94vw)',
          padding: 18,
          display: 'flex',
          flexDirection: 'column',
          gap: 12,
        }}
      >
        <strong>config.json</strong>
        <p className="settings-hint" style={{ margin: 0 }}>
          {data?.path ?? 'Loading…'}. Add local chat models under <code>chat_models</code> with{' '}
          <code>{'"is_local": true'}</code>, <code>repo_id</code>, <code>filename</code> and{' '}
          <code>dir</code>. Saving validates the file and applies it immediately.
        </p>
        {isError && (
          <p role="alert" style={{ color: 'var(--accent-record, #e5484d)', margin: 0 }}>
            Could not load config.json.
          </p>
        )}
        {isLoading ? (
          <div aria-busy="true">
            <span className="skeleton skeleton-text" style={{ width: '90%' }} />
            <span className="skeleton skeleton-text" style={{ width: '75%' }} />
          </div>
        ) : (
          <textarea
            aria-label="config.json contents"
            spellCheck={false}
            style={{
              height: 360,
              width: '100%',
              fontFamily: 'var(--font-mono, monospace)',
              fontSize: '0.85em',
              lineHeight: 1.5,
              background: 'var(--bg-secondary)',
              color: 'var(--text-primary)',
              border: '1px solid var(--border)',
              borderRadius: 6,
              padding: 12,
              resize: 'vertical',
              whiteSpace: 'pre',
            }}
            value={text}
            onChange={(e) => setText(e.target.value)}
          />
        )}
        {errors.length > 0 && (
          <ul
            role="alert"
            style={{
              margin: 0,
              paddingLeft: 18,
              fontFamily: 'var(--font-mono, monospace)',
              fontSize: '0.8em',
              lineHeight: 1.6,
              color: 'var(--accent-record, #e5484d)',
              maxHeight: 120,
              overflowY: 'auto',
            }}
          >
            {errors.map((err, i) => (
              <li key={i}>{err}</li>
            ))}
          </ul>
        )}
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
          <button className="btn btn-sm" type="button" onClick={onClose} disabled={saving}>
            Cancel
          </button>
          <button
            className="btn btn-primary btn-sm"
            type="button"
            onClick={() => void handleSave()}
            disabled={saving || isLoading}
          >
            {saving ? 'Saving…' : 'Save'}
          </button>
        </div>
      </div>
    </div>
  );
};

/** Inline "config.json" hyperlink that opens the editor dialog. Drop it into
 *  any helper sentence: "Add models by editing <ConfigJsonLink />." */
export const ConfigJsonLink: React.FC = () => {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        style={{
          background: 'none',
          border: 'none',
          padding: 0,
          font: 'inherit',
          color: 'var(--accent)',
          textDecoration: 'underline',
          cursor: 'pointer',
        }}
      >
        config.json
      </button>
      {/* Portal: the link often sits inside a <p>, and the dialog is a <div>
          overlay — rendering it in place would be invalid DOM nesting. */}
      {open && createPortal(<ConfigEditorDialog onClose={() => setOpen(false)} />, document.body)}
    </>
  );
};
