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

/** Fallback while /api/workspace/index/engines loads (or if it fails):
 *  the two engines every build ships. */
const FALLBACK_ENGINES: IndexEngineOption[] = [
  { key: 'haiku', label: 'Amazon Bedrock (Haiku)', local: false, downloaded: true },
  { key: 'local_gemma', label: 'Gemma 4 12B (Local)', local: true, downloaded: false },
];

/** The stored engine value normalized for the select: the legacy 'local'
 *  alias renders as the model it actually runs (local_gemma). */
const displayEngine = (engine: string): string => (engine === 'local' ? 'local_gemma' : engine);

const engineOptionLabel = (o: IndexEngineOption): string =>
  o.local && !o.downloaded ? `${o.label} - downloads on first pick` : o.label;

/**
 * The per-folder index settings rows — auto-refresh cadence + time, keep
 * refreshing when the app is closed, relationship mapping, and (on-device build)
 * the relationship engine picker. Shared between the recent-workspace ⋯ menu and
 * the new-folder section of the connect dialog, so a folder is configured the
 * same way before or after it lands in Recent.
 */
export const IndexSettingsPanel: React.FC<IndexSettingsPanelProps> = ({ settings: s, agentSupported, onChange, onEngineChange, onDescriptionsEngineChange }) => {
  // Engine choices: cloud Haiku + every on-device model from the config, with
  // download state for the "downloads on first pick" suffix. Shared cache key
  // across every menu that renders this panel.
  const enginesQuery = useQuery({
    queryKey: ['index-engines'],
    queryFn: indexEngines,
    staleTime: 60_000,
  });
  const engines = enginesQuery.data?.engines ?? FALLBACK_ENGINES;
  const engineLabel = (key: string) => engines.find((e) => e.key === key)?.label ?? key;
  const isLocalEngine = (key: string) => engines.find((e) => e.key === displayEngine(key))?.local ?? false;

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
        <select
          value={displayEngine(s.typed_relations.engine)}
          onChange={(e) => onEngineChange(e.target.value, engineLabel(e.target.value))}
        >
          <option value="none">Choose an engine…</option>
          {engines.map((o) => (
            <option key={o.key} value={o.key}>{engineOptionLabel(o)}</option>
          ))}
          <option value="gliner2">GLiNER2 native, local and fast (fewer links)</option>
        </select>
      </label>
    )}
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
        <select
          value={displayEngine(s.entity_descriptions.engine)}
          onChange={(e) => onDescriptionsEngineChange(e.target.value, engineLabel(e.target.value))}
        >
          <option value="none">Choose an engine…</option>
          {engines.map((o) => (
            <option key={o.key} value={o.key}>{engineOptionLabel(o)}</option>
          ))}
        </select>
      </label>
    )}
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
        <select
          value={displayEngine(s.chunk_context.engine)}
          onChange={(e) => onChange({ chunk_context: { engine: e.target.value } })}
        >
          {engines.map((o) => (
            <option key={o.key} value={o.key}>{engineOptionLabel(o)}</option>
          ))}
        </select>
      </label>
    )}
    <span className="ws-menu-hint">
      Adds the filename (or an AI-written summary) to each chunk before embedding, so filename and topic queries match. Changing the mode re-indexes the whole folder (AI-written also runs the model over every chunk).
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
