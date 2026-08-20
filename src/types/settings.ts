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
  /** Whisper checkpoint: 'turbo' (fast) or 'large-v3' (accuracy + true
   *  translate head, slower). */
  whisperVariant: string;
  /** Translate dropdown: 'off' | 'auto' | 'model' | 'apple'. */
  translateMode: string;

  /** Where indexing/RAG runs: cloud (Bedrock) | hybrid | local (on-device). */
  modelMode: ModelMode;
  /** Per-capability backend overrides, consulted only in hybrid mode. */
  backends: Partial<Record<IndexCapability, string>>;
}
