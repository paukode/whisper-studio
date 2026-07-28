import React, { useCallback, useEffect, useState } from 'react';
import { useSettingsStore } from '@/stores/settingsStore';
import { get, put, post, del } from '@/api/client';
import { PERMISSION_MODES } from '@/utils/permissionModes';
import { useSaveStatus } from '@/hooks/useSaveStatus';
import { SaveStatus } from '@/components/common/SaveStatus';

// Select options: the shared friendly label plus a short reminder of what the
// mode does (the full behavior text lives in the shared module's `help`).
const MODE_HINTS: Record<string, string> = {
  default: 'ask for writes',
  auto: 'AI classifier decides',
  plan: 'read-only, offers upgrade on writes',
  acceptEdits: 'auto-allow file edits',
  bypassPermissions: 'allow everything',
  dontAsk: 'auto-deny writes silently',
};
const MODE_OPTIONS = PERMISSION_MODES.map((m) => ({
  value: m.value,
  label: MODE_HINTS[m.value] ? `${m.label}: ${MODE_HINTS[m.value]}` : m.label,
}));

interface PermissionRule {
  tool: string;
  pattern?: string;
  prefix?: string;
  action: 'allow' | 'ask' | 'deny';
}

interface PermissionsData {
  mode: string;
  rules: PermissionRule[];
}

