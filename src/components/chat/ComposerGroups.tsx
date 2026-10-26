import React, { useMemo, useRef } from 'react';
import { useSettingsStore } from '@/stores/settingsStore';
import { useDismiss } from '@/hooks/useDismiss';
import { effortLabel, thinkingLabel } from '@/utils/effort';
import { EFFORT_NOTES, THINKING_NOTES } from '@/utils/effortNotes';
import { PERMISSION_MODES } from '@/utils/permissionModes';
import { ModelDropdown } from './ModelDropdown';
import { LocalContextWindowSlider } from './LocalContextWindowSlider';

/**
 * The composer's two control groups. Collapsed, each is one capsule of short
 * segments ("How it thinks": model, thinking, answers; "Where it works":
 * workspace, permissions). A click opens the group's labeled panel above the
 * composer, where every setting is named and explained. The parent owns which
 * panel is open, so opening one closes the other and the More menu.
 */

// Stored as GPT-5.x's native `verbosity` (low/medium/high); how each model
// applies it is resolved at send time (useChatStream).
const VERBOSITY_LEVELS = ['low', 'medium', 'high'] as const;
const VERBOSITY_LABEL: Record<string, string> = { low: 'Brief', medium: 'Normal', high: 'Detailed' };

const Icon = {
  model: (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M12 2l2.4 5.3L20 8.5l-4 4.1.9 5.9L12 15.8l-4.9 2.7.9-5.9-4-4.1 5.6-1.2z" />
    </svg>
  ),
  think: (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M9 3a3 3 0 0 0-3 3 3 3 0 0 0-2 5 3 3 0 0 0 2 5 3 3 0 0 0 6 1V5a2 2 0 0 0-3-2z" />
      <path d="M15 3a3 3 0 0 1 3 3 3 3 0 0 1 2 5 3 3 0 0 1-2 5 3 3 0 0 1-6 1" />
    </svg>
  ),
  answers: (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true">
      <line x1="4" y1="6" x2="20" y2="6" /><line x1="4" y1="12" x2="16" y2="12" /><line x1="4" y1="18" x2="12" y2="18" />
    </svg>
  ),
  folder: (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" />
    </svg>
  ),
  shield: (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
    </svg>
  ),
};

/** The tint of a permission mode: risky modes read amber or red at a glance. */
const modeTone = (mode: string): string => {
  const risk = PERMISSION_MODES.find((m) => m.value === mode)?.risk;
  return risk ?? (mode === 'plan' || mode === 'dontAsk' ? 'calm' : 'safe');
};

interface ThinkGroupProps {
  open: boolean;
  onToggle: () => void;
  onClose: () => void;
  modelOpen: boolean;
  onToggleModel: () => void;
  onSelectModel: (key: string) => void;
}

export const ThinkGroup: React.FC<ThinkGroupProps> = ({
  open, onToggle, onClose, modelOpen, onToggleModel, onSelectModel,
}) => {
  const models = useSettingsStore((s) => s.models);
  const selectedModel = useSettingsStore((s) => s.selectedModel);
  const loadedLocalModel = useSettingsStore((s) => s.loadedLocalModel);
  const cloudLevel = useSettingsStore((s) => s.effortLevel);
  const setCloudLevel = useSettingsStore((s) => s.setEffortLevel);
  const localLevel = useSettingsStore((s) => s.localThinkingLevel);
  const setLocalLevel = useSettingsStore((s) => s.setLocalThinkingLevel);
  const verbosity = useSettingsStore((s) => s.verbosity);
  const setVerbosity = useSettingsStore((s) => s.setVerbosity);
  const ref = useRef<HTMLDivElement>(null);
  useDismiss(ref, onClose, { enabled: open });

  const model = useMemo(() => models.find((m) => m.key === selectedModel), [models, selectedModel]);
  const local = !!model?.is_local;
  const levels = model?.effort_levels ?? [];
  const level = local ? localLevel : cloudLevel;
  const setLevel = local ? setLocalLevel : setCloudLevel;
  const labelOf = local ? thinkingLabel : effortLabel;
  const note = (local ? THINKING_NOTES : EFFORT_NOTES)[level] ?? 'Adaptive reasoning depth.';
  const modelName = model?.name ?? (selectedModel || 'No model');
  const answers = VERBOSITY_LABEL[verbosity] ?? 'Normal';

  return (
    <div className="cg-wrap" ref={ref}>
      <button
        type="button"
        className={`cg-group${open ? ' open' : ''}`}
        id="thinkGroup"
        aria-expanded={open}
        aria-label={`How it thinks: ${modelName}${levels.length ? `, thinking ${labelOf(level)}` : ''}${local ? '' : `, ${answers} answers`}`}
        onClick={onToggle}
      >
        <span className="cg-seg cg-model" title={`Model: ${modelName}`}>{Icon.model}<span className="cg-t">{modelName}</span></span>
        {levels.length > 0 && (
          <>
            <span className="cg-dv" aria-hidden="true" />
            <span className={`cg-seg cg-think effort-${level}`} title={`Thinking: ${labelOf(level)}`}>{Icon.think}<span className="cg-t">{labelOf(level)}</span></span>
          </>
        )}
        {!local && (
          <>
            <span className="cg-dv" aria-hidden="true" />
            <span className="cg-seg cg-ans" title={`Answers: ${answers}`}>{Icon.answers}<span className="cg-t">{answers}</span></span>
          </>
        )}
      </button>
      {open && (
        <div className="cg-panel" role="group" aria-label="How it thinks" data-testid="think-panel">
          <div className="cg-panel-title">How it thinks</div>
          <div className="cg-row">
            <div className="cg-row-label"><b>Model</b><small>who answers</small></div>
            <ModelDropdown
              models={models}
              selectedModel={selectedModel}
              loadedLocalModel={loadedLocalModel}
              open={modelOpen}
              onToggle={onToggleModel}
              onSelect={onSelectModel}
            />
          </div>
          {levels.length > 0 ? (
            <div className="cg-row cg-row-stack">
              <div className="cg-row-label"><b>Thinking</b><small>how hard it works</small></div>
              <div className="cg-segctl" role="radiogroup" aria-label="Thinking level">
                {levels.map((lv) => (
                  <button
                    type="button"
                    key={lv}
                    role="radio"
                    aria-checked={lv === level}
                    className={lv === level ? 'on' : ''}
                    data-testid={`effort-option-${lv}`}
                    onClick={() => setLevel(lv)}
                  >
                    {labelOf(lv)}
                  </button>
                ))}
              </div>
              <div className="cg-note" data-testid="effort-note">{note}</div>
            </div>
          ) : (
            <div className="cg-row">
              <div className="cg-row-label"><b>Thinking</b><small>how hard it works</small></div>
              <span className="cg-muted">This model has no thinking levels</span>
            </div>
          )}
          {!local && (
            <div className="cg-row">
              <div className="cg-row-label"><b>Answers</b><small>reply length</small></div>
              <div className="cg-segctl" role="radiogroup" aria-label="Answer length">
                {VERBOSITY_LEVELS.map((lv) => (
                  <button
                    type="button"
                    key={lv}
                    role="radio"
                    aria-checked={lv === verbosity}
                    className={lv === verbosity ? 'on' : ''}
                    data-testid={`verbosity-option-${lv}`}
                    onClick={() => setVerbosity(lv)}
                  >
                    {VERBOSITY_LABEL[lv]}
                  </button>
                ))}
              </div>
            </div>
          )}
          {local && (
            <div className="cg-row">
              <div className="cg-row-label"><b>Context</b><small>memory it reads</small></div>
              <LocalContextWindowSlider />
            </div>
          )}
        </div>
      )}
    </div>
  );
};

