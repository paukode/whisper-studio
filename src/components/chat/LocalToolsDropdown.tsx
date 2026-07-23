import React, { useRef, useState } from 'react';
import { useSettingsStore, type LocalToolScope } from '@/stores/settingsStore';
import { useDismiss } from '@/hooks/useDismiss';

interface LocalToolsDropdownProps {
  /** Called when the dropdown opens so the parent can close its other toolbar
   *  dropdowns (single-open behaviour), matching the EffortPicker. */
  onOpen?: () => void;
}

// Each scope: the value, a short button label, and a one-line description. The
// trade-off is prompt size (fewer tools = smaller prompt = faster on-device).
const OPTIONS: { value: LocalToolScope; label: string; desc: string }[] = [
  { value: 'off', label: 'Off', desc: 'No tools — pure chat (smallest, fastest)' },
  { value: 'core', label: 'Core set', desc: 'Read/search/edit files, run, git, memory' },
  { value: 'core_web', label: 'Core + web', desc: 'Core set plus web search and fetch' },
  { value: 'all', label: 'All tools (64)', desc: 'Full tool pool — heaviest prompt, slowest' },
];

const SHORT: Record<LocalToolScope, string> = {
  off: 'Off',
  core: 'Core',
  core_web: 'Core+Web',
  all: 'All',
};

/**
 * Toolbar control (on-device models that support tools only) to choose which
 * tools the local model may use. Replaces the old on/off toggle: more tools =
 * more capability but a much bigger prompt (the ~64-tool pool alone is ~8K
 * tokens), so smaller scopes keep on-device turns fast. The choice is persisted.
 */
export const LocalToolsDropdown: React.FC<LocalToolsDropdownProps> = ({ onOpen }) => {
  const model = useSettingsStore((s) => s.models.find((m) => m.key === s.selectedModel));
  const scope = useSettingsStore((s) => s.localToolScope);
  const setScope = useSettingsStore((s) => s.setLocalToolScope);
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useDismiss(ref, () => setOpen(false), { enabled: open });

  if (!model?.is_local || !model?.supports_tools) return null;

  const active = scope !== 'off';

  return (
    <div className="toolbar-dropdown-wrap" ref={ref}>
      <button
        type="button"
        className={`toolbar-btn local-tools-toggle${active ? ' active' : ''}`}
        title="Choose which tools the local model may use (more tools = bigger, slower prompt)"
        onClick={() => {
          setOpen((p) => {
            if (!p) onOpen?.();
            return !p;
          });
        }}
      >
        <span aria-hidden="true">🔧</span>
        Tools: {SHORT[scope]}
      </button>
      <div className="toolbar-dropdown" style={{ display: open ? 'block' : 'none' }}>
        <div className="toolbar-dropdown-header">Local model tools</div>
        {OPTIONS.map((o) => (
          <div
            key={o.value}
            className="toolbar-dropdown-item"
            onClick={() => { setScope(o.value); setOpen(false); }}
            style={{ flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between' }}
          >
            <div style={{ display: 'flex', flexDirection: 'column', gap: '1px', flex: 1, minWidth: 0 }}>
              <span className="toolbar-dropdown-item-name">{o.label}</span>
              <span className="toolbar-dropdown-item-desc">{o.desc}</span>
            </div>
            {scope === o.value && (
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0, marginLeft: '8px' }}>
                <polyline points="20 6 9 17 4 12"/>
              </svg>
            )}
          </div>
        ))}
      </div>
    </div>
  );
};
