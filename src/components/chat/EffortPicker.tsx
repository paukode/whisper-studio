import React, { useMemo, useRef, useState } from 'react';
import { useSettingsStore } from '@/stores/settingsStore';
import { useDismiss } from '@/hooks/useDismiss';
import { effortLabel } from '@/utils/effort';
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
 * and no slider.
 */
export const EffortPicker: React.FC<EffortPickerProps> = ({ onOpen }) => {
  const effortLevel = useSettingsStore((s) => s.effortLevel);
  const setEffortLevel = useSettingsStore((s) => s.setEffortLevel);
  const models = useSettingsStore((s) => s.models);
  const selectedModel = useSettingsStore((s) => s.selectedModel);
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  const levels = useMemo(
    () => models.find((m) => m.key === selectedModel)?.effort_levels ?? [],
    [models, selectedModel],
  );

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

  const note = EFFORT_NOTES[effortLevel] ?? 'Adaptive reasoning depth.';

  return (
    <div className="toolbar-dropdown-wrap" ref={ref}>
      <button
        type="button"
        className={`effort-chip effort-${effortLevel}`}
        id="effortBadge"
        title={`Effort: ${effortLabel(effortLevel)}, click to adjust`}
        aria-label={`Effort level: ${effortLabel(effortLevel)}. Click to adjust.`}
        aria-expanded={open}
        onClick={() => {
          setOpen((p) => {
            if (!p) onOpen?.();
            return !p;
          });
        }}
      >
        <span className="effort-chip-name">{effortLabel(effortLevel)}</span>
      </button>
      <div className="toolbar-dropdown effort-pop" style={{ display: open ? 'block' : 'none' }}>
        <div className="effort-pop-row">
          <span className="effort-pop-label">Effort</span>
          <span className={`effort-val effort-${effortLevel}`} data-testid="effort-current">
            {effortLabel(effortLevel)}
            <HelpTip text={note} />
          </span>
        </div>
        {open && (
          <StardustEffortSlider levels={levels} value={effortLevel} onChange={setEffortLevel} />
        )}
      </div>
    </div>
  );
};
