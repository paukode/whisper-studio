import React, { useLayoutEffect, useRef, useState } from 'react';
import { noChatModelHint, useSettingsStore } from '@/stores/settingsStore';

interface ModelOption {
  key: string;
  name: string;
  requires_data_retention?: boolean;
  is_local?: boolean;
  /** Who runs it (from /api/models); groups the menu. */
  provider?: 'anthropic' | 'openai' | 'local';
  /** On-device models under the agentic size threshold install with tool
   *  calling off (server/model_browser/compat.py), so the row says so at the
   *  point of choosing rather than leaving it to a turn that never uses a tool. */
  supports_tools?: boolean;
}

/** Menu groups, in display order. */
const GROUPS: { id: 'anthropic' | 'openai' | 'local'; label: string }[] = [
  { id: 'anthropic', label: 'Claude' },
  { id: 'openai', label: 'OpenAI' },
  { id: 'local', label: 'On this Mac' },
];

const groupOf = (m: ModelOption): 'anthropic' | 'openai' | 'local' =>
  m.provider ?? (m.is_local ? 'local' : 'anthropic');

interface ModelDropdownProps {
  models: ModelOption[];
  selectedModel: string;
  /** Which on-device model is resident in memory (null = none). On-device
   *  models load lazily, so the selected one can be picked but not yet loaded;
   *  the trigger flags that and each loaded local model gets a dot. */
  loadedLocalModel?: string | null;
  open: boolean;
  onToggle: () => void;
  onSelect: (key: string) => void;
}

/**
 * Model picker in the composer's "How it thinks" panel: a full-width button
 * with an arrow that opens a menu over the panel, grouped by who runs the
 * model (Claude, OpenAI, on this Mac). Purely presentational: selection
 * routing (the Mythos-class data-retention consent gate, etc.) stays in the
 * parent via `onSelect`, and closing sibling menus stays in `onToggle`.
 * On-device models carry a "Local" badge. With nothing to offer (Local mode
 * before an on-device model is installed) the menu shows where to get one
 * instead of rows: a hint, not a second control.
 */
/** The config labels name on-device models "... (Local)". The dropdown rows
 *  already carry the Local badge, so strip the suffix there to avoid saying it
 *  twice; the trigger chip has no badge and keeps the full label. */
const rowName = (m: ModelOption): string =>
  m.is_local ? m.name.replace(/\s*\(local\)\s*$/i, '') : m.name;

export const ModelDropdown: React.FC<ModelDropdownProps> = ({
  models, selectedModel, loadedLocalModel, open, onToggle, onSelect,
}) => {
  const sel = models.find((m) => m.key === selectedModel);
  const needsLocalModel = useSettingsStore((s) => s.needsLocalModel);
  // An on-device model that is selected but not resident yet — the user loads it
  // (by re-selecting or by sending the first message) to start a session.
  const selectedNotLoaded = !!sel?.is_local && loadedLocalModel !== selectedModel;
  // Open toward the side of the window with more room, as tall as that room
  // allows: the picker sits in a panel near the bottom of the window.
  const btnRef = useRef<HTMLButtonElement>(null);
  const [place, setPlace] = useState<{ up: boolean; max: number }>({ up: false, max: 480 });
  useLayoutEffect(() => {
    if (!open || !btnRef.current) return;
    const r = btnRef.current.getBoundingClientRect();
    const below = window.innerHeight - r.bottom - 16;
    const above = r.top - 16;
    const up = above > below;
    setPlace({ up, max: Math.max(160, up ? above : below) });
  }, [open]);
  const row = (m: ModelOption) => (
    <div
      key={m.key}
      className={`model-menu-item${selectedModel === m.key ? ' on' : ''}`}
      data-testid={`model-option-${m.key}`}
      role="option"
      aria-selected={selectedModel === m.key}
      onClick={() => onSelect(m.key)}
    >
      <div className="model-menu-text">
        <span className="model-menu-name">
          {rowName(m)}
          {m.requires_data_retention && <span className="model-retention-badge">Mythos</span>}
          {m.is_local && <span className="model-local-badge">Local</span>}
          {m.is_local && loadedLocalModel === m.key && (
            <span className="model-loaded-dot" title="Loaded in memory" />
          )}
        </span>
        {m.requires_data_retention && (
          <span className="model-menu-desc">Requires data-retention opt-in</span>
        )}
        {m.is_local && m.supports_tools === false && (
          <span className="model-menu-desc">Chat only, no tools or workspace access</span>
        )}
      </div>
      {selectedModel === m.key && (
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <polyline points="20 6 9 17 4 12"/>
        </svg>
      )}
    </div>
  );

  return (
    <div className="model-picker toolbar-dropdown-wrap">
      <button
        ref={btnRef}
        type="button"
        className={`model-picker-btn${open ? ' open' : ''}`}
        id="modelBtn"
        title={selectedNotLoaded ? 'Model not loaded. Select it (or send a message) to load it' : 'Choose the model'}
        aria-haspopup="listbox"
        aria-expanded={open}
        onClick={onToggle}
      >
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M12 2l2.4 5.3L20 8.5l-4 4.1.9 5.9L12 15.8l-4.9 2.7.9-5.9-4-4.1 5.6-1.2z"/>
        </svg>
        <span className="model-picker-name">{sel?.name ?? (selectedModel || 'No model')}</span>
        {selectedNotLoaded && <span className="model-picker-note">· not loaded</span>}
        <svg className="model-picker-chev" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <polyline points="6 9 12 15 18 9"/>
        </svg>
      </button>
      {open && (
        <div
          className={`model-menu${place.up ? ' up' : ''}`}
          id="modelDropdown"
          role="listbox"
          aria-label="Model"
          style={{ maxHeight: place.max }}
        >
          {models.length === 0 && (
            <div className="model-menu-desc" data-testid="model-empty-hint" style={{ padding: '8px 10px', maxWidth: 260 }}>
              {noChatModelHint(needsLocalModel)}
            </div>
          )}
          {GROUPS.map((g) => {
            const rows = models.filter((m) => groupOf(m) === g.id);
            if (rows.length === 0) return null;
            return (
              <div key={g.id} className="model-menu-group" role="group" aria-label={g.label}>
                <div className="model-menu-header">{g.label}</div>
                {rows.map(row)}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
};
