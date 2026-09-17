import React from 'react';
import { useSettingsStore } from '@/stores/settingsStore';
import { useUIStore } from '@/stores/uiStore';
import { useBackgroundTaskStore } from '@/stores/backgroundTaskStore';
import { useGitStatusBar } from '@/hooks/useGitStatusBar';
import { permissionModeLabel } from '@/utils/permissionModes';
import { useVoiceStore } from '@/stores/voiceStore';
import { useVoiceClock } from '@/hooks/useVoiceClock';

/**
 * Persistent bottom status strip for the workspace column — the glanceable
 * state a CLI status line shows: model, effort/mode, git branch + dirty +
 * sync, and a running background-task count that opens the tasks panel.
 *
 * Tokens, cost and the context meter deliberately live in ONE place, the
 * composer's TokenCounter, which is where the eye already is while typing;
 * this strip used to duplicate them from the same store fields.
 *
 * Every datum is read from an existing store (no new plumbing): the model
 * from settingsStore, git from the shared /api/git status + events, tasks
 * from the background-task store. Primitive selects only (zustand v5 safe).
 */
export const AppStatusBar: React.FC = () => {
  const selectedModel = useSettingsStore((s) => s.selectedModel);
  const models = useSettingsStore((s) => s.models);
  const effortLevel = useSettingsStore((s) => s.effortLevel);
  const permissionMode = useSettingsStore((s) => s.config.permissionMode);
  const wsConnected = useUIStore((s) => s.wsConnected);

  const runningTaskCount = useBackgroundTaskStore((s) => s.runningCount);
  const setTaskPanelOpen = useBackgroundTaskStore((s) => s.setPanelOpen);
  const taskPanelOpen = useBackgroundTaskStore((s) => s.panelOpen);

  const git = useGitStatusBar(wsConnected);
  const voiceStatus = useVoiceStore((s) => s.status);
  const voiceRegion = useVoiceStore((s) => s.region);
  const { renewIn: voiceRenewIn } = useVoiceClock();

  const modelLabel = models.find((m) => m.key === selectedModel)?.name ?? selectedModel;

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

      {voiceStatus !== 'off' && (
        <span className="asb-seg asb-voice" title="Voice conversation over Amazon Bedrock. The stream is renewed before Bedrock's 8-minute limit.">
          Voice · Nova 2 Sonic{voiceRegion ? ` · ${voiceRegion}` : ''}{voiceRenewIn ? ` · renews in ${voiceRenewIn}` : ''}
        </span>
      )}

      <span className="asb-spacer" />

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
