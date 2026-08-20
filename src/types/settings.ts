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
  /** Who produces the English line: 'model' (the ASR engine) or 'apple'
   *  (on-device Translation framework, Mac app only). */
  translationProvider: string;
  /** Whisper only: also decode non-English utterances to English and show
   *  the translation under the transcript line. */
  whisperTranslate: boolean;
  /** Where indexing/RAG runs: cloud (Bedrock) | hybrid | local (on-device). */
  modelMode: ModelMode;
  /** Per-capability backend overrides, consulted only in hybrid mode. */
  backends: Partial<Record<IndexCapability, string>>;
}
