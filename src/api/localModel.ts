import { useUIStore } from '@/stores/uiStore';
import { useSettingsStore } from '@/stores/settingsStore';

/**
 * Load an on-device model into memory, driving the shared "loading model into
 * memory" banner (ui-store `modelLoading`) from the server's SSE progress
 * stream. Returns true when the model is resident, false on failure.
 *
 * The bar is a server-driven time ramp (llama.cpp load is opaque) that snaps to
 * 100% on completion — same UX as the transcription engine load.
 */
export async function loadLocalModel(model: string, label: string, nCtx?: number): Promise<boolean> {
  const ui = useUIStore.getState;
  ui().setModelLoading({ label, progress: 0, stage: 'start' });
  let ok = true;
  try {
    const params = new URLSearchParams({ model });
    if (typeof nCtx === 'number') params.set('n_ctx', String(nCtx));
    const res = await fetch(`/api/local-model/load?${params.toString()}`);
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
        if (msg.stage === 'error') {
          ok = false;
          ui().addToast({
            type: 'error',
            message: `Failed to load ${label}: ${String(msg.error ?? 'unknown error')}`,
            duration: 8000,
          });
        } else {
          ui().setModelLoading({
            label: String(msg.label ?? label),
            progress: typeof msg.progress === 'number' ? (msg.progress as number) : 0,
            stage: (msg.stage as 'start' | 'downloading' | 'loading' | 'ready') ?? 'loading',
          });
        }
      }
    }
  } catch (e) {
    ok = false;
    ui().addToast({ type: 'error', message: `Failed to load ${label}`, duration: 6000 });
    console.warn('loadLocalModel failed:', e);
  }
  // Record residency so the UI knows this model is now loaded (and a re-select
  // of the same key is a no-op instead of a redundant load).
  if (ok) useSettingsStore.getState().setLoadedLocalModel(model);
  // Clear the banner: briefly show "ready" on success, immediately on failure.
  setTimeout(() => {
    const cur = useUIStore.getState().modelLoading;
    if (cur) useUIStore.getState().setModelLoading(null);
  }, ok ? 700 : 0);
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
  const controller = new AbortController();
  ui().setModelLoading({ label, progress: 0, stage: 'downloading', onCancel: () => controller.abort() });
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
          ui().addToast({ type: 'error', message: `Failed to download ${label}: ${String(msg.error ?? 'unknown error')}`, duration: 8000 });
        } else {
          if (msg.stage === 'ready') result = 'ready';
          ui().setModelLoading({
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
      ui().addToast({ type: 'error', message: `Failed to download ${label}`, duration: 6000 });
      console.warn('downloadLocalModel failed:', e);
    }
  }
  setTimeout(() => {
    if (useUIStore.getState().modelLoading) useUIStore.getState().setModelLoading(null);
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
