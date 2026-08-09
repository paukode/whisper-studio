import React, { useCallback, useState } from 'react';
import { get, post } from '@/api/client';
import { useUIStore } from '@/stores/uiStore';
import { useSettingsStore } from '@/stores/settingsStore';

type SourceType = 'auto' | 'claude' | 'codex';

interface ImportInstructionsResult {
  status: 'written' | 'appended' | 'skipped' | 'error';
  path?: string;
  source?: string;
  reason?: string;
}

interface ImportSkillEntry {
  name: string;
  path?: string;
  reason?: string;
}

interface ImportSkillsResult {
  copied: ImportSkillEntry[];
  skipped: ImportSkillEntry[];
}

interface ImportMcpSkipped {
  name: string;
  reason: string;
}

interface ImportMcpResult {
  added: string[];
  skipped: ImportMcpSkipped[];
  source?: string | null;
  reason?: string;
  error?: string;
}

interface ImportSummary {
  source_type: 'claude' | 'codex';
  tool: string;
  source: string;
  workspace: string;
  instructions: ImportInstructionsResult;
  skills: ImportSkillsResult;
  mcp: ImportMcpResult;
}

const INSTRUCTIONS_LABEL: Record<ImportInstructionsResult['status'], string> = {
  written: 'WHISPER.md created',
  appended: 'Appended to existing WHISPER.md',
  skipped: 'Skipped',
  error: 'Failed',
};

/** Panel next to MCPSettings/SkillsPanel: bridges an existing Claude Code or
 *  Codex CLI project into whisper's native WHISPER.md / folder-skills / MCP
 *  formats via POST /api/onboarding/import. A pure translation step — nothing
 *  it imports runs until the normal chat/skill path reads or invokes it. */
