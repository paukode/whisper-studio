import React from 'react';
import { useQuery } from '@tanstack/react-query';
import { indexEngines } from '@/api/workspace';
import type { IndexEngineOption, IndexSettings, IndexSettingsPatch } from '@/api/workspace';

interface IndexSettingsPanelProps {
  settings: IndexSettings;
  /** Whether the background-refresh helper is supported (macOS launchd). */
  agentSupported: boolean;
  onChange: (patch: IndexSettingsPatch) => void;
  /** Engine changes route through the dialog so it can download the picked
   *  on-device model first. The label is for the download banner. */
  onEngineChange: (engine: string, label: string) => void;
  /** Same download routing for the entity-descriptions engine. */
  onDescriptionsEngineChange: (engine: string, label: string) => void;
}

const WEEKDAYS = [
  ['mon', 'Monday'], ['tue', 'Tuesday'], ['wed', 'Wednesday'], ['thu', 'Thursday'],
  ['fri', 'Friday'], ['sat', 'Saturday'], ['sun', 'Sunday'],
] as const;

/** The stored engine value normalized for the select. The legacy 'local' alias
 *  means "whichever on-device model is installed", so it renders as the model it
 *  actually runs: the preferred default when present, else the first local row.
 *  Anything else (a key that isn't in the catalog) is returned as-is so the
 *  select can flag it rather than silently showing a different model. */
const displayEngine = (engine: string, engines: IndexEngineOption[]): string => {
  if (engine !== 'local') return engine;
  const locals = engines.filter((o) => o.local);
  if (locals.some((o) => o.key === 'local_gemma')) return 'local_gemma';
  return locals[0]?.key ?? 'local';
};

const engineOptionLabel = (o: IndexEngineOption): string =>
  o.local && !o.downloaded ? `${o.label} - downloads on first pick` : o.label;

/** Placeholder value for a stored engine the catalog no longer offers (a folder
 *  set to Haiku while in cloud mode, opened again in local mode). */
const UNAVAILABLE = '__unavailable';

interface EngineSelectProps {
  value: string;
  engines: IndexEngineOption[];
  /** Rendered first with value 'none' — the pass is configured but no engine
   *  chosen yet. Omitted for passes whose stored value has no 'none' sentinel. */
  allowNone?: boolean;
  /** Non-LLM choices appended after the models (relationship mapping's GLiNER2). */
  extra?: [string, string][];
  onChange: (engine: string, label: string) => void;
}

/** One engine picker. The option list is exactly what the backend offers for the
 *  active model mode — nothing is hardcoded and nothing is pre-picked, so in
 *  local mode with no installed model the list is empty (the caller renders the
 *  download note underneath) instead of naming a model that cannot run. */
const EngineSelect: React.FC<EngineSelectProps> = ({
  value,
  engines,
  allowNone,
  extra = [],
  onChange,
}) => {
  const resolved = displayEngine(value, engines);
  const known =
    (allowNone && resolved === 'none') ||
    engines.some((o) => o.key === resolved) ||
    extra.some(([v]) => v === resolved);
  const label = (key: string) =>
    engines.find((o) => o.key === key)?.label ??
    extra.find(([v]) => v === key)?.[1] ??
    key;
  return (
    <select
      value={known ? resolved : UNAVAILABLE}
      disabled={engines.length === 0 && extra.length === 0}
      onChange={(e) => {
        if (e.target.value === UNAVAILABLE) return;
        onChange(e.target.value, label(e.target.value));
      }}
    >
      {allowNone && <option value="none">Choose a model…</option>}
      {!known && (
        <option value={UNAVAILABLE} disabled>
          {engines.length === 0 ? 'No model available' : 'Not available in this model mode'}
        </option>
      )}
      {engines.map((o) => (
        <option key={o.key} value={o.key}>{engineOptionLabel(o)}</option>
      ))}
      {extra.map(([v, text]) => (
        <option key={v} value={v}>{text}</option>
      ))}
    </select>
  );
};

/** Shown under an empty picker: in local mode the catalog is the user's own
 *  downloaded on-device models, so an empty list means "go install one". */
const NoModelsNote: React.FC = () => (
  <span className="ws-menu-hint" style={{ marginLeft: 22 }}>
    No on-device chat model yet. Download one in Settings &gt; Models &gt; Chat, then pick it here.
  </span>
);

/**
 * The per-folder index settings rows — auto-refresh cadence + time, keep
 * refreshing when the app is closed, relationship mapping, and (on-device build)
 * the relationship engine picker. Shared between the recent-workspace ⋯ menu and
 * the new-folder section of the connect dialog, so a folder is configured the
 * same way before or after it lands in Recent.
 */
