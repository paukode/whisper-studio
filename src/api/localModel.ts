import { useUIStore } from '@/stores/uiStore';
import { useSettingsStore } from '@/stores/settingsStore';

/**
 * Load an on-device model into memory, driving the shared "loading model into
 * memory" banner (ui-store `modelLoading`) from the server's SSE progress
 * stream. Returns true when the model is resident, false on failure or cancel.
 *
 * The bar is a server-driven time ramp (the engine load is opaque) that snaps
 * to 100% on completion — same UX as the transcription engine load.
 *
 * Cancellation: the banner's Cancel posts /api/local-model/cancel-load (which
 * kills a running download job and stops a warming server) and aborts the SSE
 * fetch. Starting a load for a DIFFERENT model cancels the one in flight —
 * only one local model can be resident, so the older load is dead weight.
 *
 * Banner ownership: several streams can be alive for a moment (a superseded
 * load winding down while its replacement starts). Every banner write is
 * guarded by a generation token so only the NEWEST load/download paints it —
 * without this, a stale stream kept resurrecting its banner after the model
 * was deleted or replaced.
 */
// Coalesce concurrent identical loads. Several paths can ask for the same model
// at once (selecting a local model in the composer, the context-window slider
// committing, the data-retention consent flow), and each fetch drives its own
// load. Firing them together made the server stop-and-restart the model server
// once per request, so every spawn was killed ~1s in and none became ready. One
// in-flight promise per model+ctx means duplicates share the first load.
const inFlightLoads = new Map<string, Promise<boolean>>();

// Banner ownership token (see module docstring).
let bannerGen = 0;

// The load currently in flight, so a new load for a different model can cancel
// it instead of racing it for the single resident-model slot.
let activeLoad: { model: string; cancel: () => void } | null = null;

function cancelLoadOnServer(model: string): void {
  void fetch(`/api/local-model/cancel-load?model=${encodeURIComponent(model)}`, {
    method: 'POST',
  }).catch(() => {
    /* best-effort — aborting the stream already stops the banner */
  });
}

export function loadLocalModel(model: string, label: string, nCtx?: number): Promise<boolean> {
  const key = `${model}|${nCtx ?? ''}`;
  const existing = inFlightLoads.get(key);
  if (existing) return existing;
  // Supersede a different model's in-flight load: cancel it server-side so its
  // download/warm-up stops instead of fighting this one for residency.
  if (activeLoad && activeLoad.model !== model) activeLoad.cancel();
  const run = doLoadLocalModel(model, label, nCtx).finally(() => {
    inFlightLoads.delete(key);
  });
  inFlightLoads.set(key, run);
  return run;
}

async function doLoadLocalModel(model: string, label: string, nCtx?: number): Promise<boolean> {
  const ui = useUIStore.getState;
  const my = ++bannerGen;
  const controller = new AbortController();
  const cancel = () => {
    cancelLoadOnServer(model);
    controller.abort();
    if (my === bannerGen) ui().setModelLoading(null);
  };
  activeLoad = { model, cancel };
  const setBanner = (m: Parameters<ReturnType<typeof ui>['setModelLoading']>[0]) => {
    if (my === bannerGen) ui().setModelLoading(m ? { ...m, onCancel: cancel } : null);
  };
  setBanner({ label, progress: 0, stage: 'start' });
  let ok = true;
  let cancelled = false;
  try {
    const params = new URLSearchParams({ model });
    if (typeof nCtx === 'number') params.set('n_ctx', String(nCtx));
    const res = await fetch(`/api/local-model/load?${params.toString()}`, {
      signal: controller.signal,
    });
    if (!res.ok || !res.body) throw new Error(`load failed (${res.status})`);
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      const lines = buf.split('\n');
      buf = lines.pop() ?? '';
      for (const line of lines) {
        const t = line.trim();
        if (!t.startsWith('data:')) continue;
        const payload = t.slice(5).trim();
        if (!payload || payload === '[DONE]') continue;
        let msg: Record<string, unknown>;
        try { msg = JSON.parse(payload); } catch { continue; }
        if (msg.stage === 'cancelled') {
          ok = false;
          cancelled = true;
        } else if (msg.stage === 'error') {
          ok = false;
          ui().addToast({
            type: 'error',
            message: `Failed to load ${label}: ${String(msg.error ?? 'unknown error')}`,
            duration: 8000,
            source: 'local-model',
          });
        } else {
          setBanner({
            label: String(msg.label ?? label),
            progress: typeof msg.progress === 'number' ? (msg.progress as number) : 0,
            stage: (msg.stage as 'start' | 'downloading' | 'loading' | 'ready') ?? 'loading',
          });
        }
      }
    }
  } catch (e) {
    ok = false;
    if (controller.signal.aborted) {
      cancelled = true; // user (or a superseding load) cancelled — not an error
    } else {
      ui().addToast({ type: 'error', message: `Failed to load ${label}`, duration: 6000, source: 'local-model' });
      console.warn('loadLocalModel failed:', e);
    }
  }
  if (activeLoad?.model === model) activeLoad = null;
  // Record residency so the UI knows this model is now loaded (and a re-select
  // of the same key is a no-op instead of a redundant load).
  if (ok) useSettingsStore.getState().setLoadedLocalModel(model);
  // Clear the banner: briefly show "ready" on success, immediately otherwise.
  setTimeout(() => {
    if (my === bannerGen && useUIStore.getState().modelLoading) {
      useUIStore.getState().setModelLoading(null);
    }
  }, ok && !cancelled ? 700 : 0);
  return ok;
}

