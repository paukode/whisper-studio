import React from 'react';

interface ModelOption {
  key: string;
  name: string;
  requires_data_retention?: boolean;
  is_local?: boolean;
}

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
 * Model picker for the chat toolbar. Extracted from ChatInput.tsx to keep that
 * file under the size budget. Purely presentational — selection routing (the
 * Mythos-class data-retention consent gate, etc.) stays in the parent via
 * `onSelect`, and closing sibling dropdowns stays in `onToggle`. On this branch
 * on-device models carry a "Local" badge.
 */
export const ModelDropdown: React.FC<ModelDropdownProps> = ({
  models, selectedModel, loadedLocalModel, open, onToggle, onSelect,
}) => {
  const sel = models.find((m) => m.key === selectedModel);
  // An on-device model that is selected but not resident yet — the user loads it
  // (by re-selecting or by sending the first message) to start a session.
  const selectedNotLoaded = !!sel?.is_local && loadedLocalModel !== selectedModel;
  return (
  <div className="toolbar-dropdown-wrap">
    <button type="button" className="toolbar-btn" id="modelBtn" title={selectedNotLoaded ? 'Model not loaded — select it (or send a message) to load it' : 'Select AI model'} onClick={onToggle}>
      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M12 2l2.4 5.3L20 8.5l-4 4.1.9 5.9L12 15.8l-4.9 2.7.9-5.9-4-4.1 5.6-1.2z"/>
      </svg>
      {sel?.name ?? selectedModel}
      {selectedNotLoaded && (
        <span style={{ opacity: 0.6, marginLeft: 4, fontSize: '0.85em' }}>· not loaded</span>
      )}
    </button>
    <div className="toolbar-dropdown" id="modelDropdown" style={{ display: open ? 'block' : 'none' }}>
      <div className="toolbar-dropdown-header">Model</div>
      {models.map((m) => (
        <div
          key={m.key}
          className="toolbar-dropdown-item"
          data-testid={`model-option-${m.key}`}
          onClick={() => onSelect(m.key)}
          style={{ flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between' }}
        >
          <div style={{ display: 'flex', flexDirection: 'column', gap: '1px', flex: 1, minWidth: 0 }}>
            <span className="toolbar-dropdown-item-name">
              {m.name}
              {m.requires_data_retention && <span className="model-retention-badge">Mythos</span>}
              {m.is_local && <span className="model-local-badge">Local</span>}
              {m.is_local && loadedLocalModel === m.key && (
                <span title="Loaded in memory" style={{ display: 'inline-block', width: 6, height: 6, borderRadius: '50%', background: 'var(--accent)', marginLeft: 6, verticalAlign: 'middle' }} />
              )}
            </span>
            {m.requires_data_retention && (
              <span className="toolbar-dropdown-item-desc">Requires data-retention opt-in</span>
            )}
          </div>
          {selectedModel === m.key && (
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
