export type { ThemeKey } from './theme';

export type ModelMode = 'cloud' | 'hybrid' | 'local';
export type IndexCapability = 'embed' | 'rerank' | 'ner' | 'index_llm';

export interface AppConfig {
  bedrockRegion: string;
  chatModels: Record<string, string>;
  defaultChatModel: string;
  effortLevel: string;
  briefMode: boolean;
  permissionMode: string;
  autoModeEnabled: boolean;
  /** Live ASR engine: 'whisper' (utterance) or 'streaming' (Parakeet). */
  transcriptionBackend: string;
  /** Where indexing/RAG runs: cloud (Bedrock) | hybrid | local (on-device). */
  modelMode: ModelMode;
  /** Per-capability backend overrides, consulted only in hybrid mode. */
  backends: Partial<Record<IndexCapability, string>>;
}

export interface MCPServer {
  name: string;
  command: string;
  args: string[];
  env?: Record<string, string>;
  status: 'running' | 'stopped' | 'error';
}
