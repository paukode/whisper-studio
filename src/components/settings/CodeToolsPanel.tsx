import React from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { get, put } from '@/api/client';
import { useSaveToast } from '@/hooks/useSaveToast';
import { HelpTip } from '@/components/common/HelpTip';
import { useUIStore } from '@/stores/uiStore';
import {
  CodeToolsStatusResponseSchema,
  type CodeToolStatus,
  type CodeToolsStatusResponse,
} from '@/types/schemas/codeTools.schema';

/** One tool: what it powers, whether it works here, and why not. */
const CodeToolRow: React.FC<{ tool: CodeToolStatus }> = ({ tool }) => (
  <div className="settings-item" data-testid={`code-tool-${tool.id}`}>
    <div className="settings-item-info">
      <div className="settings-item-name code-tool-name">
        {tool.name}
        {tool.version && <span className="code-tool-version">{tool.version}</span>}
        <span className="code-tool-state">
          <span className={`ws-lsp-dot ${tool.ok ? 'connected' : 'error'}`} aria-hidden="true" />
          {tool.ok ? 'Working' : 'Not available'}
        </span>
      </div>
      <div className="settings-item-desc">{tool.powers}</div>
      {tool.ok
        ? tool.note && (
            <span className="settings-hint" role="note">
              {tool.note}
            </span>
          )
        : (
            <span className="settings-hint settings-hint--warn" role="note">
              {tool.reason}
            </span>
          )}
      <div className="settings-item-desc">
        {tool.source}
        {tool.command && (
          <>
            {': '}
            <code className="code-tool-command">{tool.command}</code>
          </>
        )}
      </div>
    </div>
  </div>
);

/**
 * Settings > Tools and automation > Code tools. Every row is checked by the
 * backend against the exact command the feature runs (the app's own Python for
 * ruff and pylsp, the workspace's ESLint with the app's node), for the
 * connected workspace, so what this page says is what actually happens. The
 * workspace's ESLint is read from its package.json, never run: opening this
 * page executes nothing from the workspace.
 */
export const CodeToolsPanel: React.FC = () => {
  // Keyed on the workspace so connecting or switching one re-probes it.
  const wsPath = useUIStore((s) => s.wsPath);
  const statusQuery = useQuery({
    queryKey: ['code-tools-status', wsPath],
    queryFn: () =>
      get<CodeToolsStatusResponse>('/api/code-tools/status', {
        schema: CodeToolsStatusResponseSchema,
      }),
    staleTime: 30_000,
  });
  const data = statusQuery.data;

  // The one switch for every linter and formatter run on the assistant's code
  // (the code_checks flag, server/code_tools/turn_checks.py). Off by default.
  const queryClient = useQueryClient();
  const saveToast = useSaveToast();
  const flagsQuery = useQuery({
    queryKey: ['feature-flags'],
    queryFn: () => get<Record<string, { enabled: boolean }>>('/api/feature-flags'),
    staleTime: 5 * 60_000,
  });
  const checksOn = flagsQuery.data?.code_checks?.enabled ?? false;
  const toggleChecks = async () => {
    const next = !checksOn;
    await saveToast(async () => {
      await put('/api/feature-flags/code_checks', { enabled: next });
      await queryClient.invalidateQueries({ queryKey: ['feature-flags'] });
    }, { success: `Code checks ${next ? 'on' : 'off'}`, error: 'Failed to update code checks' });
  };

  return (
    <div className="settings-form" style={{ maxWidth: 640 }}>
      <p className="settings-hint">
        The linters and language servers Whisper Studio runs: the checks on code the assistant
        writes, and the code editor&apos;s completion, hover and inline diagnostics.
      </p>
      <div className="settings-list">
        <div className="settings-item" data-testid="code-checks-toggle">
          <div className="settings-item-info">
            <div className="settings-item-name">
              Check the assistant&apos;s code
              <HelpTip text="On: ruff, Prettier and ESLint fix and format each file after the assistant writes it, and a turn that changed code ends by asking whether to run ruff and ESLint on the changed files. Issues go back to the assistant to fix. Off: Whisper Studio runs no linter or formatter on the assistant's code." />
            </div>
          </div>
          <div className="settings-item-actions">
            <label className="toggle-switch" title={checksOn ? 'On' : 'Off'}>
              <input
                type="checkbox"
                checked={checksOn}
                disabled={!flagsQuery.data}
                onChange={() => void toggleChecks()}
                aria-label="Check the assistant's code"
              />
              <span className="toggle-slider"></span>
            </label>
          </div>
        </div>
      </div>
      {data && !data.workspace && (
        <p className="settings-hint" role="note">
          Connect a workspace to check its own tools: ESLint, and whether ruff also fixes and
          formats the files the assistant writes.
        </p>
      )}
      {data?.workspace && (
        <p className="settings-hint">
          For the workspace <code className="code-tool-command">{data.workspace}</code>.
        </p>
      )}
      {statusQuery.isError && (
        <p className="settings-empty">Could not load the code tools status.</p>
      )}
      {statusQuery.isPending && (
        <div className="settings-hint" aria-busy="true">
          <span className="skeleton skeleton-text" style={{ width: '50%' }} />
        </div>
      )}
      <div className="settings-list">
        {data?.tools.map((tool) => <CodeToolRow key={tool.id} tool={tool} />)}
      </div>
      <button
        className="btn btn-sm"
        type="button"
        onClick={() => void statusQuery.refetch()}
        disabled={statusQuery.isFetching}
        style={{ marginTop: 8 }}
      >
        {statusQuery.isFetching ? 'Checking…' : 'Check again'}
      </button>
    </div>
  );
};
