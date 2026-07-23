import React from 'react';
import { HelpTip } from '@/components/common/HelpTip';
import { PERMISSION_MODES } from '@/utils/permissionModes';

interface ModeDropdownProps {
  /** Current permission mode (drives the badge + the checkmark). */
  permissionMode: string;
  /** Whether the dropdown is open. */
  open: boolean;
  /** Toggle the dropdown (the parent also closes its other dropdowns here). */
  onToggle: () => void;
  /** Pick a permission mode. */
  onSelect: (mode: string) => void;
  /** Open Settings → Permissions ("Manage rules…"). */
  onManage: () => void;
}

const RiskIcon: React.FC<{ risk: 'warn' | 'danger' }> = ({ risk }) => (
  <svg
    className={`mode-risk-icon mode-risk-${risk}`}
    width="13" height="13" viewBox="0 0 24 24" fill="none"
    stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"
    aria-hidden="true"
  >
    <path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
    <line x1="12" y1="9" x2="12" y2="13" /><line x1="12" y1="17" x2="12.01" y2="17" />
  </svg>
);

/**
 * Toolbar permission-mode dropdown. Stateless: the parent owns the open flag
 * and the select/manage handlers (so it can close sibling dropdowns and
 * persist). The pill shows only the friendly mode label; risky modes tint it
 * (amber = writes auto-approved, red = no prompts at all). Descriptions live
 * behind the per-row "?" tips instead of cluttering the list.
 */
export const ModeDropdown: React.FC<ModeDropdownProps> = ({
  permissionMode, open, onToggle, onSelect, onManage,
}) => {
  const current = PERMISSION_MODES.find((m) => m.value === permissionMode);
  return (
    <div className="toolbar-dropdown-wrap">
      <button
        type="button"
        className={`toolbar-btn mode-btn${current?.risk ? ` mode-${current.risk}` : ''}`}
        id="modeBtn"
        title={`Permission mode: ${current?.label ?? permissionMode}`}
        aria-label={`Permission mode: ${current?.label ?? permissionMode}`}
        aria-expanded={open}
        aria-haspopup="true"
        onClick={onToggle}
      >
        {current?.label ?? permissionMode}
      </button>
      <div className="toolbar-dropdown mode-pop" id="modeDropdown" style={{ display: open ? 'block' : 'none' }}>
        <div className="toolbar-dropdown-header">Permission mode</div>
        {PERMISSION_MODES.map((m) => (
          // The whole row selects (the "?" tip swallows its own clicks); the
          // inner button is the keyboard/AT target and stops propagation so a
          // button click cannot double-fire through the row handler.
          <div key={m.value} className="toolbar-dropdown-item opt-row" onClick={() => void onSelect(m.value)}>
            <button
              type="button"
              className="opt-row-main"
              data-testid={`mode-option-${m.value}`}
              aria-pressed={permissionMode === m.value}
              onClick={(e) => {
                e.stopPropagation();
                void onSelect(m.value);
              }}
            >
              <span className={`toolbar-dropdown-item-name${m.risk ? ` mode-name-${m.risk}` : ''}`}>
                {m.label}
              </span>
              {permissionMode === m.value && (
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                  <polyline points="20 6 9 17 4 12" />
                </svg>
              )}
            </button>
            <span className="opt-side">
              {m.risk && <RiskIcon risk={m.risk} />}
              <HelpTip text={m.help} />
            </span>
          </div>
        ))}
        <div className="toolbar-dropdown-sep" />
        <button type="button" className="toolbar-dropdown-item toolbar-dropdown-manage mode-manage" data-testid="mode-option-manage" onClick={onManage}>
          <span className="toolbar-dropdown-item-name">Manage rules…</span>
        </button>
      </div>
    </div>
  );
};