export const PermissionsPanel: React.FC = () => {
  const config = useSettingsStore((s) => s.config);
  const updateConfig = useSettingsStore((s) => s.updateConfig);

  const [mode, setMode] = useState(config.permissionMode || 'default');
  const [rules, setRules] = useState<PermissionRule[]>([]);
  const [ruleEditorOpen, setRuleEditorOpen] = useState(false);
  const [ruleTool, setRuleTool] = useState('');
  const [ruleMatchType, setRuleMatchType] = useState<'pattern' | 'prefix'>('pattern');
  const [rulePattern, setRulePattern] = useState('');
  const [ruleAction, setRuleAction] = useState<'allow' | 'ask' | 'deny'>('allow');

  // Inline save feedback: one for the mode button, one shared by the rule
  // add/delete actions (shown near the rules toolbar so it survives the
  // editor closing on a successful save).
  const saveMode = useSaveStatus();
  const ruleStatus = useSaveStatus();

  // Load permissions on mount
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const data = await get<PermissionsData>('/api/permissions');
        if (cancelled) return;
        setMode(data.mode ?? 'default');
        setRules(Array.isArray(data.rules) ? data.rules : []);
        updateConfig({ permissionMode: data.mode ?? 'default' });
        // Sync plan mode on initial load
        useSettingsStore.getState().setPlanMode((data.mode ?? 'default') === 'plan');
      } catch (err) {
        console.warn('Failed to load permissions:', err);
      }
    })();
    return () => { cancelled = true; };
  }, [updateConfig]);

  // Re-sync the local selection when the mode changes externally (e.g. the
  // Plan-mode toggle in the composer). During-render previous-value pattern
  // instead of an effect (React Compiler flags setState-in-effect).
  const [prevPermMode, setPrevPermMode] = useState(config.permissionMode);
  if (config.permissionMode !== prevPermMode) {
    setPrevPermMode(config.permissionMode);
    setMode(config.permissionMode || 'default');
  }

  const handleSaveMode = useCallback(() => {
    void saveMode.run(async () => {
      await put('/api/permissions/mode', { mode });
      updateConfig({ permissionMode: mode });
      // Sync plan mode state
      useSettingsStore.getState().setPlanMode(mode === 'plan');
    });
  }, [mode, updateConfig, saveMode]);

  const handleAddRule = useCallback(() => {
    setRuleTool('');
    setRuleMatchType('pattern');
    setRulePattern('');
    setRuleAction('allow');
    setRuleEditorOpen(true);
  }, []);

  const handleSaveRule = useCallback(() => {
    const tool = ruleTool.trim();
    if (!tool) return;
    const body: Record<string, string> = { tool, action: ruleAction };
    if (ruleMatchType === 'pattern') {
      body.pattern = rulePattern;
    } else {
      body.prefix = rulePattern;
    }
    void ruleStatus.run(async () => {
      await post('/api/permissions/rules', body);
      // Reload permissions
      const data = await get<PermissionsData>('/api/permissions');
      setRules(Array.isArray(data.rules) ? data.rules : []);
      setRuleEditorOpen(false);
    }, { saving: 'Saving rule…', saved: 'Rule saved', error: 'Failed to save rule' });
  }, [ruleTool, ruleMatchType, rulePattern, ruleAction, ruleStatus]);

  const handleDeleteRule = useCallback((index: number) => {
    void ruleStatus.run(async () => {
      await del(`/api/permissions/rules/${index}`);
      const data = await get<PermissionsData>('/api/permissions');
      setRules(Array.isArray(data.rules) ? data.rules : []);
    }, { saving: 'Deleting…', saved: 'Rule deleted', error: 'Failed to delete rule' });
  }, [ruleStatus]);

  const handleCancelRule = useCallback(() => {
    setRuleEditorOpen(false);
  }, []);

  return (
    <div className="settings-panel permissions-panel">
      <h3>Permissions</h3>

      {/* Permission mode select */}
      <div className="settings-form">
        <label htmlFor="permissionModeSelect">Permission Mode</label>
        <select
          id="permissionModeSelect"
          className="settings-input"
          value={mode}
          onChange={(e) => setMode(e.target.value)}
        >
          {MODE_OPTIONS.map((m) => (
            <option key={m.value} value={m.value}>{m.label}</option>
          ))}
        </select>
        <div style={{ marginTop: 12, display: 'flex', alignItems: 'center', gap: 8 }}>
          <button className="btn btn-primary btn-sm" onClick={handleSaveMode}>Save Mode</button>
          <SaveStatus status={saveMode} />
        </div>
      </div>

      {/* Permission rules */}
      <div className="permissions-rules">
        <h4>Permission Rules</h4>
        <div className="settings-toolbar">
          <SaveStatus status={ruleStatus} style={{ marginRight: 'auto' }} />
          <button className="btn btn-sm" onClick={handleAddRule}>+ Add Rule</button>
        </div>

        <div className="settings-list">
          {rules.length === 0 && (
            <p className="settings-empty">No permission rules configured.</p>
          )}
          {rules.map((rule, idx) => (
            <div key={idx} className="settings-item">
              <div className="settings-item-info">
                <div className="settings-item-name">{rule.tool}</div>
                <div className="settings-item-desc">
                  {rule.pattern ? `pattern: ${rule.pattern}` : ''}
                  {rule.prefix ? `prefix: ${rule.prefix}` : ''}
                  {' → '}{rule.action}
                </div>
              </div>
              <div className="settings-item-actions">
                <button
                  className="btn btn-sm"
                  style={{ color: 'var(--accent-record)' }}
                  onClick={() => void handleDeleteRule(idx)}
                >
                  Delete
                </button>
              </div>
            </div>
          ))}
        </div>

        {/* Rule editor form */}
        {ruleEditorOpen && (
          <div className="settings-form" style={{ marginTop: '1rem' }}>
            <label htmlFor="ruleToolInput">Tool Name</label>
            <input
              id="ruleToolInput"
              type="text"
              className="settings-input"
              placeholder="Tool name"
              value={ruleTool}
              onChange={(e) => setRuleTool(e.target.value)}
            />

            <label htmlFor="ruleMatchTypeSelect">Match Type</label>
            <select
              id="ruleMatchTypeSelect"
              className="settings-input"
              value={ruleMatchType}
              onChange={(e) => setRuleMatchType(e.target.value as 'pattern' | 'prefix')}
            >
              <option value="pattern">Pattern</option>
              <option value="prefix">Prefix</option>
            </select>

            <label htmlFor="rulePatternInput">{ruleMatchType === 'pattern' ? 'Pattern' : 'Prefix'}</label>
            <input
              id="rulePatternInput"
              type="text"
              className="settings-input"
              placeholder={ruleMatchType === 'pattern' ? 'Pattern' : 'Prefix'}
              value={rulePattern}
              onChange={(e) => setRulePattern(e.target.value)}
            />

            <label htmlFor="ruleActionSelect">Action</label>
            <select
              id="ruleActionSelect"
              className="settings-input"
              value={ruleAction}
              onChange={(e) => setRuleAction(e.target.value as 'allow' | 'ask' | 'deny')}
            >
              <option value="allow">Allow</option>
              <option value="ask">Ask</option>
              <option value="deny">Deny</option>
            </select>

            <div className="editor-actions" style={{ marginTop: '0.5rem' }}>
              <button className="btn btn-primary btn-sm" onClick={() => void handleSaveRule()}>Save Rule</button>
              <button className="btn btn-sm" onClick={handleCancelRule}>Cancel</button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
};
