import type { ReactNode } from 'react';
import { create } from 'zustand';

export const TOAST_PRIORITY = { immediate: 0, high: 1, medium: 2, low: 3 } as const;
export type ToastPriority = (typeof TOAST_PRIORITY)[keyof typeof TOAST_PRIORITY];

const DEFAULT_TIMEOUTS: Record<ToastPriority, number> = {
  [TOAST_PRIORITY.immediate]: 6000,
  [TOAST_PRIORITY.high]: 5000,
  [TOAST_PRIORITY.medium]: 4000,
  [TOAST_PRIORITY.low]: 3000,
};

const MAX_VISIBLE_TOASTS = 5;

export interface ToolPoolStats {
  advertised: number;
  deferred: number;
  total: number;
  deferred_tokens_est: number;
}

export interface Toast {
  id: string;
  type: 'success' | 'error' | 'warning' | 'info';
  /** Optional bold header line (notify_user title). */
  title?: string;
  message: string;
  duration?: number;
  priority: ToastPriority;
  key?: string;
  count: number;
  /** Optional action link rendered as a button (opens href in a new tab) */
  action?: { label: string; href: string };
  /** Timestamp when toast was shown (for progress bar) */
  shownAt?: number;
  /** Whether toast is exiting (for animation) */
  leaving?: boolean;
}

/* ── Dialog types ── */
export interface DialogFormField {
  name: string;
  label: string;
  type?: 'text' | 'email' | 'password' | 'number' | 'checkbox' | 'select' | 'textarea';
  value?: string | boolean;
  placeholder?: string;
  required?: boolean;
  options?: Array<string | { value: string; label: string }>;
}

export interface DialogWizardStep {
  name: string;
  fields?: DialogFormField[];
  body?: string;
}

export interface DialogEntry {
  id: string;
  kind: 'confirm' | 'form' | 'wizard' | 'open';
  title?: string | false;
  message?: string;
  body?: ReactNode;
  size?: 'sm' | 'md' | 'lg';
  danger?: boolean;
  confirmText?: string;
  cancelText?: string;
  fields?: DialogFormField[];
  steps?: DialogWizardStep[];
  _resolve?: (value: unknown) => void;
}

export interface UIState {
  /** Progressive tool disclosure telemetry from the latest turn. */
  toolPoolStats: ToolPoolStats | null;
  setToolPoolStats: (stats: ToolPoolStats) => void;
  sidebarCollapsed: boolean;
  /* Per-date-window collapse state for the session list (keyed by group
   * name, e.g. "Last week"). Missing key = the group's default (expanded
   * for date windows, collapsed for Archived) — which is why this is an
   * explicit setter, not a toggle: the component knows the effective
   * state including defaults, the store only records overrides. */
  sessionGroupsCollapsed: Record<string, boolean>;
  setSessionGroupCollapsed: (bucket: string, collapsed: boolean) => void;
  settingsOpen: boolean;
  settingsTab: string;
  toasts: Toast[];
  toastQueue: Toast[];

  /* Transcript panel visibility */
  transcriptVisible: boolean;

  /* Workspace connect dialog */
  workspaceConnectOpen: boolean;

  /* Relationship-graph overlay: workspace path to graph, or null when closed */
  graphWorkspace: string | null;
  openIndexGraph: (path: string) => void;
  closeIndexGraph: () => void;

  /* Workspace connected state */
  wsConnected: boolean;
  wsPath: string;

  /* Workspace panel collapsed (still connected but hidden) */
  workspacePanelCollapsed: boolean;

  /* Memory editor */
  memoryEditorOpen: boolean;
  memoryViewerOpen: boolean;

  /* /btw popup */
  btwPopup: { question: string; answer: string } | null;

  /* Local-mode "loading model into memory" banner. null when idle. The
   * progress bar fills to `progress` (0..1); stage 'ready' triggers the
   * auto-hide. Driven by websocket model_loading/model_unloaded events. */
  modelLoading: { label: string; progress: number; stage: 'start' | 'downloading' | 'loading' | 'ready'; onCancel?: () => void } | null;

