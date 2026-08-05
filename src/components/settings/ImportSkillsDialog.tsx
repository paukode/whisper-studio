import React, { useMemo, useRef, useState } from 'react';
import { post } from '@/api/client';
import { useBackdropDismiss } from '@/hooks/useBackdropDismiss';

interface PreviewSkill {
  subpath: string;
  name: string;
  description?: string;
  hasScripts: boolean;
  scriptFiles: string[];
  fileCount: number;
}

interface ImportResult {
  imported: Array<{ name: string }>;
  conflicts: Array<{ name?: string; subpath: string }>;
  errors: Array<{ subpath: string; reason: string }>;
}

type Mode = 'git' | 'local';

const errMsg = (e: unknown): string =>
  e instanceof Error ? e.message : String(e);

/** Read a JSON error body ({error} or {detail}) from a failed multipart
 *  response, falling back to the status text. */
async function readError(resp: Response): Promise<string> {
  try {
    const body = (await resp.json()) as { error?: string; detail?: string };
    return body.error || body.detail || `HTTP ${resp.status}`;
  } catch {
    return `HTTP ${resp.status}`;
  }
}

/** Import folder skills from a git URL OR a local folder: preview the available
 *  skills, select some, and import them into /skills/ (hot-reloaded, no restart).
 *  The two modes share the same select→import UX. */
