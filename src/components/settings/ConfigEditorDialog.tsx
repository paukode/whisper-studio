import React, { useCallback, useEffect, useState } from 'react';
import { createPortal } from 'react-dom';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { get, put } from '@/api/client';
import { ApiError } from '@/types/api';
import { useUIStore } from '@/stores/uiStore';

/**
 * In-app config editor (Settings > Models "config.json" link).
 *
 * Since the config split the editor edits the USER layer (config.user.json) —
 * only the user's deltas: added local models, region/keys/flags, per-field
 * overrides, and a hide-list. It merges on top of the app's built-in catalog
 * (config.example.json), which updates with each release. A second, read-only
 * "Effective config (merged)" tab shows the fully merged result so the user can
 * see the shipped models next to their own and copy a shipped entry down to
 * override it.
 *
 * Deliberately a plain monospace textarea, not the workspace MonacoEditor:
 * that component is coupled to workspace state and the config lives outside any
 * workspace. The server validates on save — JSON syntax (with line/column) plus
 * structural rules — and returns every problem at once as newline-separated
 * messages, rendered as a monospace list inside the dialog.
 */

interface RawConfig {
  /** The USER-layer file text (config.user.json), edited here. */
  user_text: string;
  /** Path of the USER-layer file being edited. */
  path: string;
  /** Read-only pretty JSON of the fully merged effective config. */
  effective_text: string;
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

type Tab = 'user' | 'effective';

export const ConfigEditorDialog: React.FC<{ onClose: () => void }> = ({ onClose }) => {
  const queryClient = useQueryClient();
  const addToast = useUIStore((s) => s.addToast);
  const [text, setText] = useState('');
  const [tab, setTab] = useState<Tab>('user');
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
    setText(data.user_text);
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
      addToast({ type: 'success', message: 'Config saved' });
      onClose();
    } catch (e) {
      const message = e instanceof ApiError ? e.message : 'Could not save your config.';
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

  const tabButton = (id: Tab, label: string) => (
    <button
      type="button"
      role="tab"
      aria-selected={tab === id}
      onClick={() => setTab(id)}
      className="btn btn-sm"
      style={{
        background: tab === id ? 'var(--surface-2, #333)' : 'transparent',
        borderBottom: tab === id ? '2px solid var(--accent)' : '2px solid transparent',
        borderRadius: 0,
      }}
    >
      {label}
    </button>
  );

  const boxStyle: React.CSSProperties = {
    height: 340,
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
  };

  return (
    // No backdrop-click-to-close: a click on the dark overlay does nothing, so a
    // text selection inside the editor that releases outside the dialog can
    // never dismiss it. Closes only via Cancel, the toast-on-save, or Escape.
    <div
      role="dialog"
      aria-modal="true"
      aria-label="Edit your config"
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
      {/* stopPropagation keeps the dialog's own clicks (Cancel/Save, text
          selection) from bubbling up the React tree to the Settings modal that
          hosts this portal — closing the editor must not close Settings. */}
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
        <strong>Your config</strong>
        <p className="settings-hint" style={{ margin: 0 }}>
          Your personal config. It merges on top of the app&apos;s built-in models, which update
          with each release. Add local chat models under <code>chat_models</code> with{' '}
          <code>{'"is_local": true'}</code>, <code>repo_id</code>, <code>filename</code> and{' '}
          <code>dir</code>; hide a shipped model by adding its key to{' '}
          <code>chat_models_disabled</code>. Saving validates and applies immediately.
        </p>
        <div style={{ fontSize: '0.8em', color: 'var(--text-secondary, #aaa)' }}>
          {data?.path ?? 'Loading…'}
        </div>
        {isError && (
          <p role="alert" style={{ color: 'var(--accent-record, #e5484d)', margin: 0 }}>
            Could not load your config.
          </p>
        )}

        <div role="tablist" aria-label="Config view" style={{ display: 'flex', gap: 4 }}>
          {tabButton('user', 'Your config')}
          {tabButton('effective', 'Effective config (merged)')}
        </div>

        {isLoading ? (
          <div aria-busy="true">
            <span className="skeleton skeleton-text" style={{ width: '90%' }} />
            <span className="skeleton skeleton-text" style={{ width: '75%' }} />
          </div>
        ) : tab === 'user' ? (
          <textarea
            aria-label="Your config contents"
            spellCheck={false}
            style={boxStyle}
            value={text}
            onChange={(e) => setText(e.target.value)}
          />
        ) : (
          <>
            <p className="settings-hint" style={{ margin: 0, fontSize: '0.8em' }}>
              Read-only. The fully merged result — the app&apos;s built-in models plus yours. Copy
              an entry here into <strong>Your config</strong> to override its fields.
            </p>
            <textarea
              aria-label="Effective config (merged)"
              readOnly
              spellCheck={false}
              style={{ ...boxStyle, opacity: 0.9 }}
              value={data?.effective_text ?? ''}
            />
          </>
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
            disabled={saving || isLoading || tab !== 'user'}
            title={tab !== 'user' ? 'Switch to "Your config" to edit and save' : undefined}
          >
            {saving ? 'Saving…' : 'Save'}
          </button>
        </div>
      </div>
    </div>
  );
};

/** Inline "config.json" hyperlink that opens the editor dialog. Drop it into
 *  any helper sentence: "Add models by editing <ConfigJsonLink />." The label
 *  stays "config.json" (the name users know); it opens the user config layer. */
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
