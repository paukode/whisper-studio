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

// Friendly labels for the ApprovalSpec categories declared in
// server/approval/bootstrap.py. Falls back to the raw category name so a
// category added later (or the dynamic github ones) still renders instead of
// vanishing.
const CATEGORY_LABELS: Record<string, string> = {
  write: 'File writes',
  delete: 'File deletes',
  cli: 'Shell commands',
  worktree: 'Worktrees',
  preview: 'Live preview',
  github: 'GitHub',
  'github-destructive': 'GitHub (destructive)',
  workflow: 'Workflow runs',
  'folder-access': 'Folder access',
  save: 'Saving files outside the workspace',
  'office-script': 'Word/Excel/PowerPoint scripts',
};
const categoryLabel = (cat: string): string => CATEGORY_LABELS[cat] ?? cat;

interface PermissionRule {
  tool: string;
  pattern?: string;
  prefix?: string;
  action: 'allow' | 'ask' | 'deny';
  justification?: string;
}

// Egress control for sandboxed commands (server/sandbox.py +
// server/security/egress_policy.py). DEFAULT-OFF: "permissive" matches
// today's behavior exactly (no domain restriction, no proxy).
type NetworkPolicyTier = 'permissive' | 'curated' | 'restrictive';

interface NetworkPolicy {
  tier: NetworkPolicyTier;
  custom_allowed_domains: string[];
  methods_only_safe: boolean;
}

const DEFAULT_NETWORK_POLICY: NetworkPolicy = {
  tier: 'permissive',
  custom_allowed_domains: [],
  methods_only_safe: false,
};

const TIER_OPTIONS: { value: NetworkPolicyTier; label: string }[] = [
  { value: 'permissive', label: 'Permissive: no restriction (default)' },
  { value: 'curated', label: 'Curated: common package/docs domains + custom' },
  { value: 'restrictive', label: 'Restrictive: nothing outbound except custom domains' },
];

interface PermissionsData {
  mode: string;
  rules: PermissionRule[];
  category_modes?: Record<string, string>;
  categories?: string[];
  network_policy?: NetworkPolicy;
}

interface RuleTestResult {
  decision: 'allow' | 'ask' | 'deny' | null;
  matched_index: number | null;
  matched_rule: PermissionRule | null;
}

