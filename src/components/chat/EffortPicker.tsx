import React, { useMemo, useRef, useState } from 'react';
import { useSettingsStore } from '@/stores/settingsStore';
import { useDismiss } from '@/hooks/useDismiss';
import { effortLabel, thinkingLabel } from '@/utils/effort';
import { StardustEffortSlider } from '@/components/chat/StardustEffortSlider';
import { HelpTip } from '@/components/common/HelpTip';

/** Per-tier blurb behind the "?" tip next to the tier name: what the tier
 *  means for speed, cost, and quality. Accurate to
 *  server/infrastructure/effort.py: extra and ultracode both send xhigh, max is
 *  the deepest raw reasoning, ultracode adds parallel orchestration. */
const EFFORT_NOTES: Record<string, string> = {
  none: 'No thinking. Fastest and cheapest; quality drops on anything tricky.',
  low: 'Light reasoning. Fast and cheap; good for small, clear-cut tasks.',
  medium: 'Moderate reasoning. Balanced speed, cost, and quality for everyday work.',
  high: 'Thorough reasoning; the default. Slower and costlier, reliable on hard tasks.',
  extra: 'Deep reasoning (xhigh). Noticeably slower and pricier; strong on complex work.',
  max: 'Deepest reasoning, no token cap. Slowest and most expensive; peak single-model quality.',
  ultracode: 'Deep reasoning plus parallel subagents. Slowest overall; highest quality and cost, heavy token use.',
};

/** The same tip for an on-device model's thinking levels. Accurate to
 *  server/local/thinking.py: llama-server caps the thinking at these token
 *  counts, a model with its own levels (gpt-oss) gets the level itself. */
const THINKING_NOTES: Record<string, string> = {
  none: 'No thinking. The model answers at once; fastest.',
  low: 'Short thinking, up to about 1K tokens. Fast; good for easy questions.',
  medium: 'Moderate thinking, up to about 4K tokens. Balanced for everyday work.',
  high: 'Long thinking, up to about 16K tokens. Slower; for hard problems.',
  max: 'Thinks as long as the model needs, no cap; the default. Slowest.',
};

interface EffortPickerProps {
  /** Called when the slider opens so the parent can close its other toolbar
   *  dropdowns (single-open behaviour). */
  onOpen?: () => void;
}

/**
 * Toolbar effort control: a label-only chip (matching the mode and verbosity
 * pills) in the tier's colour that opens a slider on click. The slider, the
 * `/effort` command, the command palette, and model-switch clamping all
 * read/write the same store value (`effortLevel`), so every surface stays in
 * sync. Models with no effort tier (Haiku) render a static "no effort" pill
 * and no slider. On an on-device model the same chip is the Thinking control:
 * it reads and writes `localThinkingLevel`, the model's own levels (Off to
 * Max), so a cloud model's effort is never changed by a local choice.
 */
export const EffortPicker: React.FC<EffortPickerProps> = ({ onOpen }) => {
  const cloudLevel = useSettingsStore((s) => s.effortLevel);
  const setCloudLevel = useSettingsStore((s) => s.setEffortLevel);
  const localLevel = useSettingsStore((s) => s.localThinkingLevel);
  const setLocalLevel = useSettingsStore((s) => s.setLocalThinkingLevel);
  const models = useSettingsStore((s) => s.models);
  const selectedModel = useSettingsStore((s) => s.selectedModel);
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  const model = useMemo(() => models.find((m) => m.key === selectedModel), [models, selectedModel]);
  const levels = model?.effort_levels ?? [];
  const local = !!model?.is_local;
  const effortLevel = local ? localLevel : cloudLevel;
  const setEffortLevel = local ? setLocalLevel : setCloudLevel;
  const labelOf = local ? thinkingLabel : effortLabel;
  const kind = local ? 'Thinking' : 'Effort';

  // Close on Escape or a click anywhere outside the wrap (incl. other toolbar
  // buttons, which live outside it).
  useDismiss(ref, () => setOpen(false), { enabled: open });

  if (levels.length === 0) {
    return (
      <span className="effort-badge effort-none" id="effortBadge" title="This model has no effort level">
        no effort
      </span>
    );
  }

  const note = (local ? THINKING_NOTES : EFFORT_NOTES)[effortLevel] ?? 'Adaptive reasoning depth.';

  return (
    <div className="toolbar-dropdown-wrap" ref={ref}>
      <button
        type="button"
        className={`effort-chip effort-${effortLevel}`}
        id="effortBadge"
        title={`${kind}: ${labelOf(effortLevel)}, click to adjust`}
        aria-label={`${kind} level: ${labelOf(effortLevel)}. Click to adjust.`}
        aria-expanded={open}
        onClick={() => {
          setOpen((p) => {
            if (!p) onOpen?.();
            return !p;
          });
        }}
      >
        <span className="effort-chip-name">{labelOf(effortLevel)}</span>
      </button>
      <div className="toolbar-dropdown effort-pop" style={{ display: open ? 'block' : 'none' }}>
        <div className="effort-pop-row">
          <span className="effort-pop-label">{kind}</span>
          <span className={`effort-val effort-${effortLevel}`} data-testid="effort-current">
            {labelOf(effortLevel)}
            <HelpTip text={note} />
          </span>
        </div>
        {open && (
          <StardustEffortSlider
            levels={levels}
            value={effortLevel}
            onChange={setEffortLevel}
            labelFor={labelOf}
            ariaLabel={`${kind} level`}
          />
        )}
      </div>
    </div>
  );
};
