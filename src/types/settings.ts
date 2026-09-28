export type { ThemeKey } from './theme';

export type ModelMode = 'cloud' | 'hybrid' | 'local';
export type IndexCapability = 'embed' | 'rerank' | 'ner' | 'index_llm';

export interface AppConfig {
  chatModels: Record<string, string>;
  effortLevel: string;
  briefMode: boolean;
  permissionMode: string;
  /** Live ASR engine: 'whisper' (utterance), 'streaming' (Parakeet), or
   *  'canary' (25 EU languages + native translation). */
  transcriptionBackend: string;
  /** Translate dropdown: 'off' | 'canary' | 'apple' (legacy stored
   *  'auto'/'model' values are normalized to 'canary' on load). */
  translateMode: string;
  /** Target language for translation lines (ISO 639-1, default 'en'). */
  translateTarget: string;

  /** Where indexing/RAG runs: cloud (Bedrock) | hybrid | local (on-device). */
  modelMode: ModelMode;
  /** Per-capability backend overrides, consulted only in hybrid mode. */
  backends: Partial<Record<IndexCapability, string>>;
  /** First-run model-mode notice dismissed. Config-backed (not localStorage)
   *  so it survives the Mac app's per-launch localhost origin changes. */
  modeNoticeSeen: boolean;
}

/** Per-model metadata as returned by GET /api/models. */
export interface ModelEntry {
  key: string;
  name: string;
  requires_data_retention?: boolean;
  /** On-device model: runs via the local runtime, not Bedrock. */
  is_local?: boolean;
  /** Whether this local model has a toggleable thinking/reasoning mode. */
  supports_thinking?: boolean;
  /** Whether this local model can use tools (local agentic loop). */
  supports_tools?: boolean;
  /** Effort levels this model exposes (empty means no effort, e.g. Haiku). */
  effort_levels?: string[];
  default_effort?: string;
  supports_ultracode?: boolean;
  /** GPT-5.x verbosity control (text.verbosity). Only openai_bedrock models. */
  supports_verbosity?: boolean;
  default_verbosity?: string;
}

/** Shape returned by GET /api/models. */
export interface ModelsResponse {
  /** Only the models the active mode can run; may be empty. */
  models: ModelEntry[];
  /** The mode's default model, or '' when nothing is runnable. */
  default: string;
  /** Local mode with no on-device chat model installed: the composer shows an
   *  install hint (Settings > Models > Discover) and refuses to send. */
  needs_local_model: boolean;
}
