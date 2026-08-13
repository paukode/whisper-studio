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

export interface TurnModelSettings {
  model: string;
  effort_level: string;
  verbosity: string;
  brief_mode: boolean;
}

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