/** Is an on-device model already downloaded to disk? */
export async function localModelDownloaded(model: string): Promise<boolean> {
  try {
    const res = await fetch(`/api/local-model/status?model=${encodeURIComponent(model)}`);
    if (!res.ok) return false;
    const data = await res.json();
    return Boolean(data?.downloaded);
  } catch {
    return false;
  }
}

/**
 * Download an on-device model's weights to disk (no load into memory), driving
 * the shared banner. Returns 'ready' on success, 'cancelled' if the user hit
 * Cancel on the banner, or 'error'. The banner shows a Cancel button (wired via
 * the onCancel callback) that aborts the stream; the server fetch may finish in
 * the background, which just caches the file for next time.
 */
export async function downloadLocalModel(model: string, label: string): Promise<'ready' | 'cancelled' | 'error'> {
  const ui = useUIStore.getState;
  const my = ++bannerGen;
  const controller = new AbortController();
  const setBanner = (m: Parameters<ReturnType<typeof ui>['setModelLoading']>[0]) => {
    if (my === bannerGen) ui().setModelLoading(m);
  };
  setBanner({ label, progress: 0, stage: 'downloading', onCancel: () => controller.abort() });
  let result: 'ready' | 'cancelled' | 'error' = 'error';
  try {
    const res = await fetch(`/api/local-model/download?model=${encodeURIComponent(model)}`, { signal: controller.signal });
    if (!res.ok || !res.body) throw new Error(`download failed (${res.status})`);
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      const lines = buf.split('\n');
      buf = lines.pop() ?? '';
      for (const line of lines) {
        const t = line.trim();
        if (!t.startsWith('data:')) continue;
        const payload = t.slice(5).trim();
        if (!payload || payload === '[DONE]') continue;
        let msg: Record<string, unknown>;
        try { msg = JSON.parse(payload); } catch { continue; }
        if (msg.stage === 'error') {
          result = 'error';
          ui().addToast({ type: 'error', message: `Failed to download ${label}: ${String(msg.error ?? 'unknown error')}`, duration: 8000, source: 'model-download' });
        } else {
          if (msg.stage === 'ready') result = 'ready';
          setBanner({
            label: String(msg.label ?? label),
            progress: typeof msg.progress === 'number' ? (msg.progress as number) : 0,
            stage: (msg.stage as 'downloading' | 'ready') ?? 'downloading',
            onCancel: () => controller.abort(),
          });
        }
      }
    }
  } catch (e) {
    if (controller.signal.aborted) {
      result = 'cancelled';
    } else {
      result = 'error';
      ui().addToast({ type: 'error', message: `Failed to download ${label}`, duration: 6000, source: 'model-download' });
      console.warn('downloadLocalModel failed:', e);
    }
  }
  setTimeout(() => {
    if (my === bannerGen && useUIStore.getState().modelLoading) {
      useUIStore.getState().setModelLoading(null);
    }
  }, result === 'ready' ? 700 : 0);
  return result;
}

/** Free the resident on-device model (called when switching away from it). */
export async function unloadLocalModel(): Promise<void> {
  useSettingsStore.getState().setLoadedLocalModel(null);
  try {
    await fetch('/api/local-model/unload', { method: 'POST' });
  } catch {
    /* best-effort — the server frees it on the next load anyway */
  }
}
