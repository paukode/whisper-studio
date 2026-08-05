/**
 * Native-shell → page toast bridge.
 *
 * The macOS shell (macapp/shell/DownloadHandler.swift) reports download
 * completion or failure by calling `window.__whisperShellToast(message, opts)`
 * via evaluateJavaScript. Registering the hook here — imported once from
 * main.tsx — keeps the shell decoupled from store internals: the shell only
 * needs one stable global with a string-and-plain-object signature.
 *
 * Toasts raised through the bridge persist to the notification bell under
 * source "export" (addToast's default persist path — no `persist: false`),
 * so "Saved to Downloads: …" survives the toast's auto-dismiss. Repeated
 * identical messages merge client-side via the toast `key` and server-side
 * via the notification log's burst dedupe.
 *
 * In a plain browser the hook is registered but never called — real browsers
 * implement anchor downloads natively and show their own download UI.
 */
import { useUIStore, type Toast } from '@/stores/uiStore';

export interface ShellToastOptions {
  type?: Toast['type'];
  title?: string;
}

declare global {
  interface Window {
    __whisperShellToast?: (message: string, opts?: ShellToastOptions) => void;
  }
}

const TOAST_TYPES: ReadonlyArray<Toast['type']> = ['success', 'error', 'warning', 'info'];

export function registerShellToastBridge(): void {
  window.__whisperShellToast = (message, opts) => {
    // The caller is native code building JS by hand — validate defensively
    // so a malformed call can never throw inside evaluateJavaScript.
    const text = typeof message === 'string' ? message : String(message ?? '');
    if (!text) return;
    const type: Toast['type'] =
      opts && TOAST_TYPES.includes(opts.type as Toast['type'])
        ? (opts.type as Toast['type'])
        : 'success';
    useUIStore.getState().addToast({
      type,
      message: text,
      title: typeof opts?.title === 'string' && opts.title ? opts.title : undefined,
      source: 'export',
      key: `shell-export:${text}`,
    });
  };
}
