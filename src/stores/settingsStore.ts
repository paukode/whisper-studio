import { create } from 'zustand';
import type { AppConfig, IndexCapability } from '@/types/settings';
import { get, put } from '@/api/client';
import {
  AppConfigResponseSchema,
  ModelsResponseSchema,
  DataRetentionResponseSchema,
  PermissionsResponseSchema,
  MCPServersResponseSchema,
  SkillsResponseSchema,
} from '@/types/schemas';
import { useUIStore } from './uiStore';
import { clampEffort, normalizeEffort, DEFAULT_EFFORT } from '@/utils/effort';

/** Local-model context-window bounds (tokens). 16K is the floor AND the default:
 *  with tools on, the tool-pool prompt alone is ~12K tokens, so anything smaller
 *  overflows. The chat-input slider raises it incrementally up to Gemma's 256K
 *  maximum (which reloads the model); going above 16K prompts a memory
 *  confirmation. MIN === DEFAULT also means any stale sub-16K value in
 *  localStorage is coerced back up to 16K on read. Persisted to localStorage
 *  since the settings store has no persist middleware. */
const LOCAL_CTX_KEY = 'whisper.localContextWindow';
export const LOCAL_CTX_MIN = 16384;
export const LOCAL_CTX_MAX = 262144;
export const LOCAL_CTX_DEFAULT = 16384;

function readLocalContextWindow(): number {
  try {
    const v = Number(localStorage.getItem(LOCAL_CTX_KEY));
    return Number.isFinite(v) && v >= LOCAL_CTX_MIN && v <= LOCAL_CTX_MAX ? v : LOCAL_CTX_DEFAULT;
  } catch {
    return LOCAL_CTX_DEFAULT;
  }
}

/** Selected chat model is persisted to localStorage so a page refresh keeps the
 *  user's choice instead of snapping back to the backend default. (Same manual
 *  approach as the context window — the settings store has no persist
 *  middleware, and wrapping the whole store would be riskier.) */
const SELECTED_MODEL_KEY = 'whisper.selectedModel';

function readSelectedModel(): string | null {
  try {
    return localStorage.getItem(SELECTED_MODEL_KEY) || null;
  } catch {
    return null;
  }
}

function persistSelectedModel(model: string): void {
  try {
    localStorage.setItem(SELECTED_MODEL_KEY, model);
  } catch {
    /* private mode / quota — selection just won't persist this session */
  }
}

/** Response-length value (stored as GPT-5.x's native verbosity low/medium/high;
 *  the toolbar shows Brief/Normal/Detailed). Persisted so the choice survives a
 *  refresh, like the model selection. */
const VERBOSITY_KEY = 'whisper.verbosity';

function readVerbosity(): string | null {
  try {
    const v = localStorage.getItem(VERBOSITY_KEY);
    return v === 'low' || v === 'medium' || v === 'high' ? v : null;
  } catch {
    return null;
  }
}

function persistVerbosity(v: string): void {
  try {
    localStorage.setItem(VERBOSITY_KEY, v);
  } catch {
    /* private mode / quota — keep the in-memory value anyway */
  }
}

/** Which tools the local (Gemma) model may use this turn:
 *  - 'off'      — no tools (pure chat)
 *  - 'core'     — a small high-value set (read/search/edit files, run, git, memory)
 *  - 'core_web' — the core set plus web search + fetch
 *  - 'all'      — the full ~64-tool pool (parity with cloud; heaviest prompt)
 *  Fewer tools = a much smaller prompt = faster on-device. Persisted to
 *  localStorage so the choice survives a refresh. Default 'off' (opt-in). */
export type LocalToolScope = 'off' | 'core' | 'core_web' | 'all';
const LOCAL_TOOL_SCOPE_KEY = 'whisper.localToolScope';
const TOOL_SCOPES: readonly LocalToolScope[] = ['off', 'core', 'core_web', 'all'];

