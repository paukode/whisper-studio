/**
 * The per-turn model settings every /api/chat body carries, shared by the
 * fresh-turn path and the approval-resume path.
 *
 * Model, effort level and response length live in the settings store only —
 * the backend persists none of them per turn — so each request has to send
 * them. A request that omits them falls back to the CONFIG defaults, which for
 * an approval continuation means the second half of one turn silently ran on
 * different settings than the first: an Ultracode turn finished at normal
 * effort, a Brief/Detailed choice reverted to the model default, and a paused
 * local turn resumed on the default cloud model.
 */
import { useSettingsStore } from '@/stores/settingsStore';
import type { ChatMessage, TurnModelSettings } from '@/types/chat';

export function turnModelSettings(): TurnModelSettings {
  const s = useSettingsStore.getState();
  // Response length (Brief/Normal/Detailed) is stored as verbosity and applied
  // per model: GPT-5.x uses text.verbosity natively, so it gets the value as-is
  // and no brief instruction; models without native verbosity get a
  // concise-instruction (brief_mode) only at the Brief end, with verbosity
  // ignored server-side.
  const supportsVerbosity = !!s.models?.find((m) => m.key === s.selectedModel)?.supports_verbosity;
  return {
    model: s.selectedModel,
    effort_level: s.effortLevel,
    verbosity: s.verbosity,
    brief_mode: supportsVerbosity ? false : s.verbosity === 'low',
  };
}

/** The settings a question or folder-prompt answer resumes its turn on: those
 *  recorded on the message whose card is being answered (readSSEStream stamps
 *  them there when the turn pauses). Null when none of `toolUseIds` belongs
 *  to such a message, which is only the case for a row saved before turns
 *  recorded their settings; the picker is then the only record there is. */
export function pausedTurnSettings(
  messages: ChatMessage[],
  toolUseIds: string[],
): TurnModelSettings | null {
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i];
    if (!m.turnSettings) continue;
    const cardIds = [
      ...(m.userQuestions ?? []).map((q) => q.toolUseId),
      ...(m.toolUse ?? []).filter((t) => t.toolName === 'ws_workspace_prompt').map((t) => t.toolId),
    ];
    if (cardIds.some((id) => toolUseIds.includes(id))) return m.turnSettings;
  }
  return null;
}
