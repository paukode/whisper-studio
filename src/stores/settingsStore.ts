import { create } from 'zustand';
import type { AppConfig, IndexCapability, ModelEntry, ModelsResponse } from '@/types/settings';
import { get, put } from '@/api/client';
import {
  AppConfigResponseSchema,
  ModelsResponseSchema,
  DataRetentionResponseSchema,
  PermissionsResponseSchema,
  SkillsResponseSchema,
} from '@/types/schemas';
import { useUIStore } from './uiStore';
import { clampEffort, normalizeEffort, DEFAULT_EFFORT } from '@/utils/effort';

/** Local-model context-window bounds (tokens). 32K is the floor AND the default:
 *  the full tool pool is always advertised and its prompt alone is ~12K tokens,
 *  so anything smaller leaves no room for transcript + conversation. The
 *  chat-input slider raises it incrementally up to Gemma's 256K maximum (which
 *  reloads the model); going above 32K prompts a memory confirmation.
 *  MIN === DEFAULT also means any stale sub-32K value in localStorage is
 *  coerced back up to 32K on read. Persisted to localStorage since the
 *  settings store has no persist middleware. */
const LOCAL_CTX_KEY = 'whisper.localContextWindow';
export const LOCAL_CTX_MIN = 32768;
export const LOCAL_CTX_MAX = 262144;
export const LOCAL_CTX_DEFAULT = 32768;

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
/** Voice mode is refused in Local mode, and the Talk button shows why: re-read
 *  its availability once the server holds a new mode. Loaded lazily (the voice
 *  controller imports this store); never rejects. */
function refreshVoiceStatus(): void {
  void import('@/services/voiceController')
    .then(({ voiceController }) => voiceController.loadStatus(true))
    .catch(() => {});
}

/** Hydrate the Memory toggle from its backend feature flag, so the toolbar
 *  shows the real state and what the active mode leaves out of it (Local mode
 *  recalls memories but records none). Re-read after a mode switch. Never
 *  rejects: with the flags API unavailable the current value stays. */
async function loadMemoryFlag(): Promise<void> {
  try {
    const flags = await get<Record<string, { enabled?: boolean; local_mode_note?: string | null }>>(
      '/api/feature-flags',
    );
    useSettingsStore.setState({
      autoMemory: !!flags.auto_memory?.enabled,
      autoMemoryNote: flags.auto_memory?.local_mode_note || null,
    });
  } catch {
    // Flags API unavailable: keep the current value.
  }
}

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

export type { ModelEntry } from '@/types/settings';

/** Shape returned by GET /api/skills */
interface SkillEntry {
  name: string;
  description?: string;
  enabled: boolean;
  isFolder?: boolean;
  hasScripts?: boolean;
  trusted?: boolean;
}

export interface SettingsState {
  config: AppConfig;

  /* Models */
  models: ModelEntry[];
  /** '' when nothing is selectable (Local mode with no on-device model). */
  selectedModel: string;
  /** Local mode with no on-device chat model installed (GET /api/models): the
   *  picker shows the install hint and the composer refuses to send. */
  needsLocalModel: boolean;
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

  /* Effort & brief */
  effortLevel: string;
  /** GPT-5.x verbosity (text.verbosity); only used by openai_bedrock models. */
  verbosity: string;
  planMode: boolean;
  autoMemory: boolean;
  /** What the active mode leaves out of auto memory (Local mode recalls but
   *  records nothing), from the flag's `local_mode_note`; null when nothing. */
  autoMemoryNote: string | null;
  /** Local-model context window (tokens). Drives the chat-input slider; changing
   *  it reloads the on-device model at the new size. Persisted to localStorage. */
  localContextWindow: number;

