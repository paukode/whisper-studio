import React, { useMemo, useState } from 'react';
import { post } from '@/api/client';

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

const errMsg = (e: unknown): string =>
  e instanceof Error ? e.message : String(e);

/** Import folder skills from a git URL: preview the repo's skills, select some,
 *  and import them into /skills/ (hot-reloaded, no restart). */
export const ImportSkillsDialog: React.FC<{
  onClose: () => void;
  onImported: () => void;
}> = ({ onClose, onImported }) => {
  const [url, setUrl] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [skills, setSkills] = useState<PreviewSkill[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [overwrite, setOverwrite] = useState(false);
  const [filter, setFilter] = useState('');
  const [searchDesc, setSearchDesc] = useState(false);
  const [summary, setSummary] = useState('');

  const doPreview = async () => {
    setLoading(true);
    setError('');
    setSummary('');
    setSkills([]);
    setSelected(new Set());
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

  const toggle = (subpath: string) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(subpath)) next.delete(subpath);
      else next.add(subpath);
      return next;
    });

  const doImport = async () => {
    if (selected.size === 0) return;
    setLoading(true);
    setError('');
    setSummary('');
    try {
      const r = await post<ImportResult>('/api/skills/import', {
        url: url.trim(),
        subpaths: [...selected],
        overwrite,
      });
      onImported();
      // Clean success: close the dialog. If some skills hit conflicts/errors,
      // keep it open with a summary so the user can enable overwrite and retry.
      if (r.conflicts.length === 0 && r.errors.length === 0) {
        onClose();
        return;
      }
      const parts = [`Imported ${r.imported.length}`];
      if (r.conflicts.length) parts.push(`${r.conflicts.length} already existed (enable overwrite to replace)`);
      if (r.errors.length) parts.push(`${r.errors.length} failed`);
      setSummary(parts.join(' · '));
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
      onClick={onClose}
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
          <strong>Import skills from Git</strong>
          <button className="btn btn-sm" onClick={onClose} type="button" aria-label="Close">
            &times;
          </button>
        </div>

        <div style={{ display: 'flex', gap: '6px' }}>
          <input
            type="text"
            className="settings-input"
            style={{ flex: 1, padding: '6px 8px' }}
            placeholder="https://github.com/owner/repo"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') void doPreview();
            }}
          />
          <button
            className="btn btn-sm"
            onClick={() => void doPreview()}
            disabled={loading || !url.trim()}
            type="button"
          >
            {loading && skills.length === 0 ? 'Loading…' : 'Preview'}
          </button>
        </div>

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