export const IndexSettingsPanel: React.FC<IndexSettingsPanelProps> = ({ settings: s, agentSupported, onChange, onEngineChange, onDescriptionsEngineChange }) => {
  // Engine choices for the active model mode: cloud/hybrid list Haiku + every
  // on-device model from the config (with download state for the "downloads on
  // first pick" suffix), local lists only the models already on disk. No
  // hardcoded fallback — an empty list is a real state (nothing installed yet)
  // and naming a model the user doesn't have is what we're fixing. Shared cache
  // key across every menu that renders this panel.
  const enginesQuery = useQuery({
    queryKey: ['index-engines'],
    queryFn: indexEngines,
    staleTime: 60_000,
  });
  const engines = enginesQuery.data?.engines ?? [];
  const noModels = engines.length === 0;
  const isLocalEngine = (key: string) =>
    engines.find((e) => e.key === displayEngine(key, engines))?.local ?? false;

  return (
  <div className="ws-menu-settings">
    <label className="ws-menu-line">
      <input
        type="checkbox"
        checked={s.schedule.enabled}
        onChange={(e) => onChange({ schedule: { enabled: e.target.checked } })}
      />
      Auto-refresh
      <select
        value={s.schedule.frequency}
        disabled={!s.schedule.enabled}
        onChange={(e) => onChange({ schedule: { frequency: e.target.value as IndexSettings['schedule']['frequency'] } })}
      >
        <option value="daily">daily</option>
        <option value="every_n_days">every</option>
        <option value="weekly">weekly</option>
      </select>
      {s.schedule.frequency === 'every_n_days' && (
        <>
          <select
            value={s.schedule.interval_days}
            disabled={!s.schedule.enabled}
            onChange={(e) => onChange({ schedule: { interval_days: Number(e.target.value) } })}
          >
            {Array.from({ length: 30 }, (_, i) => i + 1).map((n) => (<option key={n} value={n}>{n}</option>))}
          </select>
          days
        </>
      )}
      {s.schedule.frequency === 'weekly' && (
        <>
          on
          <select
            value={s.schedule.weekday}
            disabled={!s.schedule.enabled}
            onChange={(e) => onChange({ schedule: { weekday: e.target.value as IndexSettings['schedule']['weekday'] } })}
          >
            {WEEKDAYS.map(([v, label]) => (<option key={v} value={v}>{label}</option>))}
          </select>
        </>
      )}
      at
      <select
        value={s.schedule.hour}
        disabled={!s.schedule.enabled}
        onChange={(e) => onChange({ schedule: { hour: Number(e.target.value) } })}
      >
        {Array.from({ length: 24 }, (_, h) => (<option key={h} value={h}>{String(h).padStart(2, '0')}:00</option>))}
      </select>
    </label>
    {agentSupported && (
      <label className="ws-menu-line">
        <input
          type="checkbox"
          checked={s.refresh_when_closed}
          disabled={!s.schedule.enabled}
          onChange={(e) => onChange({ refresh_when_closed: e.target.checked })}
        />
        Keep refreshing even when the app is closed
      </label>
    )}
    <label className="ws-menu-line">
      <input
        type="checkbox"
        checked={s.typed_relations.enabled}
        onChange={(e) => onChange({ typed_relations: { enabled: e.target.checked } })}
      />
      Map relationships between people, companies, and topics
    </label>
    {s.typed_relations.enabled && (
      <label className="ws-menu-line" style={{ marginLeft: 22 }}>
        Build it using
        <EngineSelect
          value={s.typed_relations.engine}
          engines={engines}
          allowNone
          extra={[['gliner2', 'GLiNER2 native, local and fast (fewer links)']]}
          onChange={onEngineChange}
        />
      </label>
    )}
    {s.typed_relations.enabled && noModels && <NoModelsNote />}
    {s.typed_relations.enabled && isLocalEngine(s.typed_relations.engine) && (
      <span className="ws-menu-hint">
        Heavy: loads the model on your device, so chatting may slow down and indexing takes much longer.
      </span>
    )}
    {s.typed_relations.enabled && s.typed_relations.engine === 'gliner2' && (
      <span className="ws-menu-hint">
        Fully on-device and fast (a small extractor model, no language model), but finds far fewer relationships than Haiku or Gemma. Best when speed and privacy matter more than depth.
      </span>
    )}
    <label className="ws-menu-line">
      <input
        type="checkbox"
        checked={s.entity_descriptions?.enabled ?? false}
        onChange={(e) => onChange({ entity_descriptions: { enabled: e.target.checked } })}
      />
      Write short AI descriptions for people, companies, and topics
    </label>
    {s.entity_descriptions?.enabled && (
      <label className="ws-menu-line" style={{ marginLeft: 22 }}>
        Written by
        <EngineSelect
          value={s.entity_descriptions.engine}
          engines={engines}
          allowNone
          onChange={onDescriptionsEngineChange}
        />
      </label>
    )}
    {s.entity_descriptions?.enabled && noModels && <NoModelsNote />}
    {s.entity_descriptions?.enabled && (
      <span className="ws-menu-hint">
        One-line summaries shown on entities in the knowledge graph, written at
        the end of the next index refresh.
      </span>
    )}
    <label className="ws-menu-line">
      Contextual headers
      <select
        value={s.chunk_context?.mode ?? 'filename'}
        onChange={(e) => onChange({ chunk_context: { mode: e.target.value as IndexSettings['chunk_context']['mode'] } })}
      >
        <option value="off">Off</option>
        <option value="filename">Filename (free, offline)</option>
        <option value="llm">AI-written (richer)</option>
      </select>
    </label>
    {s.chunk_context?.mode === 'llm' && (
      <label className="ws-menu-line" style={{ marginLeft: 22 }}>
        Written by
        <EngineSelect
          value={s.chunk_context.engine}
          engines={engines}
          onChange={(engine) => onChange({ chunk_context: { engine } })}
        />
      </label>
    )}
    {s.chunk_context?.mode === 'llm' && noModels && <NoModelsNote />}
    <span className="ws-menu-hint">
      Adds the filename (or an AI-written summary) to each passage before embedding, so filename and topic queries match. Changing the mode re-indexes the whole folder (AI-written also runs the model over every passage).
    </span>
    <label className="ws-menu-line">
      Entity extractor
      <select
        value={s.ner_model ?? 'gliner'}
        onChange={(e) => onChange({ ner_model: e.target.value as IndexSettings['ner_model'] })}
      >
        <option value="gliner">GLiNER large (multilingual)</option>
        <option value="gliner2">GLiNER2 (English-focused, faster)</option>
      </select>
    </label>
    <span className="ws-menu-hint">
      GLiNER2 is English-strong and a little faster; keep GLiNER large for multilingual documents. The entity types (business vs code) are chosen automatically per file. Changing this re-indexes the folder.
    </span>
  </div>
  );
};
