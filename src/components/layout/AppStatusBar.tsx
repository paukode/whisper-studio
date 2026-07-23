import React from 'react';
import { useSettingsStore } from '@/stores/settingsStore';
import { useUIStore } from '@/stores/uiStore';
import { useActiveChatStore } from '@/stores/sessionRuntimes';
import { useBackgroundTaskStore } from '@/stores/backgroundTaskStore';
import { useGitStatusBar } from '@/hooks/useGitStatusBar';
import { permissionModeLabel } from '@/utils/permissionModes';

/**
 * Persistent bottom status strip for the workspace column — the glanceable
 * state a CLI status line shows: model, effort/mode, git branch + dirty +
 * sync, live context-window meter, per-turn tokens/cost, and a running
 * background-task count that opens the tasks panel.
 *
 * Every datum is read from an existing store (no new plumbing): the model
 * from settingsStore, git from the shared /api/git status + events, context
 * and tokens from the live chat store's usage frames, tasks from the
 * background-task store. Primitive selects only (zustand v5 safe).
 */
export const AppStatusBar: React.FC = () => {
  const selectedModel = useSettingsStore((s) => s.selectedModel);
  const models = useSettingsStore((s) => s.models);
  const effortLevel = useSettingsStore((s) => s.effortLevel);
  const permissionMode = useSettingsStore((s) => s.config.permissionMode);
  const wsConnected = useUIStore((s) => s.wsConnected);

  const inputTokens = useActiveChatStore((s) => s.inputTokens);
  const outputTokens = useActiveChatStore((s) => s.outputTokens);
  const estimatedCost = useActiveChatStore((s) => s.estimatedCost);
  const contextUsed = useActiveChatStore((s) => s.contextUsed);
  const contextMax = useActiveChatStore((s) => s.contextMax);

  const runningTaskCount = useBackgroundTaskStore((s) => s.runningCount);
  const setTaskPanelOpen = useBackgroundTaskStore((s) => s.setPanelOpen);
  const taskPanelOpen = useBackgroundTaskStore((s) => s.panelOpen);

  const git = useGitStatusBar(wsConnected);

  const modelLabel = models.find((m) => m.key === selectedModel)?.name ?? selectedModel;
  const hasTokens = inputTokens > 0 || outputTokens > 0;
  const contextPct =
    contextMax > 0 ? Math.min(100, Math.round((contextUsed / contextMax) * 100)) : 0;

  return (
    <div className="app-status-bar" id="appStatusBar" role="status">
      <span className="asb-seg asb-model" title="Active model">
        {modelLabel}
      </span>

      {effortLevel && effortLevel !== 'none' && (
        <span className="asb-seg asb-effort" title="Reasoning effort">
          {effortLevel}
        </span>
      )}

      {permissionMode && permissionMode !== 'default' && (
        <span className="asb-seg asb-mode" title="Permission mode">
          {permissionModeLabel(permissionMode)}
        </span>
      )}

      {git?.branch && (
        <span
          className={`asb-seg asb-git${git.clean ? '' : ' dirty'}`}
          title={
            git.clean
              ? 'Working tree clean'
              : `${git.changed} changed, ${git.untracked} untracked`
          }
        >
          <span className="asb-git-icon">⎇</span>
          {git.branch}
          {!git.clean && <span className="asb-git-count">±{git.changed + git.untracked}</span>}
          {git.ahead > 0 && <span className="asb-ahead">↑{git.ahead}</span>}
          {git.behind > 0 && <span className="asb-behind">↓{git.behind}</span>}
        </span>
      )}

      <span className="asb-spacer" />

      {contextMax > 0 && (
        <span className="asb-seg asb-context" title="Context window used this turn">
          <span className="asb-ctx-track">
            <span
              className={`asb-ctx-fill${contextPct >= 80 ? ' hot' : ''}`}
              style={{ width: `${contextPct}%` }}
            />
          </span>
          {contextPct}%
        </span>
      )}

      {hasTokens && (
        <span className="asb-seg asb-tokens" title="Tokens this turn / estimated cost">
          {(inputTokens + outputTokens).toLocaleString()} tok · ${estimatedCost.toFixed(4)}
        </span>
      )}

      {runningTaskCount > 0 && (
        <button
          className="asb-seg asb-tasks"
          type="button"
          title="Background tasks running — click to view"
          onClick={() => setTaskPanelOpen(!taskPanelOpen)}
          aria-expanded={taskPanelOpen}
        >
          <span className="asb-pulse" />
          {runningTaskCount} task{runningTaskCount === 1 ? '' : 's'}
        </button>
      )}
    </div>
  );
};
