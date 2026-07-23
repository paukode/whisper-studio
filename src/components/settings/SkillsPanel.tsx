import React, { useCallback, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useSettingsStore } from '@/stores/settingsStore';
import { get, post, put, del, patch } from '@/api/client';
import { useSaveToast } from '@/hooks/useSaveToast';
import { ImportSkillsDialog } from './ImportSkillsDialog';
import { SkillFileTree } from './SkillFileTree';
import { ConfirmDialog } from './ConfirmDialog';

const badgeStyle: React.CSSProperties = {
  marginLeft: '6px',
  fontSize: '10px',
  padding: '1px 6px',
  borderRadius: '10px',
  background: 'var(--surface-0, rgba(127,127,127,0.15))',
  opacity: 0.8,
  verticalAlign: 'middle',
};

export const SkillsPanel: React.FC = () => {
  const skills = useSettingsStore((s) => s.skills);
  const loadSkills = useSettingsStore((s) => s.loadSkills);

  // Editor state — editingSkill tracks which skill is expanded for editing (null = new skill, string = existing)
  const [editingSkill, setEditingSkill] = useState<string | null>(null);
  const [isEditing, setIsEditing] = useState(false);
  const [editorName, setEditorName] = useState('');
  const [editorContent, setEditorContent] = useState('');

  // Rename state — inline rename for a skill row
  const [renamingSkill, setRenamingSkill] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState('');

  // New skill flag — when true, editor shows at top (not inline with a specific skill)
  const [isNewSkill, setIsNewSkill] = useState(false);

  // Folder skills are read-only in the editor (multi-file); track the current
  // view mode and the git-import dialog.
  const [isFolderView, setIsFolderView] = useState(false);
  const [importOpen, setImportOpen] = useState(false);

  // Skill name pending a trust confirmation (null = no dialog open).
  const [trustConfirm, setTrustConfirm] = useState<string | null>(null);

  // Toast-based save feedback (the editor/rows refetch or close on success).
  const saveToast = useSaveToast();

  // Poll for external changes (e.g. skills folder edited outside the app)
  useQuery({
    queryKey: ['skills'],
    queryFn: async () => { await loadSkills(); return null; },
    refetchInterval: 10_000,
  });

  const handleToggle = useCallback(async (skillName: string) => {
    await saveToast(async () => {
      await patch(`/api/skills/${encodeURIComponent(skillName)}/toggle`);
      await useSettingsStore.getState().loadSkills();
    }, { success: 'Skill updated', error: 'Failed to update skill' });
  }, [saveToast]);

  const handleTrust = useCallback(async (skillName: string) => {
    await saveToast(async () => {
      await patch(`/api/skills/${encodeURIComponent(skillName)}/trust`);
      await useSettingsStore.getState().loadSkills();
    }, { success: 'Trust updated', error: 'Failed to update trust' });
  }, [saveToast]);

  const handleNewSkill = useCallback(() => {
    setEditingSkill(null);
    setIsNewSkill(true);
    setIsEditing(true);
    setIsFolderView(false);
    setEditorName('');
    setEditorContent('');
  }, []);

  const handleEdit = useCallback(async (skillName: string) => {
    try {
      const data = await get<{ content: string; isFolder?: boolean }>(`/api/skills/${encodeURIComponent(skillName)}`);
      setEditingSkill(skillName);
      setIsNewSkill(false);
      setIsEditing(true);
      setIsFolderView(typeof data !== 'string' && !!data.isFolder);
      setEditorName(skillName);
      setEditorContent(typeof data === 'string' ? data : (data.content ?? ''));
    } catch (err) {
      console.warn('Failed to load skill content:', err);
    }
  }, []);

  const handleSave = useCallback(async () => {
    const name = editorName.trim();
    if (!name) return;
    await saveToast(async () => {
      if (editingSkill) {
        // Update existing — use new_name if renamed during edit
        const body: Record<string, string> = { content: editorContent };
        if (name !== editingSkill) {
          body.new_name = name;
        }
        await put(`/api/skills/${encodeURIComponent(editingSkill)}`, body);
      } else {
        await post('/api/skills', { name, content: editorContent });
      }
      await useSettingsStore.getState().loadSkills();
      setIsEditing(false);
      setIsNewSkill(false);
      setEditingSkill(null);
      setEditorName('');
      setEditorContent('');
    }, { success: 'Skill saved', error: 'Failed to save skill' });
  }, [editingSkill, editorName, editorContent, saveToast]);

  const handleDelete = useCallback(async (skillName: string) => {
    if (!confirm(`Delete skill "${skillName}"?`)) return;
    await saveToast(async () => {
      await del(`/api/skills/${encodeURIComponent(skillName)}`);
      await useSettingsStore.getState().loadSkills();
      // Close editor if we deleted the skill being edited
      if (editingSkill === skillName) {
        setIsEditing(false);
        setEditingSkill(null);
      }
    }, { success: 'Skill deleted', error: 'Failed to delete skill' });
  }, [editingSkill, saveToast]);

  const handleCancel = useCallback(() => {
    setIsEditing(false);
    setIsNewSkill(false);
    setIsFolderView(false);
    setEditingSkill(null);
    setEditorName('');
    setEditorContent('');
  }, []);

  const handleRenameStart = useCallback((skillName: string) => {
    setRenamingSkill(skillName);
    setRenameValue(skillName);
  }, []);

  const handleRenameConfirm = useCallback(async () => {
    if (!renamingSkill || !renameValue.trim()) return;
    const newName = renameValue.trim();
    if (newName === renamingSkill) {
      setRenamingSkill(null);
      return;
    }
    await saveToast(async () => {
      // Fetch current content, then PUT with new_name
      const data = await get<{ content: string }>(`/api/skills/${encodeURIComponent(renamingSkill)}`);
      const content = typeof data === 'string' ? data : (data.content ?? '');
      // Update name in frontmatter content
      const updatedContent = content.replace(/^(name:\s*).+$/m, `$1${newName}`);
      await put(`/api/skills/${encodeURIComponent(renamingSkill)}`, {
        content: updatedContent,
        new_name: newName,
      });
      await useSettingsStore.getState().loadSkills();
      setRenamingSkill(null);
      setRenameValue('');
    }, { success: 'Skill renamed', error: 'Failed to rename skill' });
  }, [renamingSkill, renameValue, saveToast]);

  const handleRenameCancel = useCallback(() => {
    setRenamingSkill(null);
    setRenameValue('');
  }, []);

  const renderEditor = () => (
    <div className="settings-editor" id="skillEditor">
      <div className="editor-header">
        <input
          type="text"
          className="editor-name-input"
          aria-label="Skill name"
          placeholder="Skill name"
          value={editorName}
          onChange={(e) => setEditorName(e.target.value)}
          readOnly={!!editingSkill}
        />
        <div className="editor-actions">
          {!isFolderView && <button className="btn btn-primary btn-sm" onClick={() => void handleSave()}>Save</button>}
          <button className="btn btn-sm" onClick={handleCancel}>{isFolderView ? 'Close' : 'Cancel'}</button>
        </div>
      </div>
      <textarea
        className="editor-textarea"
        aria-label="Skill content"
        placeholder="Paste skill markdown content (with --- frontmatter ---)"
        value={editorContent}
        readOnly={isFolderView}
        onChange={(e) => setEditorContent(e.target.value)}
      />
      {isFolderView && editingSkill && <SkillFileTree skillName={editingSkill} />}
    </div>
  );

  return (
    <div className="settings-panel skills-panel">
      <div className="settings-toolbar">
        <button className="btn btn-sm" id="newSkillBtn" onClick={handleNewSkill}>+ New Skill</button>
        <button className="btn btn-sm" id="importSkillsBtn" onClick={() => setImportOpen(true)}>Import from Git</button>
      </div>

      {importOpen && (
        <ImportSkillsDialog
          onClose={() => setImportOpen(false)}
          onImported={() => void useSettingsStore.getState().loadSkills()}
        />
      )}

      {trustConfirm && (
        <ConfirmDialog
          title={`Trust "${trustConfirm}"?`}
          message={
            <>
              Trusting this skill lets it run its <strong>own bundled scripts without an
              approval prompt</strong> each time it is used. Only trust skills whose code you
              have reviewed and fully trust. You can revoke trust at any time.
            </>
          }
          confirmLabel="Trust skill"
          onConfirm={() => {
            const name = trustConfirm;
            setTrustConfirm(null);
            void handleTrust(name);
          }}
          onCancel={() => setTrustConfirm(null)}
        />
      )}

      {/* New skill editor — appears at the top */}
      {isEditing && isNewSkill && renderEditor()}

      <div className="settings-list" id="skillsList">
        {skills.length === 0 && !isEditing && (
          <p className="settings-empty">No skills available.</p>
        )}
        {[...skills].sort((a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: 'base' })).map((skill) => (
          <React.Fragment key={skill.name}>
            <div className="settings-item">
              <div className="settings-item-info">
                {renamingSkill === skill.name ? (
                  <div style={{ display: 'flex', gap: '4px', alignItems: 'center' }}>
                    <input
                      className="settings-input"
                      aria-label="Rename skill"
                      value={renameValue}
                      onChange={(e) => setRenameValue(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter') void handleRenameConfirm();
                        if (e.key === 'Escape') { e.preventDefault(); handleRenameCancel(); }
                      }}
                      style={{ width: '140px', padding: '2px 6px' }}
                      autoFocus
                    />
                    <button className="btn btn-sm" onClick={() => void handleRenameConfirm()} type="button">OK</button>
                    <button className="btn btn-sm" onClick={handleRenameCancel} type="button">&times;</button>
                  </div>
                ) : (
                  <>
                    <div className="settings-item-name">
                      {skill.name}
                      {skill.isFolder && <span style={badgeStyle}>folder</span>}
                      {skill.hasScripts && <span style={badgeStyle} title="Contains executable scripts">scripts</span>}
                    </div>
                    <div className="settings-item-desc">{skill.description ?? ''}</div>
                  </>
                )}
              </div>
              <div className="settings-item-actions">
                <button className="btn btn-sm" onClick={() => void handleEdit(skill.name)}>{skill.isFolder ? 'View' : 'Edit'}</button>
                {!skill.isFolder && <button className="btn btn-sm" onClick={() => handleRenameStart(skill.name)}>Rename</button>}
                <button
                  className="btn btn-sm"
                  style={{ color: 'var(--accent-record)' }}
                  onClick={() => void handleDelete(skill.name)}
                >
                  Delete
                </button>
                {skill.isFolder && (
                  <button
                    type="button"
                    className="btn btn-sm"
                    title="Trusted skills run their own bundled scripts without an approval prompt"
                    onClick={() => (skill.trusted ? void handleTrust(skill.name) : setTrustConfirm(skill.name))}
                    style={skill.trusted ? { color: 'var(--text-accent, #c8862a)', fontWeight: 500 } : undefined}
                  >
                    {skill.trusted ? 'Trusted ✓' : 'Trust'}
                  </button>
                )}
                <label className="toggle-switch">
                  <input
                    type="checkbox"
                    checked={skill.enabled}
                    onChange={() => void handleToggle(skill.name)}
                  />
                  <span className="toggle-slider"></span>
                </label>
              </div>
            </div>
            {/* In-place editor — appears directly below the skill being edited */}
            {isEditing && !isNewSkill && editingSkill === skill.name && renderEditor()}
          </React.Fragment>
        ))}
      </div>
    </div>
  );
};