  /* Actions */
  loadConfig: () => Promise<void>;
  loadModels: () => Promise<void>;
  loadDataRetention: () => Promise<void>;
  setDataRetentionEnabled: (on: boolean) => void;
  loadSkills: () => Promise<void>;
  updateConfig: (partial: Partial<AppConfig>) => void;
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
  setLocalContextWindow: (size: number) => void;
  /** Dismiss the first-run model-mode notice, persisting to config so it
   *  never re-shows on later launches (any install, any origin). */
  markModeNoticeSeen: () => void;
}

const defaultConfig: AppConfig = {
  chatModels: {},
  effortLevel: DEFAULT_EFFORT,
  briefMode: false,
  permissionMode: 'default',
  transcriptionBackend: 'streaming',
  translateMode: 'off',
  translateTarget: 'en',
  modelMode: 'cloud',
  backends: {},
  // Default TRUE pre-load: the first-run notice may only appear after the
  // server config explicitly reports it unseen, never as a pre-fetch flash.
  modeNoticeSeen: true,
};

/** Decide which chat model is active after loading the model list, in priority
 *  order: (1) the user's persisted choice if it's still a valid model (this is
 *  what survives a hard refresh), (2) an on-device model if one is offered (on
 *  local builds the UI defaults to Gemma, while the configured backend default
 *  stays a cloud model so headless / model-less requests in Cloud or Hybrid mode
 *  never load the local weights), (3) the backend default, which is '' when the
 *  active mode can run nothing. Pure + exported for unit testing. */
export function pickActiveModel(
  models: ModelEntry[],
  backendDefault: string,
  persisted: string | null,
): string {
  if (persisted && models.some((m) => m.key === persisted)) return persisted;
  return models.find((m) => m.is_local)?.key ?? backendDefault;
}

/** Why there is no chat model to use, worded for the picker's empty state and
 *  the send refusal alike. */
export function noChatModelHint(needsLocalModel: boolean): string {
  return needsLocalModel
    ? 'Local mode needs an on-device chat model. Install one from Settings > Models > Discover.'
    : 'No chat model is available. Pick or enable one in Settings > Models.';
}

/** The reason a message cannot be sent with the current selection, or null
 *  when it can. Refused: Local mode with no on-device model, or a selection the
 *  loaded list does not offer. An empty list the server has not explained (not
 *  loaded yet, or the fetch failed) is not refused here: the server decides,
 *  and it refuses a turn with no runnable model with a visible error. Pure +
 *  exported for unit testing. */
export function chatModelBlockReason(
  s: Pick<SettingsState, 'models' | 'selectedModel' | 'needsLocalModel'>,
): string | null {
  if (s.models.some((m) => m.key === s.selectedModel)) return null;
  if (s.needsLocalModel || s.models.length > 0) return noChatModelHint(s.needsLocalModel);
  return null;
}