function readLocalToolScope(): LocalToolScope {
  try {
    const v = localStorage.getItem(LOCAL_TOOL_SCOPE_KEY) as LocalToolScope | null;
    return v && TOOL_SCOPES.includes(v) ? v : 'off';
  } catch {
    return 'off';
  }
}

/** Per-model metadata as returned by GET /api/models. */
export interface ModelEntry {
  key: string;
  name: string;
  requires_data_retention?: boolean;
  /** On-device model — runs via the local runtime, not Bedrock. */
  is_local?: boolean;
  /** Whether this local model has a toggleable thinking/reasoning mode. */
  supports_thinking?: boolean;
  /** Whether this local model can use tools (local agentic loop). */
  supports_tools?: boolean;
  /** Effort levels this model exposes (empty ⇒ no effort, e.g. Haiku). */
  effort_levels?: string[];
  default_effort?: string;
  supports_ultracode?: boolean;
  /** GPT-5.x verbosity control (text.verbosity). Only openai_bedrock models. */
  supports_verbosity?: boolean;
  default_verbosity?: string;
}

/** Shape returned by GET /api/models */
interface ModelsResponse {
  models: ModelEntry[];
  default: string;
}

/** Shape returned by GET /api/skills */
interface SkillEntry {
  name: string;
  description?: string;
  enabled: boolean;
  isFolder?: boolean;
  hasScripts?: boolean;
  trusted?: boolean;
}

/** Shape returned by GET /api/mcp/servers */
interface MCPEntry {
  name: string;
  status: string;
  /** Persisted opt-in flag — see server/mcp.py::get_bedrock_tools.
   *  False (default) means the server's tools are not advertised to
   *  Bedrock unless overridden per-request. */
  enabled: boolean;
  tools?: Array<{ name: string; description?: string }>;
}

export interface SettingsState {
  config: AppConfig;
  isLoading: boolean;

  /* Models */
  models: ModelEntry[];
  defaultModel: string;
  selectedModel: string;
  /** Which on-device model is actually RESIDENT in server memory right now, or
   *  null if none. Distinct from selectedModel: in local/hybrid mode a local
   *  model can be the selection without being loaded (we no longer eager-load at
   *  startup — the user loads one when they start a session). Drives the
   *  "select a model to start" cue and lets re-selecting an unloaded model still
   *  trigger the load. In-memory only (reset on reload); the backend's
   *  load_sync is idempotent so a redundant load after reload is a cheap no-op. */
  loadedLocalModel: string | null;

  /** Whether the AWS account's Bedrock data-retention mode is currently
   *  provider_data_share. Models flagged requires_data_retention (Fable 5)
   *  only work when this is on; the picker gates selection behind a consent
   *  screen that flips it via PUT /api/data-retention. */
  dataRetentionEnabled: boolean;

  /* Skills */
  skills: SkillEntry[];

  /* MCP — the persisted `enabled` flag on each server is the single source of
   *  truth. Both the Settings panel and the chat-toolbar checklist toggle it
   *  (via useMcpToggle); the backend resolves the active set from these flags. */
  mcpServers: MCPEntry[];

  /* Effort & brief */
  effortLevel: string;
  /** GPT-5.x verbosity (text.verbosity); only used by openai_bedrock models. */
  verbosity: string;
  planMode: boolean;
  autoMemory: boolean;
  /** Local-model thinking toggle (opt-in reasoning for on-device models that
   *  support it, e.g. Gemma). Client-only, sent per chat turn. */
  localThinking: boolean;
  /** Local-model tools toggle (opt-in tool use for on-device models, off by
   *  default to keep local fully offline). Client-only, sent per chat turn. */
  localToolScope: LocalToolScope;
  /** Local-model context window (tokens). Drives the chat-input slider; changing
   *  it reloads the on-device model at the new size. Persisted to localStorage. */
  localContextWindow: number;

