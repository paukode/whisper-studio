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
}