  /* Dialog stack */
  dialogStack: DialogEntry[];

  /* Command palette (Cmd/Ctrl+K) */
  commandPaletteOpen: boolean;

  // Actions
  toggleSidebar: () => void;
  openCommandPalette: () => void;
  closeCommandPalette: () => void;
  openSettings: (tab?: string) => void;
  closeSettings: () => void;
  addToast: (toast: Omit<Toast, 'id' | 'count' | 'shownAt' | 'leaving' | 'priority'> & { key?: string; priority?: ToastPriority }) => string;
  removeToast: (id: string) => void;
  dismissToast: (id: string) => void;
  clearToasts: () => void;
  _processQueue: () => void;

  showTranscript: () => void;
  toggleTranscript: () => void;

  openWorkspaceConnect: () => void;
  closeWorkspaceConnect: () => void;

  setWsConnected: (connected: boolean, path?: string) => void;

  collapseWorkspacePanel: () => void;
  expandWorkspacePanel: () => void;

  openMemoryEditor: () => void;
  closeMemoryEditor: () => void;
  openMemoryViewer: () => void;
  closeMemoryViewer: () => void;

  setBtwPopup: (popup: { question: string; answer: string } | null) => void;

  setModelLoading: (m: UIState['modelLoading']) => void;

  /* Dialog helpers */
  pushDialog: (entry: Omit<DialogEntry, 'id'>) => string;
  resolveDialog: (id: string, result: unknown) => void;
}

const SIDEBAR_COLLAPSED_KEY = 'whisper_sidebar_collapsed';

function loadSidebarCollapsed(): boolean {
  try {
    const stored = localStorage.getItem(SIDEBAR_COLLAPSED_KEY);
    if (stored !== null) return stored === 'true';
    // No saved preference yet: start collapsed on narrow (mobile) viewports,
    // where the sidebar is a drawer that would otherwise cover the app on
    // first load. 768px matches the drawer breakpoint in style.css. Expanded
    // by default on desktop.
    return typeof window !== 'undefined' && window.innerWidth <= 768;
  } catch {
    return false;
  }
}

function persistSidebarCollapsed(collapsed: boolean): void {
  try {
    localStorage.setItem(SIDEBAR_COLLAPSED_KEY, String(collapsed));
  } catch {
    // localStorage may be unavailable
  }
}

const SESSION_GROUPS_COLLAPSED_KEY = 'whisper_session_groups_collapsed';

function loadSessionGroupsCollapsed(): Record<string, boolean> {
  try {
    const stored = localStorage.getItem(SESSION_GROUPS_COLLAPSED_KEY);
    const parsed: unknown = stored ? JSON.parse(stored) : null;
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
      return parsed as Record<string, boolean>;
    }
  } catch {
    // localStorage may be unavailable or hold stale junk
  }
  return {};
}

function persistSessionGroupsCollapsed(state: Record<string, boolean>): void {
  try {
    localStorage.setItem(SESSION_GROUPS_COLLAPSED_KEY, JSON.stringify(state));
  } catch {
    // localStorage may be unavailable
  }
}

/** OS notification for high-priority toasts when tab is hidden */
function _osNotify(_type: string, message: string): void {
  if (!('Notification' in window)) return;
  if (Notification.permission === 'granted') {
    new Notification('Whisper Studio', { body: message });
  } else if (Notification.permission !== 'denied') {
    void Notification.requestPermission().then((perm) => {
      if (perm === 'granted') {
        new Notification('Whisper Studio', { body: message });
      }
    });
  }
}