export const ImportSkillsDialog: React.FC<{
  onClose: () => void;
  onImported: () => void;
}> = ({ onClose, onImported }) => {
  const [mode, setMode] = useState<Mode>('git');
  const [url, setUrl] = useState('');
  // Files chosen via the local-folder picker (each carries webkitRelativePath).
  const [localFiles, setLocalFiles] = useState<File[]>([]);
  const [folderLabel, setFolderLabel] = useState('');
  const folderInputRef = useRef<HTMLInputElement | null>(null);

  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [skills, setSkills] = useState<PreviewSkill[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [overwrite, setOverwrite] = useState(false);
  const [filter, setFilter] = useState('');
  const [searchDesc, setSearchDesc] = useState(false);
  const [summary, setSummary] = useState('');

  const resetResults = () => {
    setError('');
    setSummary('');
    setSkills([]);
    setSelected(new Set());
  };

  const switchMode = (next: Mode) => {
    if (next === mode) return;
    setMode(next);
    resetResults();
    setFilter('');
  };

  const doPreviewGit = async () => {
    setLoading(true);
    resetResults();
    try {
      const r = await post<{ skills: PreviewSkill[] }>('/api/skills/import/preview', {
        url: url.trim(),
      });
      setSkills(r.skills ?? []);
      if ((r.skills ?? []).length === 0) setError('No skills found in that repository.');
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setLoading(false);
    }
  };

  // Build a multipart body preserving each file's folder-relative path (sent as
  // the part filename) so the backend can reconstruct the picked tree.
  const buildForm = (files: File[]): FormData => {
    const fd = new FormData();
    for (const f of files) fd.append('files', f, f.webkitRelativePath || f.name);
    return fd;
  };

  const onFolderPicked = async (files: File[]) => {
    setLocalFiles(files);
    const first = files[0]?.webkitRelativePath ?? '';
    setFolderLabel(first ? first.split('/')[0] : `${files.length} files`);
    setLoading(true);
    resetResults();
    try {
      const resp = await fetch('/api/skills/import/local/preview', {
        method: 'POST',
        body: buildForm(files),
      });
      if (!resp.ok) throw new Error(await readError(resp));
      const r = (await resp.json()) as { skills: PreviewSkill[] };
      setSkills(r.skills ?? []);
      if ((r.skills ?? []).length === 0) setError('No skills found in that folder.');
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setLoading(false);
    }
  };

  const toggle = (subpath: string) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(subpath)) next.delete(subpath);
      else next.add(subpath);
      return next;
    });

  const applyResult = (r: ImportResult) => {
    onImported();
    // Clean success: close the dialog. If some skills hit conflicts/errors,
    // keep it open with a summary so the user can enable overwrite and retry.
    if (r.conflicts.length === 0 && r.errors.length === 0) {
      onClose();
      return;
    }
    const parts = [`Imported ${r.imported.length}`];
    if (r.conflicts.length)
      parts.push(`${r.conflicts.length} already existed (enable overwrite to replace)`);
    if (r.errors.length) parts.push(`${r.errors.length} failed`);
    setSummary(parts.join(' · '));
  };

  const doImport = async () => {
    if (selected.size === 0) return;
    setLoading(true);
    setError('');
    setSummary('');
    try {
      if (mode === 'git') {
        const r = await post<ImportResult>('/api/skills/import', {
          url: url.trim(),
          subpaths: [...selected],
          overwrite,
        });
        applyResult(r);
      } else {
        const fd = buildForm(localFiles);
        fd.append('subpaths', JSON.stringify([...selected]));
        fd.append('overwrite', overwrite ? 'true' : 'false');
        const resp = await fetch('/api/skills/import/local', { method: 'POST', body: fd });
        if (!resp.ok) throw new Error(await readError(resp));
        applyResult((await resp.json()) as ImportResult);
      }
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setLoading(false);
    }
  };

  const shown = useMemo(() => {
    const q = filter.trim().toLowerCase();
    if (!q) return skills;
    return skills.filter((s) => {
      if (s.subpath.toLowerCase().includes(q) || s.name.toLowerCase().includes(q)) return true;
      return searchDesc && (s.description ?? '').toLowerCase().includes(q);
    });
  }, [skills, filter, searchDesc]);

  // Dismiss only on a genuine backdrop press (mousedown AND click both on the
  // overlay), so selecting text in the URL/filter inputs and releasing outside
  // does not close the dialog.
  const backdrop = useBackdropDismiss(onClose);

  const tabStyle = (active: boolean): React.CSSProperties => ({
    padding: '5px 12px',
    borderRadius: '8px',
    border: '1px solid var(--border, #333)',
    background: active ? 'var(--accent, #47f)' : 'transparent',
    color: active ? '#fff' : 'var(--text-primary, #eee)',
    cursor: 'pointer',
    fontSize: '13px',
  });

  return (
    <div
      role="dialog"
      aria-modal="true"
      style={{
        position: 'fixed',
        inset: 0,
        background: 'rgba(0,0,0,0.45)',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        zIndex: 1000,
      }}
      {...backdrop}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          background: 'var(--surface-1, #1e1e1e)',
          color: 'var(--text-primary, #eee)',
          borderRadius: '12px',
          width: 'min(680px, 92vw)',
          maxHeight: '86vh',
          display: 'flex',
          flexDirection: 'column',
          padding: '16px',
          gap: '10px',
        }}
      >
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <strong>Import skills</strong>
          <button className="btn btn-sm" onClick={onClose} type="button" aria-label="Close">
            &times;
          </button>
        </div>

        {/* Source mode selector */}
        <div role="tablist" aria-label="Import source" style={{ display: 'flex', gap: '6px' }}>
          <button
            type="button"
            role="tab"
            aria-selected={mode === 'git'}
            style={tabStyle(mode === 'git')}
            onClick={() => switchMode('git')}
          >
            From Git
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={mode === 'local'}
            style={tabStyle(mode === 'local')}
            onClick={() => switchMode('local')}
          >
            From local folder
          </button>
        </div>

        {mode === 'git' ? (
          <div style={{ display: 'flex', gap: '6px' }}>
            <input
              type="text"
              className="settings-input"
              style={{ flex: 1, padding: '6px 8px' }}
              placeholder="https://github.com/owner/repo"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') void doPreviewGit();
              }}
            />
            <button
              className="btn btn-sm"
              onClick={() => void doPreviewGit()}
              disabled={loading || !url.trim()}
              type="button"
            >
              {loading && skills.length === 0 ? 'Loading…' : 'Preview'}
            </button>
          </div>
        ) : (
          <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
            <button
              className="btn btn-sm"
              type="button"
              disabled={loading}
              onClick={() => folderInputRef.current?.click()}
            >
              {loading && skills.length === 0 ? 'Scanning…' : 'Choose folder…'}
            </button>
            {folderLabel && (
              <span style={{ fontSize: '12px', opacity: 0.7 }}>
                <span style={{ fontFamily: 'var(--font-mono, monospace)' }}>{folderLabel}</span>
              </span>
            )}
            <input
              ref={(el) => {
                folderInputRef.current = el;
                // webkitdirectory is not in the React attribute types; set it
                // imperatively so the picker selects a whole directory tree.
                if (el) {
                  el.setAttribute('webkitdirectory', '');
                  el.setAttribute('directory', '');
                }
              }}
              type="file"
              multiple
              aria-label="Choose skills folder"
              style={{ display: 'none' }}
              onChange={(e) => {
                const files = Array.from(e.target.files ?? []);
                // Allow re-picking the same folder later.
                e.target.value = '';
                if (files.length) void onFolderPicked(files);
              }}
            />
          </div>
        )}

        {error && <div style={{ color: 'var(--accent-record, #e57)' }}>{error}</div>}
        {summary && <div style={{ color: 'var(--text-success, #5c8)' }}>{summary}</div>}

        {skills.length > 0 && (
          <>
            <div style={{ display: 'flex', gap: '10px', alignItems: 'center' }}>
              <input
                type="text"
                className="settings-input"
                style={{ flex: 1, padding: '6px 8px' }}
                placeholder={`Filter ${skills.length} skills…`}
                value={filter}
                onChange={(e) => setFilter(e.target.value)}
              />
              <label style={{ display: 'flex', gap: '6px', alignItems: 'center', fontSize: '13px', whiteSpace: 'nowrap' }}>
                <input type="checkbox" checked={searchDesc} onChange={(e) => setSearchDesc(e.target.checked)} />
                Search descriptions
              </label>
            </div>
            <div style={{ overflow: 'auto', flex: 1, border: '1px solid var(--border, #333)', borderRadius: '8px' }}>
              {shown.map((s) => (
                <label
                  key={s.subpath}
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    gap: '8px',
                    padding: '6px 10px',
                    borderBottom: '1px solid var(--border, #2a2a2a)',
                    cursor: 'pointer',
                  }}
                >
                  <input
                    type="checkbox"
                    checked={selected.has(s.subpath)}
                    onChange={() => toggle(s.subpath)}
                  />
                  <span style={{ flex: 1, minWidth: 0 }}>
                    <span>
                      <span style={{ fontFamily: 'var(--font-mono, monospace)' }}>{s.subpath || s.name}</span>
                      {s.hasScripts && (
                        <span style={{ marginLeft: '8px', fontSize: '11px', opacity: 0.7 }}>
                          {s.scriptFiles.length} script{s.scriptFiles.length === 1 ? '' : 's'}
                        </span>
                      )}
                    </span>
                    {s.description && (
                      <span
                        style={{
                          display: 'block',
                          fontSize: '12px',
                          opacity: 0.6,
                          overflow: 'hidden',
                          textOverflow: 'ellipsis',
                          whiteSpace: 'nowrap',
                        }}
                      >
                        {s.description}
                      </span>
                    )}
                  </span>
                </label>
              ))}
            </div>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
              <label style={{ display: 'flex', gap: '6px', alignItems: 'center', fontSize: '13px' }}>
                <input type="checkbox" checked={overwrite} onChange={(e) => setOverwrite(e.target.checked)} />
                Overwrite existing
              </label>
              <button
                className="btn btn-primary btn-sm"
                onClick={() => void doImport()}
                disabled={loading || selected.size === 0}
                type="button"
              >
                Import {selected.size > 0 ? `(${selected.size})` : ''}
              </button>
            </div>
            <div style={{ fontSize: '12px', opacity: 0.6 }}>
              Imported scripts run through the normal approval prompt. Mark a skill trusted
              afterward to let it run its own scripts without prompting.
            </div>
          </>
        )}
      </div>
    </div>
  );
};
