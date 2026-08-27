/**
 * Pre-recording model gate.
 *
 * A recording session needs the active ASR engine's weights (Parakeet for the
 * default 'streaming' backend, Whisper for the batch one) plus the ECAPA
 * speaker encoder for diarization. When any of those are not on disk yet, the
 * websocket's lazy ensure_* path would block silently while the server
 * downloads gigabytes. This gate runs BEFORE the mic and websocket open:
 *
 *  - installed already: one cheap GET /api/models/status and the caller
 *    proceeds — the only cost on the happy path;
 *  - missing: start the downloads through the model download manager
 *    (/api/models/{key}/download — the same worker-subprocess machinery as
 *    Settings > Models), drive the shared model-loading banner (the exact
 *    component the local chat model flow uses) from a 1s status poll, and
 *    resolve 'ready' so recording continues without a second click.
 *
 * Cancel on the banner cancels the downloads server-side (partial files stay
 * for resume). The server-side blocking download remains as a fallback — this
 * gate just makes it rare.
 */
import {
  cancelModelDownload,
  fetchModelsCatalog,
  fetchModelsStatus,
  startModelDownload,
} from '@/api/models';
import type { ManagedModelStatus } from '@/api/models';
import { useSettingsStore } from '@/stores/settingsStore';
import { useUIStore } from '@/stores/uiStore';
import { ApiError } from '@/types/api';
import { humanSize } from '@/utils/humanSize';

export type EnsureModelsResult = 'ready' | 'cancelled' | 'error';

const POLL_MS = 1000;
/** Consecutive non-409 failures to (re)start one model's download before the
 *  gate gives up. 409s never count: they mean "already installed", which the
 *  poll resolves naturally. (Capacity is no longer a 409 — the server queues
 *  the download and reports state 'queued' instead.) */
const MAX_FAILED_STARTS = 5;
/** How long the error banner stays up before auto-hiding. */
const ERROR_BANNER_MS = 8000;
/** Brief "ready" beat before the banner hides, mirroring the local chat flow. */
const READY_BANNER_MS = 700;

/** Catalog keys a recording session needs: the active engine's weights plus
 *  the speaker encoder (diarization always runs). The active engine comes
 *  from the settings store — hydrated from /api/config and kept current by
 *  the transcript panel's live engine switch. */
export function requiredRecordingModelKeys(): string[] {
  const { transcriptionBackend } = useSettingsStore.getState().config;
  const engineKey =
    transcriptionBackend === 'whisper'
      ? 'whisper_large_v3'
      : transcriptionBackend === 'canary'
        ? 'canary'
        : 'parakeet';
  // Canary needs its language-ID companion (per-sentence source detection).
  if (engineKey === 'canary') return ['canary', 'voxlingua_lid', 'ecapa_speaker'];
  return [engineKey, 'ecapa_speaker'];
}

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

const isConflict = (e: unknown): boolean => e instanceof ApiError && e.status === 409;

type StatusMap = Record<string, ManagedModelStatus>;

/**
 * Make sure the models recording needs are on disk, downloading them with a
 * progress banner when they aren't. Resolves 'ready' when recording can
 * proceed, 'cancelled' when the user (here or in Settings > Models) called
 * the downloads off, 'error' when a download failed (shown on the banner).
 */
