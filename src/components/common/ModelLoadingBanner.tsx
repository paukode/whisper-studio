import React from 'react';
import { useUIStore } from '@/stores/uiStore';

/**
 * Local-mode banner: shows while a transcription engine (Whisper / Parakeet)
 * is loading into memory, with a progress bar that fills as the model loads.
 *
 * The load itself is opaque (MLX gives no byte-level progress), so the bar is
 * a server-driven time ramp that snaps to 100% the instant the model is
 * resident — see server/websocket.py::load_model_into_memory. Driven entirely
 * by the `modelLoading` ui-store state set from the websocket handler.
 */
export const ModelLoadingBanner: React.FC = () => {
  const modelLoading = useUIStore((s) => s.modelLoading);
  if (!modelLoading) return null;

  const { label, progress, stage, onCancel } = modelLoading;
  const pct = Math.round(Math.max(0, Math.min(1, progress)) * 100);
  const ready = stage === 'ready';
  const downloading = stage === 'downloading';
  const text = ready
    ? `${label} ready`
    : downloading
      ? `Downloading ${label} (first run, several GB)…`
      : `Loading ${label} into memory…`;

  return (
    <div className="model-loading-banner" role="status" aria-live="polite">
      <div className="model-loading-banner__row">
        <span className="model-loading-banner__spinner" aria-hidden="true" />
        <span className="model-loading-banner__label">
          {text}
        </span>
        <span className="model-loading-banner__pct">{pct}%</span>
        {onCancel && !ready && (
          <button type="button" className="model-loading-banner__cancel" onClick={onCancel}>
            Cancel
          </button>
        )}
      </div>
      <div className="model-loading-banner__track">
        <div className="model-loading-banner__fill" style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
};
