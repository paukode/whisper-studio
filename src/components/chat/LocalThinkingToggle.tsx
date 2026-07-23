import React from 'react';
import { useSettingsStore } from '@/stores/settingsStore';

/**
 * Toolbar toggle for a local model's reasoning (thinking) mode. Only shown when
 * the selected model is on-device AND advertises a thinking mode (e.g. Gemma).
 * When on, the backend renders the model's chat template with enable_thinking
 * and streams the reasoning into the chat's "Thought process" block.
 */
export const LocalThinkingToggle: React.FC = () => {
  const model = useSettingsStore((s) => s.models.find((m) => m.key === s.selectedModel));
  const localThinking = useSettingsStore((s) => s.localThinking);
  const setLocalThinking = useSettingsStore((s) => s.setLocalThinking);

  if (!model?.is_local || !model?.supports_thinking) return null;

  return (
    <button
      type="button"
      className={`toolbar-btn local-think-toggle${localThinking ? ' active' : ''}`}
      title="Toggle the local model's thinking (reasoning) mode"
      aria-pressed={localThinking}
      onClick={() => setLocalThinking(!localThinking)}
    >
      <span aria-hidden="true">✨</span>
      Think {localThinking ? 'on' : 'off'}
    </button>
  );
};
