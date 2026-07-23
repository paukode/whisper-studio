import React, { useRef, useState } from 'react';
import { useSettingsStore } from '@/stores/settingsStore';
import { useDismiss } from '@/hooks/useDismiss';
import { HelpTip } from '@/components/common/HelpTip';

interface ResponseLengthPickerProps {
  /** Called when the popover opens so the parent can close its other toolbar
   *  dropdowns (single-open behaviour), mirroring EffortPicker. */
  onOpen?: () => void;
}

// One control, three levels. The stored value stays GPT-5.x's native
// `verbosity` (low/medium/high) so the OpenAI path needs no mapping; the label
// is model-agnostic. How it's applied per model is resolved at send time
// (useChatStream): GPT-5.x uses text.verbosity directly; models without native
// verbosity get a "be concise" instruction (brief_mode) only at the Brief end.
const LEVELS = ['low', 'medium', 'high'] as const;
const LABEL: Record<string, string> = { low: 'Brief', medium: 'Normal', high: 'Detailed' };
const NOTE: Record<string, string> = {
  low: 'Short, direct answers.',
  medium: 'Balanced detail.',
  high: 'Thorough, expansive answers.',
};

/**
 * Unified "Response length" control for cloud models. The pill shows only the
 * current level name; clicking it opens a plain three-option list where each
 * row carries a "?" tip with the level's description.
 */
export const ResponseLengthPicker: React.FC<ResponseLengthPickerProps> = ({ onOpen }) => {
  const verbosity = useSettingsStore((s) => s.verbosity);
  const setVerbosity = useSettingsStore((s) => s.setVerbosity);
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useDismiss(ref, () => setOpen(false), { enabled: open });

  const label = LABEL[verbosity] ?? 'Normal';

  return (
    <div className="toolbar-dropdown-wrap" ref={ref}>
      <button
        type="button"
        className="effort-chip verbosity-chip"
        title={`Response length: ${label}, click to adjust`}
        aria-label={`Response length: ${label}. Click to adjust.`}
        aria-expanded={open}
        onClick={() => {
          setOpen((p) => {
            if (!p) onOpen?.();
            return !p;
          });
        }}
      >
        <span className="effort-chip-name">{label}</span>
      </button>
      <div className="toolbar-dropdown verbosity-pop" style={{ display: open ? 'block' : 'none' }}>
        {LEVELS.map((lv) => (
          // Whole-row click target, mirroring ModeDropdown: the row selects,
          // the inner button is the keyboard/AT target and stops propagation.
          <div
            key={lv}
            className="toolbar-dropdown-item opt-row"
            onClick={() => {
              setVerbosity(lv);
              setOpen(false);
            }}
          >
            <button
              type="button"
              className="opt-row-main"
              data-testid={`verbosity-option-${lv}`}
              aria-pressed={lv === verbosity}
              onClick={(e) => {
                e.stopPropagation();
                setVerbosity(lv);
                setOpen(false);
              }}
            >
              <span className="toolbar-dropdown-item-name">{LABEL[lv]}</span>
              {lv === verbosity && (
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                  <polyline points="20 6 9 17 4 12" />
                </svg>
              )}
            </button>
            <span className="opt-side">
              <HelpTip text={NOTE[lv]} />
            </span>
          </div>
        ))}
      </div>
    </div>
  );
};