export const ImportPanel: React.FC = () => {
  const [sourcePath, setSourcePath] = useState('');
  const [workspacePath, setWorkspacePath] = useState('');
  const [sourceType, setSourceType] = useState<SourceType>('auto');
  const [importing, setImporting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<ImportSummary | null>(null);
  const addToast = useUIStore((s) => s.addToast);

  const handleBrowseSource = useCallback(async () => {
    try {
      const res = await get<{ path: string | null; cancelled?: boolean; error?: string }>(
        '/api/workspace/pick-folder',
      );
      if (res.path) setSourcePath(res.path);
    } catch {
      addToast({
        type: 'error',
        message: 'Native folder picker is not available on this platform.',
      });
    }
  }, [addToast]);

  const handleBrowseWorkspace = useCallback(async () => {
    try {
      const res = await get<{ path: string | null; cancelled?: boolean; error?: string }>(
        '/api/workspace/pick-folder',
      );
      if (res.path) setWorkspacePath(res.path);
    } catch {
      addToast({
        type: 'error',
        message: 'Native folder picker is not available on this platform.',
      });
    }
  }, [addToast]);

  const handleImport = useCallback(async () => {
    const source = sourcePath.trim();
    if (!source) {
      setError('Source path is required.');
      return;
    }
    setImporting(true);
    setError(null);
    setResult(null);
    try {
      const body: Record<string, string> = { source_path: source };
      if (workspacePath.trim()) body.workspace_path = workspacePath.trim();
      if (sourceType !== 'auto') body.source_type = sourceType;
      const summary = await post<ImportSummary>('/api/onboarding/import', body);
      setResult(summary);
      addToast({ type: 'success', message: `Imported ${summary.tool} project`, persist: false });
      // The import may have added folder skills and/or MCP servers — refresh
      // the composer's cached copies so they show up without a reload.
      await useSettingsStore.getState().loadSkills();
      await useSettingsStore.getState().loadMCP();
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Import failed';
      setError(message);
      addToast({ type: 'error', message: `Import failed: ${message}` });
    } finally {
      setImporting(false);
    }
  }, [sourcePath, workspacePath, sourceType, addToast]);

  return (
    <div className="settings-panel import-panel">
      <div className="settings-panel-header">
        <h3>Import project</h3>
      </div>
      <p className="plugins-intro">
        Bring an existing Claude Code or Codex CLI project's instructions, skills, and MCP servers
        into this app. The source project's files are read, never modified.
      </p>

      <div className="settings-form">
        <label htmlFor="import-source-path">Source project folder</label>
        <div style={{ display: 'flex', gap: 6 }}>
          <input
            id="import-source-path"
            className="settings-input"
            placeholder="/path/to/existing/project"
            value={sourcePath}
            onChange={(e) => setSourcePath(e.target.value)}
            style={{ flex: 1 }}
          />
          <button className="btn btn-sm" type="button" onClick={() => void handleBrowseSource()}>
            Browse…
          </button>
        </div>

        <label htmlFor="import-workspace-path">Workspace to import into</label>
        <div style={{ display: 'flex', gap: 6 }}>
          <input
            id="import-workspace-path"
            className="settings-input"
            placeholder="Leave blank to use the connected workspace"
            value={workspacePath}
            onChange={(e) => setWorkspacePath(e.target.value)}
            style={{ flex: 1 }}
          />
          <button className="btn btn-sm" type="button" onClick={() => void handleBrowseWorkspace()}>
            Browse…
          </button>
        </div>

        <label htmlFor="import-source-type">Source type</label>
        <select
          id="import-source-type"
          className="settings-input"
          value={sourceType}
          onChange={(e) => setSourceType(e.target.value as SourceType)}
        >
          <option value="auto">Auto-detect</option>
          <option value="claude">Claude Code</option>
          <option value="codex">Codex CLI</option>
        </select>
      </div>

      <div className="settings-toolbar" style={{ marginTop: 10 }}>
        <button
          className="btn btn-primary btn-sm"
          type="button"
          disabled={importing || !sourcePath.trim()}
          onClick={() => void handleImport()}
        >
          {importing ? 'Importing…' : 'Import'}
        </button>
      </div>

      {error && (
        <p className="settings-empty" role="alert">
          {error}
        </p>
      )}

      {result && (
        <div className="settings-list" style={{ marginTop: 12 }}>
          <div
            className="settings-item"
            style={{ flexDirection: 'column', alignItems: 'flex-start' }}
          >
            <div className="settings-item-name">Instructions</div>
            <div className="settings-item-desc">
              {INSTRUCTIONS_LABEL[result.instructions.status]}
              {result.instructions.reason ? ` — ${result.instructions.reason}` : ''}
              {result.instructions.path ? ` (${result.instructions.path})` : ''}
            </div>
          </div>

          <div
            className="settings-item"
            style={{ flexDirection: 'column', alignItems: 'flex-start' }}
          >
            <div className="settings-item-name">
              Skills — {result.skills.copied.length} copied, {result.skills.skipped.length} skipped
            </div>
            {result.skills.copied.length > 0 && (
              <div className="settings-item-desc">
                Copied: {result.skills.copied.map((s) => s.name).join(', ')}
              </div>
            )}
            {result.skills.skipped.map((s) => (
              <div className="settings-item-desc" key={s.name}>
                Skipped {s.name}: {s.reason}
              </div>
            ))}
          </div>

          <div
            className="settings-item"
            style={{ flexDirection: 'column', alignItems: 'flex-start' }}
          >
            <div className="settings-item-name">
              MCP servers — {result.mcp.added.length} added, {result.mcp.skipped.length} skipped
            </div>
            {result.mcp.added.length > 0 && (
              <div className="settings-item-desc">Added: {result.mcp.added.join(', ')}</div>
            )}
            {result.mcp.skipped.map((s) => (
              <div className="settings-item-desc" key={s.name}>
                Skipped {s.name}: {s.reason}
              </div>
            ))}
            {result.mcp.reason && <div className="settings-item-desc">{result.mcp.reason}</div>}
            {result.mcp.error && <div className="settings-item-desc">{result.mcp.error}</div>}
          </div>
        </div>
      )}
    </div>
  );
};
