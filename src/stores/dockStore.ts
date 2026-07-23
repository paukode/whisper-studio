import { create } from 'zustand';

/**
 * dockStore — the dynamic right-side dock: a stack of typed panels (live
 * preview, plan document, workspace file, tasks) opened from the conversation.
 *
 * Panels are deduped by `id` (singletons like live/tasks use their kind; files
 * use their path; plans use `plan:<id>`). `sizes` are parallel flex-grow
 * fractions; RightDock renders each panel with `flex-grow: sizes[i]` and the
 * drag handles between panels reallocate space between neighbours.
 *
 * The live panel gets extra state: the current preview session (fed by
 * useDockLiveWatcher) and a persisted "dismissed" flag so closing the live
 * panel stays closed across a page reload (until a new session or a reopen).
 */
export type DockKind = 'live' | 'plan' | 'file' | 'tasks';

export interface DockPanel {
  id: string;
  kind: DockKind;
  title: string;
  meta?: Record<string, unknown>;
}

export interface LiveSession {
  name: string;
  url: string | null;
  port: number | null;
}

interface DockState {
  panels: DockPanel[];
  sizes: number[];
  open: boolean;
  liveSession: LiveSession | null;
  liveDismissed: string | null;
  /** URL the Live pane should show, overriding the session URL — set by
   *  clicking an http://localhost link in chat. null = follow the session. */
  liveNavUrl: string | null;
  /** Monotonic (never resets) key: bumps on each manual nav and on every
   *  active-session change. The Live panel is React-keyed by it, so a new
   *  target remounts the browser with fresh nav state — no fragile in-place
   *  merging of an external URL into the panel's local history. */
  liveNavKey: number;
  openPanel: (panel: DockPanel) => void;
  /** Open a workspace file at an optional cited line range. Dedupes by path; a
   *  re-click on an already-open file updates its line target and bumps lineRev
   *  so the viewer re-reveals and the panel flashes. */
  openFile: (t: { path: string; title: string; startLine?: number; endLine?: number }) => void;
  closePanel: (id: string) => void;
  setSizes: (sizes: number[]) => void;
  setLiveSession: (s: LiveSession | null) => void;
  openLive: () => void;
  closeLive: () => void;
  /** Open the Live pane and navigate it to a URL (localhost link routing). */
  previewUrl: (url: string) => void;
}

const DISMISS_KEY = 'whisper_live_dismissed';
function loadDismissed(): string | null {
  try { return localStorage.getItem(DISMISS_KEY); } catch { return null; }
}
function saveDismissed(v: string | null): void {
  try {
    if (v) localStorage.setItem(DISMISS_KEY, v);
    else localStorage.removeItem(DISMISS_KEY);
  } catch { /* storage unavailable */ }
}

function equalize(n: number): number[] {
  return n > 0 ? Array(n).fill(1 / n) : [];
}

function livePanel(s: LiveSession): DockPanel {
  return { id: 'live', kind: 'live', title: `Live · ${s.name}`, meta: { name: s.name, url: s.url, port: s.port } };
}

export const useDockStore = create<DockState>((set, get) => ({
  panels: [],
  sizes: [],
  open: false,
  liveSession: null,
  liveDismissed: loadDismissed(),
  liveNavUrl: null,
  liveNavKey: 0,

  openPanel: (panel) => {
    const { panels } = get();
    if (panels.some((p) => p.id === panel.id)) {
      set({ open: true });
      return;
    }
    const next = [...panels, panel];
    set({ panels: next, sizes: equalize(next.length), open: true });
  },

  openFile: (t) => {
    const id = `file:${t.path}`;
    const { panels } = get();
    const existing = panels.find((p) => p.id === id);
    // lineRev is monotonic per panel: it bumps even on a repeat click of the
    // same citation, so the viewer re-reveals the range and the panel flashes.
    const lineRev = ((existing?.meta?.lineRev as number) ?? 0) + 1;
    const meta = { path: t.path, startLine: t.startLine, endLine: t.endLine, lineRev };
    if (existing) {
      set({ panels: panels.map((p) => (p.id === id ? { ...p, meta } : p)), open: true });
      return;
    }
    const next = [...panels, { id, kind: 'file' as const, title: t.title, meta }];
    set({ panels: next, sizes: equalize(next.length), open: true });
  },

  closePanel: (id) => {
    const next = get().panels.filter((p) => p.id !== id);
    set({ panels: next, sizes: equalize(next.length), open: next.length > 0 });
  },

  setSizes: (sizes) => set({ sizes }),

  // Fed by useDockLiveWatcher with the ACTIVE (latest alive) preview session. A
  // single Live pane tracks it (Claude Code's model): when the active server
  // *changes* (a genuinely new session name), the pane switches to it — the
  // manual URL override is dropped and the nav key bumps so the panel remounts
  // onto the new server. A mere url/port refresh of the same session only
  // updates the panel meta in place (no remount, so in-panel navigation and any
  // routed URL survive). Never auto-closes: when the server stops (s === null)
  // the panel stays and shows its "stopped" state.
  setLiveSession: (s) => {
    const prevName = get().liveSession?.name ?? null;
    const newName = s?.name ?? null;
    const nameChanged = prevName !== newName;
    if (nameChanged) {
      set({ liveSession: s, liveNavUrl: null, liveNavKey: get().liveNavKey + 1 });
    } else {
      set({ liveSession: s });
    }
    if (!s) return;
    const existing = get().panels.find((p) => p.kind === 'live');
    if (existing) {
      if (existing.meta?.name !== s.name || existing.meta?.url !== s.url || existing.meta?.port !== s.port) {
        set({ panels: get().panels.map((p) => (p.kind === 'live' ? livePanel(s) : p)) });
      }
      return;
    }
    if (s.name !== get().liveDismissed) get().openPanel(livePanel(s));
  },

  // Reopen chip: clear the dismissal and open the live panel again.
  openLive: () => {
    saveDismissed(null);
    set({ liveDismissed: null });
    const s = get().liveSession;
    if (s) get().openPanel(livePanel(s));
  },

  // Clicking an http://localhost link in chat routes here: open the Live pane
  // and point its browser at the URL, instead of leaving the app / opening a
  // browser tab. Reuses the running preview's panel when one exists; otherwise
  // opens a URL-only Live panel (a server the assistant didn't register). The
  // nav-key bump remounts the panel's browser onto the URL.
  previewUrl: (url) => {
    if (!get().panels.some((p) => p.kind === 'live')) {
      const s = get().liveSession;
      const panel: DockPanel = s
        ? livePanel(s)
        : { id: 'live', kind: 'live', title: 'Live preview', meta: { name: '', url, port: null } };
      saveDismissed(null);
      set({ liveDismissed: null });
      get().openPanel(panel);
    } else {
      set({ open: true });
    }
    set({ liveNavUrl: url, liveNavKey: get().liveNavKey + 1 });
  },

  // The live panel's × / Close: remember the dismissal (persisted) so a reload
  // doesn't reopen it while the same session is still running.
  closeLive: () => {
    const name = get().liveSession?.name;
    if (name) {
      saveDismissed(name);
      set({ liveDismissed: name });
    }
    get().closePanel('live');
  },
}));