export const useUIStore = create<UIState>()((set) => ({
  sidebarCollapsed: loadSidebarCollapsed(),
  sessionGroupsCollapsed: loadSessionGroupsCollapsed(),
  settingsOpen: false,
  settingsTab: 'general',
  toasts: [],
  toolPoolStats: null,
  setToolPoolStats: (stats) => set({ toolPoolStats: stats }),
  toastQueue: [],
  transcriptVisible: false,
  workspaceConnectOpen: false,
  graphWorkspace: null,
  wsConnected: false,
  wsPath: '',
  workspacePanelCollapsed: false,
  memoryEditorOpen: false,
  memoryViewerOpen: false,
  btwPopup: null,
  modelLoading: null,
  dialogStack: [],
  commandPaletteOpen: false,

  openCommandPalette: () => set({ commandPaletteOpen: true }),
  closeCommandPalette: () => set({ commandPaletteOpen: false }),

  toggleSidebar: () => {
    set((state) => {
      const next = !state.sidebarCollapsed;
      persistSidebarCollapsed(next);
      return { sidebarCollapsed: next };
    });
  },

  setSessionGroupCollapsed: (bucket: string, collapsed: boolean) => {
    set((state) => {
      const next = { ...state.sessionGroupsCollapsed, [bucket]: collapsed };
      persistSessionGroupsCollapsed(next);
      return { sessionGroupsCollapsed: next };
    });
  },

  openSettings: (tab?: string) => {
    set({
      settingsOpen: true,
      ...(tab !== undefined ? { settingsTab: tab } : {}),
    });
  },

  closeSettings: () => {
    set({ settingsOpen: false });
  },

  addToast: (toast) => {
    const id = crypto.randomUUID();
    const priority = toast.priority ?? TOAST_PRIORITY.medium;
    const duration = toast.duration ?? DEFAULT_TIMEOUTS[priority];
    const item: Toast = { ...toast, id, priority, duration, count: 1, shownAt: Date.now() };

    // OS notification for high+ priority when tab is hidden
    if (priority <= TOAST_PRIORITY.high && document.hidden) {
      _osNotify(toast.type, toast.message);
    }

    set((state) => {
      // Dedup by key: if same key already active, increment count
      if (toast.key) {
        const idx = state.toasts.findIndex((t) => t.key === toast.key && !t.leaving);
        if (idx !== -1) {
          const updated = [...state.toasts];
          updated[idx] = { ...updated[idx], count: updated[idx].count + 1, shownAt: Date.now(), duration };
          return { toasts: updated };
        }
      }

      // Immediate priority: clear all and show
      if (priority === TOAST_PRIORITY.immediate) {
        return { toasts: [item], toastQueue: [] };
      }

      // Under max visible: show directly
      if (state.toasts.filter((t) => !t.leaving).length < MAX_VISIBLE_TOASTS) {
        return { toasts: [...state.toasts, item] };
      }

      // Over max: queue with priority sort
      const newQueue = [...state.toastQueue, item].sort((a, b) => a.priority - b.priority);
      return { toastQueue: newQueue };
    });

    return id;
  },

  removeToast: (id: string) => {
    set((state) => ({
      toasts: state.toasts.filter((t) => t.id !== id),
    }));
    // Process queue after removal
    setTimeout(() => useUIStore.getState()._processQueue(), 10);
  },

  dismissToast: (id: string) => {
    // Mark as leaving for exit animation, then remove after 250ms
    set((state) => ({
      toasts: state.toasts.map((t) => (t.id === id ? { ...t, leaving: true } : t)),
    }));
    setTimeout(() => {
      useUIStore.getState().removeToast(id);
    }, 250);
  },

  clearToasts: () => {
    set({ toasts: [], toastQueue: [] });
  },

  _processQueue: () => {
    set((state) => {
      const activeCount = state.toasts.filter((t) => !t.leaving).length;
      if (activeCount >= MAX_VISIBLE_TOASTS || state.toastQueue.length === 0) return state;

      const slotsAvailable = MAX_VISIBLE_TOASTS - activeCount;
      const toPromote = state.toastQueue.slice(0, slotsAvailable).map((t) => ({ ...t, shownAt: Date.now() }));
      const remaining = state.toastQueue.slice(slotsAvailable);
      return {
        toasts: [...state.toasts, ...toPromote],
        toastQueue: remaining,
      };
    });
  },

  showTranscript: () => {
    set({ transcriptVisible: true });
  },

  toggleTranscript: () => {
    set((state) => ({ transcriptVisible: !state.transcriptVisible }));
  },

  openWorkspaceConnect: () => {
    set({ workspaceConnectOpen: true });
  },

  closeWorkspaceConnect: () => {
    set({ workspaceConnectOpen: false });
  },

  openIndexGraph: (path: string) => {
    set({ graphWorkspace: path, workspaceConnectOpen: false });
  },

  closeIndexGraph: () => {
    set({ graphWorkspace: null });
  },

  setWsConnected: (connected, path) => {
    set({ wsConnected: connected, wsPath: path ?? '' });
  },

  collapseWorkspacePanel: () => {
    set({ workspacePanelCollapsed: true });
  },

  expandWorkspacePanel: () => {
    set({ workspacePanelCollapsed: false });
  },

  openMemoryEditor: () => {
    set({ memoryEditorOpen: true });
  },

  closeMemoryEditor: () => {
    set({ memoryEditorOpen: false });
  },

  openMemoryViewer: () => {
    set({ memoryViewerOpen: true });
  },

  closeMemoryViewer: () => {
    set({ memoryViewerOpen: false });
  },

  setBtwPopup: (popup) => {
    set({ btwPopup: popup });
  },

  setModelLoading: (m) => {
    set({ modelLoading: m });
  },

  /* Dialog stack */
  pushDialog: (entry) => {
    const id = crypto.randomUUID();
    set((state) => ({
      dialogStack: [...state.dialogStack, { ...entry, id }],
    }));
    return id;
  },

  resolveDialog: (id, result) => {
    const entry = useUIStore.getState().dialogStack.find((d) => d.id === id);
    if (entry?._resolve) entry._resolve(result);
    set((state) => ({
      dialogStack: state.dialogStack.filter((d) => d.id !== id),
    }));
  },

}));

