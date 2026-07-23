import React, { useRef, useState } from 'react';
import { useSettingsStore } from '@/stores/settingsStore';
import { useUIStore, dialogConfirm } from '@/stores/uiStore';
import { useDismiss } from '@/hooks/useDismiss';
import { loadLocalModel } from '@/api/localModel';

interface LocalContextWindowSliderProps {
  /** Called when the popover opens so the parent can close its other toolbar
   *  dropdowns (single-open behaviour), matching the EffortPicker. */
  onOpen?: () => void;
}

// Context-window sizes (tokens), 16K up to Gemma's 256K maximum. 16K is the
// default and the floor: with tools on, the tool-pool prompt alone is ~12K
// tokens, so a smaller window overflows. Anything above 16K is allowed but
// requires confirmation, because the KV cache grows fast with context length.
const MARKS = [16384, 32768, 65536, 131072, 262144];
const fmt = (n: number) => `${Math.round(n / 1024)}K`;

// Threshold above which we prompt for confirmation before reloading.
const CONFIRM_ABOVE = 16384;

// Recommended TOTAL system memory per context window. Approximate ranges
// (model weights ~7 GB + KV cache, which Gemma's sliding-window attention
// shrinks): a guideline, not your specific machine. Lower bound assumes the
// efficient case; upper bound is conservative headroom.
const RECOMMENDED_MEMORY: Record<number, string> = {
  16384: '12 to 16 GB',
  32768: '16 to 24 GB',
  65536: '24 to 40 GB',
  131072: '40 to 64 GB',
  262144: '64 to 128 GB',
};

const nearestMarkIndex = (value: number): number =>
  MARKS.reduce(
    (best, m, i) => (Math.abs(m - value) < Math.abs(MARKS[best] - value) ? i : best),
    0,
  );

const recMem = (ctx: number): string =>
  RECOMMENDED_MEMORY[ctx] ?? RECOMMENDED_MEMORY[MARKS[nearestMarkIndex(ctx)]];

/** Note shown under the slider. Above 16K it becomes a memory warning — the
 *  user keeps control, but the recommended memory is spelled out (no
 *  machine-specific reference). */
function memoryNote(ctx: number): { text: string; warn: boolean } {
  const rec = recMem(ctx);
  if (ctx <= CONFIRM_ABOVE) {
    return {
      text: `Recommended memory: ${rec}. Changing this reloads the model.`,
      warn: false,
    };
  }
  return {
    text: `⚠ Recommended memory: ${rec}. If your system has less, the model may fail to load or become unstable. Changing this reloads the model.`,
    warn: true,
  };
}

/** Rich body for the above-16K confirmation dialog: the cost of the chosen size
 *  plus the full recommended-memory table (chosen row highlighted). */
const ContextWarningBody: React.FC<{ ctx: number }> = ({ ctx }) => (
  <div className="ctx-warn-body">
    <p>
      Loading Gemma at <strong>{fmt(ctx)}</strong> tokens needs roughly{' '}
      <strong>{recMem(ctx)}</strong> of total system memory.
    </p>
    <p>
      Your system may not support this context window: if there is not enough
      memory, the model can fail to load, run very slowly, or the app or your
      system may become unstable or shut down. Recommended memory per size:
    </p>
    <table className="ctx-mem-table">
      <thead>
        <tr>
          <th>Context</th>
          <th>Recommended memory</th>
        </tr>
      </thead>
      <tbody>
        {MARKS.map((m) => (
          <tr key={m} className={m === ctx ? 'active' : ''}>
            <td>{fmt(m)}</td>
            <td>{recMem(m)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  </div>
);

/**
 * Toolbar control (on-device models only) to set the local model's context
 * window. Mirrors the EffortPicker: a badge that opens a slider popover. Because
 * llama.cpp fixes n_ctx at load time, committing a different size RELOADS the
 * model at that size behind the shared "loading model" banner. Above 16K, a
 * confirmation dialog (with the memory requirements) gates the reload. Hidden
 * for cloud models. The chosen value is persisted (localStorage) by the store.
 */
export const LocalContextWindowSlider: React.FC<LocalContextWindowSliderProps> = ({ onOpen }) => {
  const model = useSettingsStore((s) => s.models.find((m) => m.key === s.selectedModel));
  const ctx = useSettingsStore((s) => s.localContextWindow);
  const setCtx = useSettingsStore((s) => s.setLocalContextWindow);
  const modelLoading = useUIStore((s) => s.modelLoading);
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  // The size the resident model was last (re)loaded at. We reload only when the
  // committed value actually differs, so opening and closing without a change
  // never reloads.
  const appliedRef = useRef(ctx);

  // Commit on close: if the size changed, reload the model at the new size.
  // Above 16K, confirm first (memory requirements); cancel reverts the slider.
  // Reads live store values so a stale closure can't reload the wrong size.
  const commit = async () => {
    setOpen(false);
    const st = useSettingsStore.getState();
    const m = st.models.find((x) => x.key === st.selectedModel);
    const liveCtx = st.localContextWindow;
    if (!m?.is_local || liveCtx === appliedRef.current) return;

    if (liveCtx > CONFIRM_ABOVE) {
      const ok = await dialogConfirm({
        danger: true,
        size: 'md',
        title: 'Large context window',
        body: <ContextWarningBody ctx={liveCtx} />,
        confirmText: `Load at ${fmt(liveCtx)}`,
        cancelText: 'Cancel',
      });
      if (!ok) {
        st.setLocalContextWindow(appliedRef.current); // revert; no reload
        return;
      }
    }

    appliedRef.current = liveCtx;
    void loadLocalModel(m.key, m.name, liveCtx);
  };

  useDismiss(ref, () => void commit(), { enabled: open });

  if (!model?.is_local) return null;

  const idx = nearestMarkIndex(ctx);
  const busy = modelLoading !== null;
  const note = memoryNote(ctx);

  return (
    <div className="toolbar-dropdown-wrap" ref={ref}>
      <button
        type="button"
        className={`ctx-badge${note.warn ? ' ctx-badge-warn' : ''}`}
        id="ctxBadge"
        title={`Context window: ${fmt(ctx)} tokens — click to change (reloads the model)`}
        onClick={() => {
          if (open) {
            void commit(); // closing via the badge commits too
          } else {
            onOpen?.();
            setOpen(true);
          }
        }}
      >
        {fmt(ctx)} ctx
      </button>
      <div className="toolbar-dropdown effort-pop ctx-pop" style={{ display: open ? 'block' : 'none' }}>
        <div className="effort-pop-row">
          <span className="effort-pop-label">Context window</span>
          <span className="effort-pop-val">{fmt(ctx)} tokens</span>
        </div>
        <input
          type="range"
          className="effort-slider"
          min={0}
          max={MARKS.length - 1}
          step={1}
          value={idx}
          disabled={busy}
          onChange={(e) => setCtx(MARKS[Number(e.target.value)])}
          aria-label="Context window size"
        />
        <div className="effort-pop-ticks">
          {MARKS.map((m, i) => (
            <button
              type="button"
              key={m}
              className={`effort-pop-tick${i === idx ? ' active' : ''}`}
              disabled={busy}
              onClick={() => setCtx(m)}
            >
              {fmt(m)}
            </button>
          ))}
        </div>
        <div className={`effort-pop-note${note.warn ? ' ctx-pop-warn' : ''}`}>
          {busy ? 'Reloading the model at the new size…' : note.text}
        </div>
      </div>
    </div>
  );
};
