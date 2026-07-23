import React, { useCallback, useMemo, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { get, post, put, del } from '@/api/client';
import { useUIStore } from '@/stores/uiStore';

interface HookRow {
  id: string;
  event: string;
  matcher: string;
  command: string;
  timeout: number;
  enabled: boolean;
  on_error: string;
  source: string;
}

interface HooksResponse {
  version: number;
  available_events: string[];
  hooks: Record<string, HookRow[]>;
  project: {
    workspace: string | null;
    status: 'none' | 'trusted' | 'pending_approval';
    hooks: Record<string, HookRow[]>;
  };
}

interface TestResult {
  exit_code: number;
  stdout: string;
  stderr: string;
  decision: string;
  reason: string;
}

const FALLBACK_EVENTS = [
  'PreToolUse',
  'PostToolUse',
  'PostToolUseFailure',
  'UserPromptSubmit',
  'SessionStart',
  'Stop',
];

const EVENT_HELP: Record<string, string> = {
  PreToolUse: 'Before a tool runs. Exit 2 (or JSON deny) blocks it; JSON rewrite edits its input.',
  PostToolUse: 'After a tool succeeds. stdout is fed back to the model as context.',
  PostToolUseFailure: 'After a tool errors. stdout is fed back to the model.',
  UserPromptSubmit: 'When you send a message. stdout is added as context.',
  SessionStart: 'At the start of a turn. Good for loading project conventions.',
  Stop: 'When the turn is about to end. Exit 2 keeps the model working toward the goal.',
};

const BLANK = { event: 'PreToolUse', matcher: '*', command: '', timeout: 10, on_error: 'ignore', enabled: true };

export const HooksPanel: React.FC = () => {
  const queryClient = useQueryClient();
  const hooksQuery = useQuery({
    queryKey: ['hooks'],
    queryFn: () => get<HooksResponse>('/api/hooks'),
    staleTime: 5 * 60_000,
  });
  const data = hooksQuery.data;
  const events = data?.available_events ?? FALLBACK_EVENTS;

  const rows = useMemo<HookRow[]>(() => {
    if (!data?.hooks) return [];
    return events.flatMap((ev) => data.hooks[ev] ?? []);
  }, [data, events]);

  const projectRows = useMemo<HookRow[]>(() => {
    if (!data?.project?.hooks) return [];
    return events.flatMap((ev) => data.project.hooks[ev] ?? []);
  }, [data, events]);

  const [error, setError] = useState<string | null>(null);
  const addToast = useUIStore((s) => s.addToast);
  const [isEditorOpen, setIsEditorOpen] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [form, setForm] = useState(BLANK);
  const [test, setTest] = useState<TestResult | null>(null);

  const displayError = error ?? (hooksQuery.isError ? 'Could not load hooks. The hooks API may not be available.' : null);

  const startAdd = useCallback(() => {
    setEditingId(null);
    setForm(BLANK);
    setTest(null);
    setError(null);
    setIsEditorOpen(true);
  }, []);

  const startEdit = useCallback((h: HookRow) => {
    setEditingId(h.id);
    setForm({ event: h.event, matcher: h.matcher, command: h.command, timeout: h.timeout, on_error: h.on_error, enabled: h.enabled });
    setTest(null);
    setError(null);
    setIsEditorOpen(true);
  }, []);

  const closeEditor = useCallback(() => {
    setIsEditorOpen(false);
    setEditingId(null);
    setTest(null);
  }, []);

  const handleSave = useCallback(async () => {
    if (!form.command.trim()) return;
    try {
      if (editingId !== null) {
        await put(`/api/hooks/${encodeURIComponent(editingId)}`, form);
      } else {
        await post('/api/hooks', form);
      }
      setError(null);
      closeEditor();
      void queryClient.invalidateQueries({ queryKey: ['hooks'] });
      addToast({ type: 'success', message: editingId !== null ? 'Hook updated' : 'Hook added' });
    } catch {
      setError(editingId !== null ? 'Failed to update hook.' : 'Failed to add hook.');
    }
  }, [form, editingId, queryClient, closeEditor, addToast]);

  const handleDelete = useCallback(async (id: string) => {
    try {
      await del(`/api/hooks/${encodeURIComponent(id)}`);
      void queryClient.invalidateQueries({ queryKey: ['hooks'] });
      addToast({ type: 'success', message: 'Hook deleted' });
    } catch {
      setError('Failed to delete hook.');
    }
  }, [queryClient, addToast]);

  const toggleEnabled = useCallback(async (h: HookRow) => {
    try {
      await put(`/api/hooks/${encodeURIComponent(h.id)}`, { ...h, enabled: !h.enabled });
      void queryClient.invalidateQueries({ queryKey: ['hooks'] });
      addToast({ type: 'success', message: h.enabled ? 'Hook disabled' : 'Hook enabled' });
    } catch {
      setError('Failed to update hook.');
    }
  }, [queryClient, addToast]);

  const runTest = useCallback(async () => {
    if (!form.command.trim()) return;
    try {
      const res = await post<TestResult>('/api/hooks/test', {
        command: form.command,
        event: form.event,
        timeout: form.timeout,
      });
      setTest(res);
    } catch {
      setTest({ exit_code: -1, stdout: '', stderr: 'Test failed to run.', decision: 'error', reason: '' });
    }
  }, [form]);

  const approveProject = useCallback(async () => {
    try {
      await post('/api/hooks/project/approve', {});
      void queryClient.invalidateQueries({ queryKey: ['hooks'] });
      addToast({ type: 'success', message: 'Project hooks trusted' });
    } catch {
      setError('Failed to trust project hooks.');
    }
  }, [queryClient, addToast]);

  const revokeProject = useCallback(async () => {
    try {
      await post('/api/hooks/project/revoke', {});
      void queryClient.invalidateQueries({ queryKey: ['hooks'] });
      addToast({ type: 'success', message: 'Project hooks revoked' });
    } catch {
      setError('Failed to revoke project hooks.');
    }
  }, [queryClient, addToast]);

  const projectPendingCount = useMemo(() => {
    if (data?.project?.status !== 'pending_approval') return 0;
    return Object.values(data.project.hooks).reduce((n, arr) => n + arr.length, 0);
  }, [data]);

  const matcherLabel = form.event === 'PreToolUse' || form.event === 'PostToolUse' || form.event === 'PostToolUseFailure';

  return (
    <div className="settings-form" style={{ maxWidth: 620 }}>
      <p className="settings-hint">
        Shell commands that run on lifecycle events and can <strong>block</strong> or <strong>steer</strong> the loop.
        A <code>PreToolUse</code> hook that exits 2 denies the tool; a <code>Stop</code> hook that exits 2 keeps
        the model working toward the goal. The full event JSON arrives on <code>stdin</code>.
      </p>

      {data?.project?.status === 'pending_approval' && (
        <div className="settings-editor" style={{ borderColor: 'var(--accent-warn, #b8860b)' }}>
          <div className="settings-item-name">
            Project hooks need review ({projectPendingCount})
          </div>
          <div className="settings-item-desc" style={{ marginBottom: 8 }}>
            This workspace defines the following hook{projectPendingCount === 1 ? '' : 's'} in
            <code> .whisper/settings.json</code>. This is arbitrary shell code from the repo — read every
            command before trusting. They stay inert until you do.
          </div>
          {projectRows.map((h) => (
            <div key={h.id} className="settings-item">
              <div className="settings-item-info">
                <div className="settings-item-name">
                  {h.event}{h.matcher && h.matcher !== '*' ? ` · ${h.matcher}` : ''}
                </div>
                <div className="settings-item-desc" style={{ fontFamily: 'var(--font-mono, monospace)' }}>
                  {h.command}
                </div>
              </div>
            </div>
          ))}
          <div style={{ marginTop: 8 }}>
            <button className="btn btn-sm btn-primary" onClick={() => void approveProject()} type="button">
              Trust these hooks
            </button>
          </div>
        </div>
      )}

      {data?.project?.status === 'trusted' && projectRows.length > 0 && (
        <div className="settings-editor">
          <div className="settings-item-name" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
            <span>Trusted project hooks ({projectRows.length})</span>
            <button className="btn btn-sm" style={{ color: 'var(--accent-record)' }} onClick={() => void revokeProject()} type="button">
              Revoke trust
            </button>
          </div>
          {projectRows.map((h) => (
            <div key={h.id} className="settings-item">
              <div className="settings-item-info">
                <div className="settings-item-name">
                  {h.event}{h.matcher && h.matcher !== '*' ? ` · ${h.matcher}` : ''} · project
                </div>
                <div className="settings-item-desc" style={{ fontFamily: 'var(--font-mono, monospace)' }}>
                  {h.command}
                </div>
              </div>
            </div>
          ))}
        </div>
      )}

      <div className="settings-toolbar">
        <button className="btn btn-sm" id="addHookBtn" onClick={startAdd} type="button" disabled={isEditorOpen}>
          + Add Hook
        </button>
      </div>

      {displayError && <p className="settings-empty">{displayError}</p>}

      <div className="settings-list" id="hooksList">
        {rows.length === 0 && !displayError && !isEditorOpen && (
          <p className="settings-empty">No hooks configured.</p>
        )}
        {rows.map((h) => (
          <div key={h.id} className="settings-item" style={{ opacity: h.enabled ? 1 : 0.5 }}>
            <div className="settings-item-info">
              <div className="settings-item-name">
                {h.event}
                {h.matcher && h.matcher !== '*' ? ` · ${h.matcher}` : ''}
                {h.on_error === 'block' ? ' · fail-closed' : ''}
              </div>
              <div className="settings-item-desc" style={{ fontFamily: 'var(--font-mono, monospace)' }}>
                {h.command}
              </div>
            </div>
            <div className="settings-item-actions">
              <button className="btn btn-sm" onClick={() => void toggleEnabled(h)} type="button">
                {h.enabled ? 'Disable' : 'Enable'}
              </button>
              <button className="btn btn-sm" onClick={() => startEdit(h)} type="button">Edit</button>
              <button
                className="btn btn-sm"
                style={{ color: 'var(--accent-record)' }}
                onClick={() => void handleDelete(h.id)}
                type="button"
              >
                Delete
              </button>
            </div>
          </div>
        ))}
      </div>

      {isEditorOpen && (
        <div className="settings-editor" id="hookEditor">
          <div className="settings-form">
            <label>Event</label>
            <select
              className="settings-input"
              id="hookEvent"
              value={form.event}
              onChange={(e) => setForm((p) => ({ ...p, event: e.target.value }))}
            >
              {events.map((ev) => (
                <option key={ev} value={ev}>{ev}</option>
              ))}
            </select>
            <p className="settings-hint" style={{ marginTop: 4 }}>{EVENT_HELP[form.event] ?? ''}</p>

            {matcherLabel && (
              <>
                <label>Tool matcher (* = all, <code>a|b</code>, or <code>/regex/</code>)</label>
                <input
                  className="settings-input"
                  id="hookMatcher"
                  placeholder="* or ws_write_file|ws_edit_file"
                  value={form.matcher}
                  onChange={(e) => setForm((p) => ({ ...p, matcher: e.target.value }))}
                />
              </>
            )}

            <label>Shell command (event JSON on stdin)</label>
            <input
              className="settings-input"
              id="hookCommand"
              placeholder='jq -e ".tool_input.path | test(\"secret\") | not" || exit 2'
              value={form.command}
              onChange={(e) => setForm((p) => ({ ...p, command: e.target.value }))}
            />

            <div style={{ display: 'flex', gap: 12 }}>
              <div style={{ flex: 1 }}>
                <label>Timeout (s)</label>
                <input
                  className="settings-input"
                  type="number"
                  min={1}
                  max={60}
                  value={form.timeout}
                  onChange={(e) => setForm((p) => ({ ...p, timeout: Number(e.target.value) }))}
                />
              </div>
              <div style={{ flex: 1 }}>
                <label>On error</label>
                <select
                  className="settings-input"
                  value={form.on_error}
                  onChange={(e) => setForm((p) => ({ ...p, on_error: e.target.value }))}
                >
                  <option value="ignore">ignore (fail-open)</option>
                  <option value="block">block (fail-closed)</option>
                </select>
              </div>
            </div>

            <label style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 8 }}>
              <input
                type="checkbox"
                checked={form.enabled}
                onChange={(e) => setForm((p) => ({ ...p, enabled: e.target.checked }))}
              />
              Enabled
            </label>

            {test && (
              <div className="settings-item" style={{ marginTop: 8, flexDirection: 'column', alignItems: 'stretch' }}>
                <div className="settings-item-name">
                  Test → exit {test.exit_code} ·{' '}
                  <span style={{ color: test.decision === 'deny' ? 'var(--accent-record)' : test.decision === 'error' ? 'var(--accent-warn, #b8860b)' : 'var(--accent-ok, #2e7d32)' }}>
                    {test.decision}
                  </span>
                  {test.reason ? ` — ${test.reason}` : ''}
                </div>
                {test.decision === 'error' && (
                  <div className="settings-item-desc">
                    A non-0/2 exit is an infra error → at runtime this hook is{' '}
                    {form.on_error === 'block' ? 'a deny (fail-closed)' : 'ignored (fail-open)'}.
                  </div>
                )}
                {(test.stdout || test.stderr) && (
                  <pre className="settings-item-desc" style={{ whiteSpace: 'pre-wrap', maxHeight: 120, overflow: 'auto' }}>
                    {test.stdout}{test.stderr ? `\n[stderr] ${test.stderr}` : ''}
                  </pre>
                )}
                <div className="settings-item-desc" style={{ opacity: 0.7 }}>
                  Test runs the raw command against a sample payload; the tool matcher isn't applied.
                </div>
              </div>
            )}

            <div style={{ marginTop: 8, display: 'flex', gap: 8 }}>
              <button className="btn btn-primary btn-sm" id="saveHookBtn" onClick={() => void handleSave()} type="button">
                {editingId !== null ? 'Update Hook' : 'Save Hook'}
              </button>
              <button className="btn btn-sm" onClick={() => void runTest()} type="button">Test</button>
              <button className="btn btn-sm" id="cancelHookBtn" onClick={closeEditor} type="button">Cancel</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};