export async function ensureRecordingModels(): Promise<EnsureModelsResult> {
  const keys = requiredRecordingModelKeys();

  let status: StatusMap;
  try {
    status = (await fetchModelsStatus()).models;
  } catch {
    // Status endpoint unreachable — don't block the mic on it; the
    // websocket's blocking ensure_* fallback still guarantees the models.
    return 'ready';
  }

  // Keys the manager doesn't know are left to the server fallback too.
  const missing = keys.filter((k) => status[k] && status[k].state !== 'installed');
  if (missing.length === 0) return 'ready';

  // Human labels for the banner (best-effort — raw keys still work).
  const labels = new Map<string, string>();
  try {
    for (const m of (await fetchModelsCatalog()).models) labels.set(m.key, m.label);
  } catch {
    /* banner falls back to the raw keys */
  }
  const labelOf = (key: string) => labels.get(key) ?? key;

  const ui = useUIStore.getState;
  const total = missing.length;
  let remaining = [...missing];
  /** Keys whose download we know is in flight — running OR queued (started by
   *  us, or observed). If such a key later reads 'absent' with no error, it
   *  was cancelled/dequeued externally (Settings > Models) — the gate stands
   *  down instead of restarting it against the user's wishes. */
  const sawDownloading = new Set<string>();
  const failedStarts = new Map<string, number>();
  let cancelRequested = false;

  const cancelAll = () =>
    Promise.all(
      remaining.map((key) =>
        cancelModelDownload(key).catch(() => {
          /* not downloading (finished or never started) — nothing to cancel */
        }),
      ),
    );

  const onCancel = () => {
    if (cancelRequested) return;
    cancelRequested = true;
    ui().setModelLoading(null);
    void cancelAll();
  };

  const updateBanner = (st: StatusMap) => {
    const key = remaining[0];
    const s = st[key];
    if (s?.state === 'queued') {
      // Waiting for a download slot (the server runs at most two at a time
      // and queues the rest) — no bytes are moving yet.
      ui().setModelLoading({
        label: labelOf(key),
        progress: 0,
        stage: 'downloading',
        detail: 'Waiting for other downloads to finish…',
        onCancel,
      });
      return;
    }
    const bytes = s?.bytes_on_disk ?? 0;
    const est = s?.size_bytes_estimate ?? 0;
    // Server-computed fraction — the same number Settings > Models and the
    // local-chat banner show. Never recomputed here from bytes/estimate.
    const progress = s?.progress ?? 0;
    const done = bytes > 0 ? humanSize(bytes) : '0 MB';
    const size = est > 0 ? `${done} of ${humanSize(est)}` : done;
    const position = total > 1 ? ` · model ${total - remaining.length + 1} of ${total}` : '';
    ui().setModelLoading({
      label: labelOf(key),
      progress,
      stage: 'downloading',
      detail: `${size}${position}`,
      onCancel,
    });
  };

  const failGate = (key: string, message: string | null) => {
    ui().setModelLoading({
      label: labelOf(key),
      progress: 0,
      stage: 'error',
      detail: message ?? 'The download failed. See Settings > Models for details.',
      // The banner renders the cancel slot as Dismiss on errors.
      onCancel: () => ui().setModelLoading(null),
    });
    setTimeout(() => {
      const cur = useUIStore.getState().modelLoading;
      if (cur && cur.stage === 'error') useUIStore.getState().setModelLoading(null);
    }, ERROR_BANNER_MS);
  };

  const tryStart = async (key: string): Promise<'ok' | 'wait' | 'fail'> => {
    try {
      await startModelDownload(key);
      sawDownloading.add(key);
      failedStarts.delete(key);
      return 'ok';
    } catch (e) {
      // 409 is benign: it now only means "already installed" (duplicate
      // starts and full slots return 200) — the poll below resolves it.
      if (isConflict(e)) return 'wait';
      const n = (failedStarts.get(key) ?? 0) + 1;
      failedStarts.set(key, n);
      if (n >= MAX_FAILED_STARTS) {
        failGate(key, e instanceof Error ? e.message : null);
        return 'fail';
      }
      return 'wait';
    }
  };

  // Kick off every missing download (at most two run server-side; the rest
  // enter the server's FIFO queue and start as slots free up).
  for (const key of missing) {
    if (status[key].state === 'downloading' || status[key].state === 'queued') {
      sawDownloading.add(key); // already in flight (e.g. via Settings > Models)
      continue;
    }
    if ((await tryStart(key)) === 'fail') return 'error';
  }
  updateBanner(status);

  for (;;) {
    await sleep(POLL_MS);
    if (cancelRequested) return 'cancelled';

    let st: StatusMap;
    try {
      st = (await fetchModelsStatus()).models;
    } catch {
      continue; // transient poll failure — keep the last banner frame
    }
    if (cancelRequested) return 'cancelled';

    for (const key of [...remaining]) {
      const s = st[key];
      if (!s || s.state === 'installed') {
        remaining = remaining.filter((k) => k !== key);
        continue;
      }
      if (s.state === 'error') {
        // A sibling download (if any) keeps running: it just caches files
        // for the retry, like a background download from Settings > Models.
        failGate(key, s.error);
        return 'error';
      }
      if (s.state === 'downloading' || s.state === 'queued') {
        // Queued counts as in-flight: the server's poll-driven pump starts it
        // when a slot frees, so there is nothing for the gate to do but wait.
        sawDownloading.add(key);
        continue;
      }
      // state === 'absent':
      if (sawDownloading.has(key)) {
        // Was downloading or queued, now gone with no error recorded:
        // cancelled from Settings > Models. Respect it and stand down entirely.
        onCancel();
        return 'cancelled';
      }
      // The start request never landed (e.g. transient network error) — try
      // again now; the server will run it or queue it.
      if ((await tryStart(key)) === 'fail') return 'error';
    }

    if (remaining.length === 0) {
      ui().setModelLoading({
        label: total === 1 ? labelOf(missing[0]) : 'Speech models',
        progress: 1,
        stage: 'ready',
      });
      setTimeout(() => {
        const cur = useUIStore.getState().modelLoading;
        if (cur && cur.stage === 'ready') useUIStore.getState().setModelLoading(null);
      }, READY_BANNER_MS);
      return 'ready';
    }
    updateBanner(st);
  }
}