const DEFAULT_TEST_INPUT = '{"path": "example.txt", "command": "example command"}';

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
  const [ruleJustification, setRuleJustification] = useState('');

  // "Test this rule": a sample tool-input JSON blob plus the last result.
  const [testInput, setTestInput] = useState(DEFAULT_TEST_INPUT);
  const [testResult, setTestResult] = useState<RuleTestResult | null>(null);
  const [testError, setTestError] = useState<string | null>(null);
  const [testing, setTesting] = useState(false);

  // Per-category default approval modes: category -> mode, or unset for
  // "use the global mode above".
  const [categories, setCategories] = useState<string[]>([]);
  const [categoryModes, setCategoryModes] = useState<Record<string, string>>({});

  // Egress / network policy for sandboxed commands. custom_allowed_domains
  // is edited as newline-separated text and split/joined at the boundary.
  const [networkPolicy, setNetworkPolicy] = useState<NetworkPolicy>(DEFAULT_NETWORK_POLICY);
  const [customDomainsText, setCustomDomainsText] = useState('');

  // Inline save feedback: one for the mode button, one shared by the rule
  // add/delete actions (shown near the rules toolbar so it survives the
  // editor closing on a successful save), one for the category-mode rows,
  // one for the network policy form.
  const saveMode = useSaveStatus();
  const ruleStatus = useSaveStatus();
  const categoryModeStatus = useSaveStatus();
  const saveNetworkPolicy = useSaveStatus();

  // Folders outside the workspace the user has approved access to. Granted
  // only through the approval card (never from here — this panel can revoke
  // but not widen reach); listed so a grant is never an invisible one-way
  // door.
  const [folderGrants, setFolderGrants] = useState<string[]>([]);
  const folderGrantStatus = useSaveStatus();

  const loadFolderGrants = useCallback(async () => {
    try {
      const data = await get<{ granted: string[] }>('/api/permissions/folder-grants');
      setFolderGrants(Array.isArray(data.granted) ? data.granted : []);
    } catch {
      setFolderGrants([]);
    }
  }, []);

  const handleRevokeFolderGrant = (path: string) =>
    folderGrantStatus.run(
      async () => {
        await del(`/api/permissions/folder-grants?path=${encodeURIComponent(path)}`);
        await loadFolderGrants();
      },
      { saving: 'Revoking…', saved: 'Access revoked', error: 'Could not revoke' },
    );

  // Load permissions on mount
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const data = await get<PermissionsData>('/api/permissions');
        if (cancelled) return;
        setMode(data.mode ?? 'default');
        setRules(Array.isArray(data.rules) ? data.rules : []);
        setCategories(Array.isArray(data.categories) ? data.categories : []);
        setCategoryModes(data.category_modes ?? {});
        updateConfig({ permissionMode: data.mode ?? 'default' });
        // Sync plan mode on initial load
        useSettingsStore.getState().setPlanMode((data.mode ?? 'default') === 'plan');
        const np = { ...DEFAULT_NETWORK_POLICY, ...(data.network_policy ?? {}) };
        setNetworkPolicy(np);
        setCustomDomainsText((np.custom_allowed_domains ?? []).join('\n'));
      } catch (err) {
        console.warn('Failed to load permissions:', err);
      }
      if (!cancelled) await loadFolderGrants();
    })();
    return () => { cancelled = true; };
  }, [updateConfig, loadFolderGrants]);

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

  const handleSaveNetworkPolicy = useCallback(() => {
    const custom_allowed_domains = customDomainsText
      .split('\n')
      .map((d) => d.trim())
      .filter(Boolean);
    const next: NetworkPolicy = { ...networkPolicy, custom_allowed_domains };
    void saveNetworkPolicy.run(async () => {
      await put('/api/permissions', { network_policy: next });
      setNetworkPolicy(next);
    });
  }, [networkPolicy, customDomainsText, saveNetworkPolicy]);

  const handleAddRule = useCallback(() => {
    setRuleTool('');
    setRuleMatchType('pattern');
    setRulePattern('');
    setRuleAction('allow');
    setRuleJustification('');
    setTestInput(DEFAULT_TEST_INPUT);
    setTestResult(null);
    setTestError(null);
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
    if (ruleJustification.trim()) {
      body.justification = ruleJustification.trim();
    }
    void ruleStatus.run(async () => {
      await post('/api/permissions/rules', body);
      // Reload permissions
      const data = await get<PermissionsData>('/api/permissions');
      setRules(Array.isArray(data.rules) ? data.rules : []);
      setRuleEditorOpen(false);
    }, { saving: 'Saving rule…', saved: 'Rule saved', error: 'Failed to save rule' });
  }, [ruleTool, ruleMatchType, rulePattern, ruleAction, ruleJustification, ruleStatus]);

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

  // Tests the rule currently in the editor — as a draft, before it's saved —
  // against a sample tool input the user supplies as JSON.
  const handleTestRule = useCallback(async () => {
    const tool = ruleTool.trim();
    if (!tool) return;
    let input: unknown;
    try {
      input = testInput.trim() ? JSON.parse(testInput) : {};
    } catch {
      setTestResult(null);
      setTestError('Sample input must be valid JSON.');
      return;
    }
    const draftRule: Record<string, string> = { tool, action: ruleAction };
    if (ruleMatchType === 'pattern') {
      draftRule.pattern = rulePattern;
    } else {
      draftRule.prefix = rulePattern;
    }
    setTestError(null);
    setTesting(true);
    try {
      const result = await post<RuleTestResult>('/api/permissions/rules/test', {
        tool,
        input,
        rules: [draftRule],
      });
      setTestResult(result);
    } catch {
      setTestResult(null);
      setTestError('Test failed to run.');
    } finally {
      setTesting(false);
    }
  }, [ruleTool, ruleMatchType, rulePattern, ruleAction, testInput]);

  const handleCategoryModeChange = useCallback((category: string, value: string) => {
    setCategoryModes((prev) => {
      if (!value) {
        const { [category]: _drop, ...rest } = prev;
        return rest;
      }
      return { ...prev, [category]: value };
    });
  }, []);

  const handleSaveCategoryModes = useCallback(() => {
    void categoryModeStatus.run(async () => {
      const data = await put<PermissionsData>('/api/permissions', { category_modes: categoryModes });
      setCategoryModes(data.category_modes ?? {});
    }, { saving: 'Saving…', saved: 'Category settings saved', error: 'Failed to save category settings' });
  }, [categoryModes, categoryModeStatus]);

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

      {/* Per-category default overrides */}
      {categories.length > 0 && (
        <div className="settings-form" style={{ marginTop: '1.5rem' }}>
          <h4>Per-Category Defaults</h4>
          <p className="settings-hint">
            Override the permission mode above for a specific kind of action — e.g. auto-approve
            reads while always confirming deletes — without loosening or tightening everything at once.
          </p>
          {categories.map((cat) => {
            const isDestructive = cat === 'github-destructive';
            return (
              <React.Fragment key={cat}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 8 }}>
                  <label style={{ flex: '0 0 180px' }} htmlFor={`categoryMode-${cat}`}>
                    {categoryLabel(cat)}
                  </label>
                  <select
                    id={`categoryMode-${cat}`}
                    className="settings-input"
                    style={{ flex: 1 }}
                    value={isDestructive ? '' : (categoryModes[cat] ?? '')}
                    disabled={isDestructive}
                    onChange={(e) => handleCategoryModeChange(cat, e.target.value)}
                  >
                    <option value="">
                      {isDestructive ? 'Always asks (cannot be overridden)' : 'Use global setting'}
                    </option>
                    {!isDestructive && MODE_OPTIONS.map((m) => (
                      <option key={m.value} value={m.value}>{m.label}</option>
                    ))}
                  </select>
                </div>
                {cat === 'workflow' && (
                  <p className="settings-hint" style={{ margin: '4px 0 0 188px' }}>
                    For workflows, Auto launches new scripts budgeted up to 600,000 output tokens
                    without the approval card; larger budgets still ask. Bypass never asks and
                    Don&apos;t ask declines launches. Trusted saved workflows always run directly.
                  </p>
                )}
              </React.Fragment>
            );
          })}
          <div style={{ marginTop: 12, display: 'flex', alignItems: 'center', gap: 8 }}>
            <button className="btn btn-primary btn-sm" onClick={handleSaveCategoryModes}>
              Save Category Settings
            </button>
            <SaveStatus status={categoryModeStatus} />
          </div>
        </div>
      )}

      {/* Egress / network policy for sandboxed commands (terminal_run, aws_cli,
          run_python, hooks). Default-off: "permissive" is today's behavior. */}
      <div className="settings-form">
        <h4>Sandbox Network Policy</h4>
        <p className="settings-item-desc" style={{ marginBottom: 8 }}>
          Restricts which internet domains commands run inside the sandbox
          (terminal, AWS CLI, Python) can reach. Permissive matches today's
          behavior exactly — nothing changes unless you switch tiers.
        </p>

        <label htmlFor="networkPolicyTierSelect">Tier</label>
        <select
          id="networkPolicyTierSelect"
          className="settings-input"
          value={networkPolicy.tier}
          onChange={(e) =>
            setNetworkPolicy((np) => ({ ...np, tier: e.target.value as NetworkPolicyTier }))
          }
        >
          {TIER_OPTIONS.map((t) => (
            <option key={t.value} value={t.value}>{t.label}</option>
          ))}
        </select>

        <label htmlFor="networkPolicyDomainsInput">Custom allowed domains (one per line)</label>
        <textarea
          id="networkPolicyDomainsInput"
          className="settings-input"
          rows={4}
          placeholder={'example.com\ninternal.example.org'}
          value={customDomainsText}
          onChange={(e) => setCustomDomainsText(e.target.value)}
        />

        <div style={{ marginTop: 8, display: 'flex', alignItems: 'center', gap: 6 }}>
          <input
            id="networkPolicyMethodsOnlySafe"
            type="checkbox"
            checked={networkPolicy.methods_only_safe}
            onChange={(e) =>
              setNetworkPolicy((np) => ({ ...np, methods_only_safe: e.target.checked }))
            }
          />
          <label htmlFor="networkPolicyMethodsOnlySafe" style={{ margin: 0 }}>
            Safe methods only (GET/HEAD/OPTIONS) — best-effort, only enforced on
            plain (non-HTTPS-tunneled) requests, see docs
          </label>
        </div>

        <div style={{ marginTop: 12, display: 'flex', alignItems: 'center', gap: 8 }}>
          <button className="btn btn-primary btn-sm" onClick={handleSaveNetworkPolicy}>
            Save Network Policy
          </button>
          <SaveStatus status={saveNetworkPolicy} />
        </div>
      </div>

      {/* Folder access grants — approved once, remembered after */}
      <div className="permissions-rules">
        <h4>Folder Access</h4>
        <p className="settings-hint">
          Folders outside the connected workspace that you have allowed this app to
          read. You are asked once per folder; a grant also covers everything inside
          it. Revoking makes the app ask again next time.
        </p>
        <div className="settings-toolbar">
          <SaveStatus status={folderGrantStatus} style={{ marginRight: 'auto' }} />
        </div>
        <div className="settings-list">
          {folderGrants.length === 0 && (
            <p className="settings-empty">
              No folders approved yet. Approvals appear here after you allow one.
            </p>
          )}
          {folderGrants.map((path) => (
            <div key={path} className="settings-item">
              <div className="settings-item-info">
                <div className="settings-item-name">{path}</div>
                <div className="settings-item-desc">Includes everything inside this folder</div>
              </div>
              <div className="settings-item-actions">
                <button
                  className="btn btn-sm"
                  style={{ color: 'var(--accent-record)' }}
                  onClick={() => void handleRevokeFolderGrant(path)}
                >
                  Revoke
                </button>
              </div>
            </div>
          ))}
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
                {rule.justification && (
                  <div className="settings-item-desc" style={{ opacity: 0.7, fontStyle: 'italic' }}>
                    {rule.justification}
                  </div>
                )}
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

            <label htmlFor="ruleJustificationInput">Justification (optional)</label>
            <input
              id="ruleJustificationInput"
              type="text"
              className="settings-input"
              placeholder="Why does this rule exist?"
              value={ruleJustification}
              onChange={(e) => setRuleJustification(e.target.value)}
            />

            <label htmlFor="ruleTestInput" style={{ marginTop: 8 }}>Test input (JSON)</label>
            <input
              id="ruleTestInput"
              type="text"
              className="settings-input"
              style={{ fontFamily: 'var(--font-mono, monospace)' }}
              placeholder={DEFAULT_TEST_INPUT}
              value={testInput}
              onChange={(e) => setTestInput(e.target.value)}
            />

            {testError && <p className="settings-empty">{testError}</p>}
            {testResult && (
              <div className="settings-item" style={{ marginTop: 8, flexDirection: 'column', alignItems: 'stretch' }}>
                <div className="settings-item-name">
                  Test →{' '}
                  <span
                    style={{
                      color:
                        testResult.decision === 'deny'
                          ? 'var(--accent-record)'
                          : testResult.decision === 'allow'
                            ? 'var(--accent-ok, #2e7d32)'
                            : 'var(--accent-warn, #b8860b)',
                    }}
                  >
                    {testResult.decision ?? 'no match'}
                  </span>
                </div>
                <div className="settings-item-desc" style={{ opacity: 0.7 }}>
                  {testResult.matched_rule
                    ? 'This rule matched the sample input.'
                    : 'No rule matched — the sample input would fall through to the permission mode.'}
                </div>
              </div>
            )}

            <div className="editor-actions" style={{ marginTop: '0.5rem' }}>
              <button className="btn btn-primary btn-sm" onClick={() => void handleSaveRule()}>Save Rule</button>
              <button className="btn btn-sm" onClick={() => void handleTestRule()} disabled={testing}>
                {testing ? 'Testing…' : 'Test this rule'}
              </button>
              <button className="btn btn-sm" onClick={handleCancelRule}>Cancel</button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
};