interface WorkGroupProps {
  open: boolean;
  onToggle: () => void;
  onClose: () => void;
  connected: boolean;
  wsPath: string;
  recentPaths: string[];
  onBrowse: () => void;
  onConnect: (path: string) => void;
  permissionMode: string;
  onSelectMode: (mode: string) => void;
  onManage: () => void;
}

const baseName = (p: string): string => p.split('/').filter(Boolean).pop() || p;

export const WorkGroup: React.FC<WorkGroupProps> = ({
  open, onToggle, onClose, connected, wsPath, recentPaths, onBrowse, onConnect,
  permissionMode, onSelectMode, onManage,
}) => {
  const ref = useRef<HTMLDivElement>(null);
  useDismiss(ref, onClose, { enabled: open });
  const current = PERMISSION_MODES.find((m) => m.value === permissionMode);
  const modeLabel = current?.label ?? permissionMode;
  const wsName = connected && wsPath ? baseName(wsPath) : 'No workspace';

  return (
    <div className="cg-wrap" ref={ref}>
      <button
        type="button"
        className={`cg-group${open ? ' open' : ''}`}
        id="workGroup"
        aria-expanded={open}
        aria-label={`Where it works: ${wsName}, permissions ${modeLabel}`}
        onClick={onToggle}
      >
        <span className={`cg-seg cg-ws${connected ? ' on' : ''}`} title={connected ? wsPath : 'No workspace connected'}>{Icon.folder}<span className="cg-t">{wsName}</span></span>
        <span className="cg-dv" aria-hidden="true" />
        <span className={`cg-seg cg-perm tone-${modeTone(permissionMode)}`} title={`Permission mode: ${modeLabel}`}>{Icon.shield}<span className="cg-t">{modeLabel}</span></span>
      </button>
      {open && (
        <div className="cg-panel" role="group" aria-label="Where it works" data-testid="work-panel">
          <div className="cg-panel-title">Where it works</div>
          <div className="cg-row cg-row-stack">
            <div className="cg-row-label"><b>Workspace</b><small>files it can see</small></div>
            <div className="cg-ws-current" title={connected ? wsPath : undefined}>{connected ? wsPath : 'No workspace connected'}</div>
            <div className="cg-ws-list">
              <button type="button" className="cg-opt" data-testid="workspace-option-browse" onClick={onBrowse}>
                <b>Browse…</b><small>Folder browser with recents and sorting</small>
              </button>
              {recentPaths.filter((p) => p !== wsPath).map((p) => (
                <button type="button" key={p} className="cg-opt" title={p} data-testid={`workspace-option-${p}`} onClick={() => onConnect(p)}>
                  <b>{baseName(p)}</b><small>{p}</small>
                </button>
              ))}
            </div>
          </div>
          <div className="cg-row cg-row-stack">
            <div className="cg-row-label"><b>Permissions</b><small>what runs without asking</small></div>
            <div className="cg-modes">
              {PERMISSION_MODES.map((m) => (
                <button
                  type="button"
                  key={m.value}
                  className={`cg-opt cg-mode tone-${modeTone(m.value)}${permissionMode === m.value ? ' on' : ''}`}
                  data-testid={`mode-option-${m.value}`}
                  aria-pressed={permissionMode === m.value}
                  onClick={() => void onSelectMode(m.value)}
                >
                  <span className="cg-mode-icon">{Icon.shield}</span>
                  <span><b>{m.label}</b><small>{m.help}</small></span>
                </button>
              ))}
            </div>
            <button type="button" className="cg-link" data-testid="mode-option-manage" onClick={onManage}>Manage rules…</button>
          </div>
        </div>
      )}
    </div>
  );
};