  /* Actions */
  loadConfig: () => Promise<void>;
  loadModels: () => Promise<void>;
  loadDataRetention: () => Promise<void>;
  setDataRetentionEnabled: (on: boolean) => void;
  loadSkills: () => Promise<void>;
  loadMCP: () => Promise<void>;
  /** Optimistically set a server's persisted enabled flag in the live store
   *  copy (the PATCH + rollback is owned by useMcpToggle). */
  setMcpServerEnabled: (name: string, enabled: boolean) => void;
  updateConfig: (partial: Partial<AppConfig>) => void;
  setConfig: (config: AppConfig) => void;
  setSelectedModel: (model: string) => void;
  setLoadedLocalModel: (model: string | null) => void;
  setEffortLevel: (level: string) => void;
  setVerbosity: (v: string) => void;
  setPlanMode: (on: boolean) => void;
  setAutoMemory: (on: boolean) => void;
  /** Set the index/RAG model mode (cloud | hybrid | local), persisting to config. */
  setModelMode: (mode: AppConfig['modelMode']) => void;
  /** Set a hybrid-mode per-capability backend override, persisting to config. */
  setBackend: (capability: IndexCapability, backend: string) => void;
  setLocalThinking: (on: boolean) => void;
  setLocalToolScope: (scope: LocalToolScope) => void;
  setLocalContextWindow: (size: number) => void;
}

const defaultConfig: AppConfig = {
  bedrockRegion: 'us-east-1',
  chatModels: {},
  defaultChatModel: '',
  effortLevel: DEFAULT_EFFORT,
  briefMode: false,
  permissionMode: 'default',
  autoModeEnabled: false,
  transcriptionBackend: 'streaming',
  modelMode: 'cloud',
  backends: {},
};

/** Decide which chat model is active after loading the model list, in priority
 *  order: (1) the user's persisted choice if it's still a valid model (this is
 *  what survives a hard refresh), (2) an on-device model if one is offered — on
 *  local builds the UI defaults to Gemma, while the *backend* default stays a
 *  cloud model so headless / model-less requests never load the local weights,
 *  (3) the backend default. Pure + exported for unit testing. */
export function pickActiveModel(
  models: ModelEntry[],
  backendDefault: string,
  persisted: string | null,
): string {
  if (persisted && models.some((m) => m.key === persisted)) return persisted;
  return models.find((m) => m.is_local)?.key ?? backendDefault;
}