/* ── Imperative dialog helpers ── */

/** Show a confirm dialog. Resolves to `true` (confirm) or `null` (cancel). */
export function dialogConfirm(opts: {
  title?: string;
  message?: string;
  /** Rich body content; takes precedence over `message` when provided. */
  body?: ReactNode;
  size?: 'sm' | 'md' | 'lg';
  danger?: boolean;
  confirmText?: string;
  cancelText?: string;
}): Promise<boolean | null> {
  return new Promise((resolve) => {
    useUIStore.getState().pushDialog({
      kind: 'confirm',
      title: opts.title ?? 'Confirm',
      message: opts.message,
      body: opts.body,
      size: opts.size ?? 'sm',
      danger: opts.danger,
      confirmText: opts.confirmText,
      cancelText: opts.cancelText,
      _resolve: resolve as (v: unknown) => void,
    });
  });
}

/** Show a form dialog. Resolves to form data object or `null` (cancel). */
export function dialogForm(opts: {
  title?: string;
  size?: 'sm' | 'md' | 'lg';
  fields: DialogFormField[];
  submitText?: string;
  cancelText?: string;
}): Promise<Record<string, string | boolean> | null> {
  return new Promise((resolve) => {
    useUIStore.getState().pushDialog({
      kind: 'form',
      title: opts.title ?? 'Form',
      size: opts.size ?? 'sm',
      fields: opts.fields,
      confirmText: opts.submitText ?? 'Submit',
      cancelText: opts.cancelText,
      _resolve: resolve as (v: unknown) => void,
    });
  });
}

/** Show a multi-step wizard. Resolves to accumulated data or `null` (cancel). */
export function dialogWizard(opts: {
  title?: string;
  size?: 'sm' | 'md' | 'lg';
  steps: DialogWizardStep[];
}): Promise<Record<string, unknown> | null> {
  return new Promise((resolve) => {
    useUIStore.getState().pushDialog({
      kind: 'wizard',
      title: opts.title ?? 'Setup',
      size: opts.size ?? 'md',
      steps: opts.steps,
      _resolve: resolve as (v: unknown) => void,
    });
  });
}
