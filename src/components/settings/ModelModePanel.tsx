import React, { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useSettingsStore } from '@/stores/settingsStore';
import { indexEngines } from '@/api/workspace';
import type { IndexEngineOption } from '@/api/workspace';
import type { IndexCapability, ModelMode } from '@/types/settings';

interface Capability {
  key: IndexCapability;
  label: string;
  hint: string;
  cloudDefault: string;
  options: [string, string][];
}

/** Fallback while /api/workspace/index/engines loads (or if it fails). */
const FALLBACK_ENGINES: IndexEngineOption[] = [
  { key: 'haiku', label: 'Claude Haiku', local: false, downloaded: true },
  { key: 'local_gemma', label: 'Gemma 4 12B (Local)', local: true, downloaded: false },
];

const llmOption = (o: IndexEngineOption): [string, string] => [
  o.key,
  o.local
    ? o.downloaded
      ? o.label
      : `${o.label} — downloads on first use`
    : 'Claude Haiku — Bedrock',
];

/** Hybrid-mode per-capability backend options. The first option in each list is
 *  the cloud (Bedrock) backend, which is also the resolver's fallback when a
 *  capability is left unset, so we show it as the effective default. The two
 *  LLM-backed capabilities list Haiku plus every on-device model from the
 *  config (the same catalog as the per-folder index settings). */
const buildCapabilities = (engines: IndexEngineOption[]): Capability[] => [
  {
    key: 'embed',
    label: 'Embeddings (search index)',
    hint: 'Each backend keeps its own index, so a folder is indexed once per embedder. Switching never rebuilds the other.',
    cloudDefault: 'cohere',
    options: [['cohere', 'Cohere Embed v4 — Bedrock'], ['qwen3', 'Qwen3 — on-device']],
  },
  {
    key: 'rerank',
    label: 'Reranker',
    hint: 'Reorders retrieved passages before grounding.',
    cloudDefault: 'cohere',
    options: [['cohere', 'Cohere Rerank 3.5 — Bedrock'], ['qwen3', 'Qwen3 Reranker — on-device']],
  },
  {
    key: 'ner',
    label: 'Entity extraction',
    hint: 'Pulls people, orgs, and topics out of documents for the graph. GLiNER is the fast purpose-built extractor; a chat model reads more context but runs slower.',
    cloudDefault: 'haiku',
    options: [
      ...engines.filter((o) => !o.local).map(llmOption),
      ['gliner', 'GLiNER — on-device extractor'],
      ...engines.filter((o) => o.local).map(llmOption),
    ],
  },
  {
    key: 'index_llm',
    label: 'One-shot writer (summaries)',
    hint: 'Runs one-shot map calls such as transcript condensation. Relationship mapping and chunk headers are chosen per folder in its index settings.',
    cloudDefault: 'haiku',
    options: [
      ...engines.map(llmOption),
      ['local', 'On-device — follows the active local model'],
      ['none', 'Off'],
    ],
  },
];

const MODE_BLURB: Record<ModelMode, string> = {
  cloud: 'Everything runs on Amazon Bedrock. Nothing is downloaded or loaded on your machine.',
  hybrid: 'Pick a backend per capability below. Each indexing backend has its own separate index.',
  local: 'Everything runs on-device. Models load lazily when you start a session; nothing calls Bedrock.',
};

export const ModelModePanel: React.FC = () => {
  const mode = useSettingsStore((s) => s.config.modelMode);
  const backends = useSettingsStore((s) => s.config.backends);
  const setModelMode = useSettingsStore((s) => s.setModelMode);
  const setBackend = useSettingsStore((s) => s.setBackend);

  // Same catalog (and cache key) as the per-folder index settings pickers.
  const enginesQuery = useQuery({
    queryKey: ['index-engines'],
    queryFn: indexEngines,
    staleTime: 60_000,
  });
  const capabilities = buildCapabilities(enginesQuery.data?.engines ?? FALLBACK_ENGINES);

  // Changes persist immediately, but the model picker + any loaded on-device
  // model are initialized at startup — so the running app only fully switches on
  // a reload. Rather than interrupt with a modal (which would fire before a
  // hybrid user can configure the pickers), surface a non-blocking banner that
  // stays until they reload, so they can finish configuring first.
  const [pendingReload, setPendingReload] = useState(false);

  const changeMode = (next: ModelMode) => {
    if (next === mode) return;
    setModelMode(next);
    setPendingReload(true);
  };

  const changeBackend = (cap: IndexCapability, backend: string) => {
    if ((backends[cap] ?? '') === backend) return;
    setBackend(cap, backend);
    setPendingReload(true);
  };

  return (
    <div className="settings-form" style={{ maxWidth: 560 }}>
      <p className="settings-hint">
        Where indexing and retrieval run. Chat model selection is separate (the toolbar picker).
      </p>

      {pendingReload && (
        <div
          role="status"
          style={{
            display: 'flex', alignItems: 'center', justifyContent: 'space-between',
            gap: 12, padding: '10px 12px', marginBottom: 12, borderRadius: 8,
            background: 'var(--accent-dim)', border: '1px solid var(--accent)',
          }}
        >
          <span style={{ fontSize: 12, color: 'var(--text-primary)' }}>
            Saved. Reload the app to switch indexing, retrieval, and the model picker to the new mode.
          </span>
          <button
            type="button"
            className="btn btn-primary btn-sm"
            style={{ flexShrink: 0 }}
            onClick={() => window.location.reload()}
          >
            Reload now
          </button>
        </div>
      )}

      <label htmlFor="modelModeSelect">Mode</label>
      <select
        id="modelModeSelect"
        className="settings-input"
        value={mode}
        onChange={(e) => changeMode(e.target.value as ModelMode)}
      >
        <option value="cloud">Cloud — all Bedrock</option>
        <option value="hybrid">Hybrid — pick per capability</option>
        <option value="local">Local — all on-device</option>
      </select>
      <p className="settings-hint">{MODE_BLURB[mode]}</p>

      {mode === 'hybrid' && (
        <div className="settings-list" style={{ marginTop: 8 }}>
          {capabilities.map((cap) => (
            <div key={cap.key} className="settings-form" style={{ marginBottom: 10 }}>
              <label htmlFor={`backend-${cap.key}`}>{cap.label}</label>
              <select
                id={`backend-${cap.key}`}
                className="settings-input"
                value={backends[cap.key] ?? cap.cloudDefault}
                onChange={(e) => changeBackend(cap.key, e.target.value)}
              >
                {cap.options.map(([value, text]) => (
                  <option key={value} value={value}>{text}</option>
                ))}
              </select>
              <p className="settings-hint">{cap.hint}</p>
            </div>
          ))}
        </div>
      )}

      <p className="settings-hint" style={{ marginTop: 12 }}>
        Index storage is per embedder: a folder indexed with one embedder isn't re-indexed when you
        switch — the other index stays put. Re-index a folder once under a backend to search it there.
      </p>
    </div>
  );
};