export const useSettingsStore = create<SettingsState>()((set, _get) => ({
  config: { ...defaultConfig },
  isLoading: false,
  models: [],
  defaultModel: 'opus4.8',
  // Hydrate from the persisted choice so there's no flash of the wrong model
  // before loadModels resolves; loadModels then validates it against the list.
  selectedModel: readSelectedModel() ?? 'opus4.8',
  // Nothing is resident until the user loads a model (lazy in local mode).
  loadedLocalModel: null,
  dataRetentionEnabled: false,
  skills: [],
  mcpServers: [],
  effortLevel: DEFAULT_EFFORT,
  verbosity: readVerbosity() ?? 'medium',
  planMode: false,
  // Global memory defaults ON (matches config.example.json feature_flags.auto_memory).
  autoMemory: true,
  localThinking: false,
  localToolScope: readLocalToolScope(),
  localContextWindow: readLocalContextWindow(),

  loadConfig: async () => {
    set({ isLoading: true });
    try {
      const data = await get<Record<string, unknown>>('/api/config', { schema: AppConfigResponseSchema });
      const parsed = AppConfigResponseSchema.safeParse(data);
      const d = parsed.success ? parsed.data : data as Record<string, unknown>;
      const config: AppConfig = {
        bedrockRegion: String(d.bedrock_region || 'us-east-1'),
        chatModels: (d.chat_models as Record<string, string>) ?? {},
        defaultChatModel: String(d.default_chat_model ?? ''),
        effortLevel: normalizeEffort(d.effort_level as string | undefined),
        briefMode: Boolean(d.brief_mode ?? false),
        permissionMode: String(d.permission_mode ?? 'default'),
        autoModeEnabled: Boolean(d.auto_mode ?? false),
        transcriptionBackend: String(d.transcription_backend ?? 'streaming'),
        modelMode: ((d.model_mode as AppConfig['modelMode']) ?? 'cloud'),
        backends: ((d.backends as AppConfig['backends']) ?? {}),
      };
      set({
        config,
        effortLevel: normalizeEffort(config.effortLevel),
      });

      // One-time migration: the old standalone brief toggle is now the "Brief"
      // end of the unified Response length control (stored as verbosity). If the
      // user hasn't picked a length yet, carry over their brief preference.
      if (!readVerbosity() && config.briefMode) {
        persistVerbosity('low');
        set({ verbosity: 'low' });
      }

      // Load actual permission mode from the permissions endpoint (ground truth)
      try {
        const perms = await get<{ mode?: string }>('/api/permissions', { schema: PermissionsResponseSchema });
        const actualMode = perms.mode ?? 'default';
        set({ planMode: actualMode === 'plan' });
        set((state) => ({ config: { ...state.config, permissionMode: actualMode } }));
      } catch {
        // Permissions API not available — fall back to config value
        set({ planMode: config.permissionMode === 'plan' });
      }

      // Hydrate the global-memory toggle from its backend feature flag so the
      // toolbar reflects the real state (and the on-by-default config value),
      // rather than only the store's initial default.
      try {
        const flags = await get<Record<string, { enabled?: boolean }>>('/api/feature-flags');
        set({ autoMemory: !!flags.auto_memory?.enabled });
      } catch {
        // Flags API unavailable — keep the current value.
      }
    } catch (err) {
      console.warn('Failed to load config:', err);
      useUIStore.getState().addToast({
        type: 'error',
        message: 'Failed to load config',
        duration: 4000,
      });
    } finally {
      set({ isLoading: false });
    }
  },

  loadModels: async () => {
    try {
      const data = await get<ModelsResponse>('/api/models', { schema: ModelsResponseSchema });
      const models = data.models ?? [];
      const def = data.default ?? 'opus4.8';
      set((state) => {
        const chosen = pickActiveModel(models, def, readSelectedModel());
        const allowed = models.find((m) => m.key === chosen)?.effort_levels ?? [];
        return {
          models,
          defaultModel: def,
          selectedModel: chosen,
          // Reconcile effort to whatever the chosen model supports.
          ...(allowed.length ? { effortLevel: clampEffort(state.effortLevel, allowed) } : {}),
        };
      });
    } catch (err) {
      console.warn('Failed to load models:', err);
      useUIStore.getState().addToast({
        type: 'error',
        message: 'Failed to load model list',
        duration: 4000,
      });
    }
  },

  loadDataRetention: async () => {
    try {
      const data = await get<{ mode: string; enabled: boolean }>(
        '/api/data-retention', { schema: DataRetentionResponseSchema });
      set({ dataRetentionEnabled: !!data.enabled });
    } catch (err) {
      // Read-only probe. If the identity lacks GetAccountDataRetention or the
      // call fails, assume off — the consent flow surfaces a real error on the
      // subsequent enable attempt.
      console.warn('Failed to load data-retention state:', err);
      set({ dataRetentionEnabled: false });
    }
  },

  setDataRetentionEnabled: (on) => {
    set({ dataRetentionEnabled: on });
  },

  loadSkills: async () => {
    try {
      const data = await get<{ skills: SkillEntry[]; mcpTools: Array<{ name: string; description: string; server: string }> }>('/api/skills', { schema: SkillsResponseSchema });
      set({ skills: Array.isArray(data.skills) ? data.skills : [] });
    } catch (err) {
      console.warn('Failed to load skills:', err);
      useUIStore.getState().addToast({
        type: 'error',
        message: 'Failed to load skills',
        duration: 4000,
      });
    }
  },

  loadMCP: async () => {
    try {
      const data = await get<{ servers: Record<string, { name?: string; command: string; args: string[]; enabled?: boolean; status: string; error?: string | null }> }>('/api/mcp/servers', { schema: MCPServersResponseSchema });
      const serversObj = data.servers ?? {};
      const list: MCPEntry[] = Object.entries(serversObj).map(([name, info]) => ({
        name,
        status: info.status ?? 'stopped',
        enabled: !!info.enabled,
      }));
      set({ mcpServers: list });
    } catch (err) {
      console.warn('Failed to load MCP servers:', err);
      useUIStore.getState().addToast({
        type: 'error',
        message: 'Failed to load MCP servers',
        duration: 4000,
      });
    }
  },

  updateConfig: (partial) => {
    set((state) => ({
      config: { ...state.config, ...partial },
    }));
  },

  setConfig: (config) => {
    set({ config });
  },

  setMcpServerEnabled: (name, enabled) => {
    set((state) => ({
      mcpServers: state.mcpServers.map((s) => (s.name === name ? { ...s, enabled } : s)),
    }));
  },

  setSelectedModel: (model) => {
    // Reconcile the effort level to what the new model supports: keep it if
    // valid, otherwise clamp to the nearest lower level (Ultracode → Max,
    // Extra → High on a standard-tier model). Effort-less models (Haiku) leave
    // the stored level untouched so it restores when switching back.
    const { models, effortLevel } = _get();
    const allowed = models.find((m) => m.key === model)?.effort_levels ?? [];
    persistSelectedModel(model); // survive page refresh / hard refresh
    set({
      selectedModel: model,
      ...(allowed.length ? { effortLevel: clampEffort(effortLevel, allowed) } : {}),
    });
  },

  setLoadedLocalModel: (model) => {
    set({ loadedLocalModel: model });
  },

  setEffortLevel: (level) => {
    set({ effortLevel: level });
  },

  setVerbosity: (v) => {
    persistVerbosity(v);
    set({ verbosity: v });
  },

  setPlanMode: (on) => {
    set({ planMode: on });
  },

  setAutoMemory: (on) => {
    // Optimistic: flip the toolbar immediately, then persist the backend
    // feature flag (the actual control for memory recall/extraction). Roll
    // back the toggle if the write fails so the UI never lies about state.
    const prev = useSettingsStore.getState().autoMemory;
    set({ autoMemory: on });
    void put('/api/feature-flags/auto_memory', { enabled: on }).catch(() => {
      set({ autoMemory: prev });
      useUIStore.getState().addToast({
        type: 'error',
        message: 'Could not update global memory',
        duration: 3000,
      });
    });
  },

  setModelMode: (mode) => {
    // Optimistic: flip the mode immediately, persist via PUT /api/config, roll
    // back + toast on failure so the UI never lies about the active mode.
    const prev = useSettingsStore.getState().config.modelMode;
    set((s) => ({ config: { ...s.config, modelMode: mode } }));
    void put('/api/config', { model_mode: mode }).catch(() => {
      set((s) => ({ config: { ...s.config, modelMode: prev } }));
      useUIStore.getState().addToast({ type: 'error', message: 'Could not update model mode', duration: 3000 });
    });
  },

  setBackend: (capability, backend) => {
    const prev = useSettingsStore.getState().config.backends;
    const next = { ...prev, [capability]: backend };
    set((s) => ({ config: { ...s.config, backends: next } }));
    void put('/api/config', { backends: next }).catch(() => {
      set((s) => ({ config: { ...s.config, backends: prev } }));
      useUIStore.getState().addToast({ type: 'error', message: 'Could not update backend', duration: 3000 });
    });
  },

  setLocalThinking: (on) => {
    set({ localThinking: on });
  },

  setLocalToolScope: (scope) => {
    try {
      localStorage.setItem(LOCAL_TOOL_SCOPE_KEY, scope);
    } catch {
      /* private mode / quota — keep the in-memory value anyway */
    }
    set({ localToolScope: scope });
  },

  setLocalContextWindow: (size) => {
    const v = Math.max(LOCAL_CTX_MIN, Math.min(Math.round(size), LOCAL_CTX_MAX));
    try {
      localStorage.setItem(LOCAL_CTX_KEY, String(v));
    } catch {
      /* private mode / quota — keep the in-memory value anyway */
    }
    set({ localContextWindow: v });
  },
}));
