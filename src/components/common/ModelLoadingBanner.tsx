import React from 'react';
import { useUIStore } from '@/stores/uiStore';

/**
 * Shared model banner: shows while a transcription engine (Whisper / Parakeet)
 * loads into memory, while an on-device chat model downloads, or while the
 * pre-recording gate downloads the speech models a recording needs.
 *
 * A memory load is opaque (MLX gives no byte-level progress), so its bar is a
 * server-driven time ramp that snaps to 100% the instant the model is resident
 * — see server/websocket.py::load_model_into_memory. Downloads have real byte
 * progress, passed via `progress` plus a human-readable `detail` clause
 * ("1.1 GB of 2.5 GB · model 1 of 2"). Stage 'error' renders the failure with
 * the Cancel slot doubling as Dismiss. Driven entirely by the `modelLoading`
 * ui-store state.
 */
export const ModelLoadingBanner: React.FC = () => {
  const modelLoading = useUIStore((s) => s.modelLoading);
  if (!modelLoading) return null;

  const { label, progress, stage, detail, onCancel } = modelLoading;
  const pct = Math.round(Math.max(0, Math.min(1, progress)) * 100);
  const ready = stage === 'ready';
  const failed = stage === 'error';
  const downloading = stage === 'downloading';
  const text = failed
    ? `Download failed: ${label}`
    : ready
      ? `${label} ready`
      : downloading
        ? detail
          ? `Downloading ${label} · ${detail}`
          : `Downloading ${label} (first run, several GB)…`
        : `Loading ${label} into memory…`;

  return (
    <div
      className={`model-loading-banner${failed ? ' model-loading-banner--error' : ''}`}
      role={failed ? 'alert' : 'status'}
      aria-live="polite"
    >
      <div className="model-loading-banner__row">
        {!failed && <span className="model-loading-banner__spinner" aria-hidden="true" />}
        <span className="model-loading-banner__label">
          {text}
        </span>
        {!failed && <span className="model-loading-banner__pct">{pct}%</span>}
        {onCancel && !ready && (
          <button type="button" className="model-loading-banner__cancel" onClick={onCancel}>
            {failed ? 'Dismiss' : 'Cancel'}
          </button>
        )}
      </div>
      {failed ? (
        detail ? <div className="model-loading-banner__detail">{detail}</div> : null
      ) : (
        <div className="model-loading-banner__track">
          <div className="model-loading-banner__fill" style={{ width: `${pct}%` }} />
        </div>
      )}
    </div>
  );
};