export const useSettingsStore = create<SettingsState>()((set, _get) => ({
  config: { ...defaultConfig },
  models: [],
  // Hydrate from the persisted choice so there's no flash of the wrong model
  // before loadModels resolves; loadModels then validates it against the list.
  // No hardcoded stand-in: with nothing persisted the selection is empty until
  // the list says what the active mode can run.
  selectedModel: readSelectedModel() ?? '',
  needsLocalModel: false,
  // Nothing is resident until the user loads a model (lazy in local mode).
  loadedLocalModel: null,
  dataRetentionEnabled: false,
  skills: [],
  effortLevel: DEFAULT_EFFORT,
  verbosity: readVerbosity() ?? 'medium',
  planMode: false,
  // Global memory defaults ON (matches config.example.json feature_flags.auto_memory).
  autoMemory: true,
  autoMemoryNote: null,
  localContextWindow: readLocalContextWindow(),

  loadConfig: async () => {
    try {
      const data = await get<Record<string, unknown>>('/api/config', { schema: AppConfigResponseSchema });
      const parsed = AppConfigResponseSchema.safeParse(data);
      const d = parsed.success ? parsed.data : data as Record<string, unknown>;
      const config: AppConfig = {
        chatModels: (d.chat_models as Record<string, string>) ?? {},
        effortLevel: normalizeEffort(d.effort_level as string | undefined),
        briefMode: Boolean(d.brief_mode ?? false),
        permissionMode: String(d.permission_mode ?? 'default'),
        transcriptionBackend: String(d.transcription_backend ?? 'streaming'),
        // Legacy stored modes from the earlier design map to Canary.
        translateMode: (() => {
          const raw = String(d.translate_mode ?? 'off');
          return raw === 'auto' || raw === 'model' ? 'canary' : raw;
        })(),
        translateTarget: String(d.translate_target ?? 'en'),
        modelMode: ((d.model_mode as AppConfig['modelMode']) ?? 'cloud'),
        backends: ((d.backends as AppConfig['backends']) ?? {}),
        modeNoticeSeen: Boolean(d.mode_notice_seen ?? false),
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
      await loadMemoryFlag();
    } catch (err) {
      console.warn('Failed to load config:', err);
      useUIStore.getState().addToast({
        type: 'error',
        message: 'Failed to load config',
        duration: 4000,
      });
    }
  },

  loadModels: async () => {
    try {
      const data = await get<ModelsResponse>('/api/models', { schema: ModelsResponseSchema });
      const models = data.models ?? [];
      const def = data.default ?? '';
      const needsLocalModel = !!data.needs_local_model;
      set((state) => {
        // Called again after any catalog change (config-editor save, the
        // Settings visibility toggles) so the composer picker updates live. On
        // such a REFRESH (a list is already loaded) a still-offered selection
        // stays put — even one the user never explicitly picked, which
        // localStorage doesn't know about; only a selection that vanished
        // (removed/disabled/mode-hidden) falls back, exactly like initial
        // selection does. The first load keeps the historical pick order:
        // persisted choice, then an on-device model, then the backend default.
        const isRefresh = state.models.length > 0;
        const chosen =
          isRefresh && models.some((m) => m.key === state.selectedModel)
            ? state.selectedModel
            : pickActiveModel(models, def, readSelectedModel());
        const allowed = models.find((m) => m.key === chosen)?.effort_levels ?? [];
        return {
          models,
          needsLocalModel,
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
      const data = await get<{ skills: SkillEntry[] }>('/api/skills', { schema: SkillsResponseSchema });
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

  updateConfig: (partial) => {
    set((state) => ({
      config: { ...state.config, ...partial },
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
    // Once the mode is SAVED (the backend filters on the persisted value), the
    // composer picker and the data-retention state follow it live: Local offers
    // only on-device models, and a switch to Cloud or Hybrid re-reads retention
    // (Local mode never probes AWS).
    const prev = useSettingsStore.getState().config.modelMode;
    set((s) => ({ config: { ...s.config, modelMode: mode } }));
    void put('/api/config', { model_mode: mode })
      .then(() => {
        // A mode switch changes what is visible and usable: the chat model
        // list, the data-retention gate, and whether voice mode is offered.
        const s = useSettingsStore.getState();
        void s.loadModels();
        void s.loadDataRetention();
        void loadMemoryFlag();
        refreshVoiceStatus();
      })
      .catch(() => {
        set((s) => ({ config: { ...s.config, modelMode: prev } }));
        useUIStore.getState().addToast({ type: 'error', message: 'Could not update model mode', duration: 3000 });
      });
  },

  markModeNoticeSeen: () => {
    set((s) => ({ config: { ...s.config, modeNoticeSeen: true } }));
    // Best effort: on failure the notice shows once more next launch rather
    // than surfacing an error for a purely informational dialog.
    void put('/api/config', { mode_notice_seen: true }).catch(() => {});
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

// The picker follows the connected workspace: /api/models is workspace-aware (a
// project's .whisper/settings.json can define or hide chat models), so connecting
// or disconnecting one re-reads the list. loadModels keeps a still-offered
// selection, so this never moves a valid pick.
useUIStore.subscribe((state, prev) => {
  if (state.wsPath !== prev.wsPath) void useSettingsStore.getState().loadModels();
});
