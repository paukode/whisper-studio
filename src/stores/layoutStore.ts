import { create } from 'zustand';

/**
 * layoutStore — single source of truth for pane split ratios.
 *
 * Splits are expressed as fractions (0..1) of the parent's available space.
 * The Splitter component renders these as `flex: ${grow} 1 0`, so the browser
 * distributes available space by ratio independently of:
 *   - container size (window resizes are free)
 *   - sibling presence (when a pane is added/removed the surviving panes
 *     automatically reabsorb its share via flexbox redistribution)
 *
 * No component should ever write to element.style.flex directly. Drag handlers
 * update fractions here; React re-renders the Splitter with new grow values.
 */

const STORAGE_KEY = 'whisper_studio_layout';

export interface LayoutState {
  /** Fraction of the .panels row taken by the workspace pane. Right column = 1 - this. */
  workspaceFrac: number;
  /** Fraction of the top-row taken by transcript. Chat = 1 - this. */
  transcriptFrac: number;
  /** Fraction of the .panels row taken by the right-side dock. App content = 1 - this. */
  dockFrac: number;
  setWorkspaceFrac: (f: number) => void;
  setTranscriptFrac: (f: number) => void;
  setDockFrac: (f: number) => void;
}

const DEFAULTS = {
  workspaceFrac: 0.35,
  transcriptFrac: 0.4,
  dockFrac: 0.42,
} as const;

const MIN_FRAC = 0.1;
const MAX_FRAC = 0.9;

function clamp(value: number): number {
  if (!Number.isFinite(value)) return 0.5;
  return Math.max(MIN_FRAC, Math.min(MAX_FRAC, value));
}

type Fracs = { workspaceFrac: number; transcriptFrac: number; dockFrac: number };

function load(): Fracs {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return { ...DEFAULTS };
    const parsed = JSON.parse(raw) as Partial<typeof DEFAULTS>;
    return {
      workspaceFrac: clamp(parsed.workspaceFrac ?? DEFAULTS.workspaceFrac),
      transcriptFrac: clamp(parsed.transcriptFrac ?? DEFAULTS.transcriptFrac),
      dockFrac: clamp(parsed.dockFrac ?? DEFAULTS.dockFrac),
    };
  } catch {
    return { ...DEFAULTS };
  }
}

function persist(state: Fracs): void {
  try {
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({
        workspaceFrac: state.workspaceFrac,
        transcriptFrac: state.transcriptFrac,
        dockFrac: state.dockFrac,
      }),
    );
  } catch {
    // localStorage may be unavailable
  }
}

export const useLayoutStore = create<LayoutState>((set, get) => ({
  ...load(),
  setWorkspaceFrac: (f) => {
    const next = clamp(f);
    set({ workspaceFrac: next });
    persist({ workspaceFrac: next, transcriptFrac: get().transcriptFrac, dockFrac: get().dockFrac });
  },
  setTranscriptFrac: (f) => {
    const next = clamp(f);
    set({ transcriptFrac: next });
    persist({ workspaceFrac: get().workspaceFrac, transcriptFrac: next, dockFrac: get().dockFrac });
  },
  setDockFrac: (f) => {
    const next = clamp(f);
    set({ dockFrac: next });
    persist({ workspaceFrac: get().workspaceFrac, transcriptFrac: get().transcriptFrac, dockFrac: next });
  },
}));
