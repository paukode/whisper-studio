import { useCallback, useEffect, useRef, useState } from 'react';
import { createTerminalSocket } from '@/api/terminal';

/**
 * xterm.js + PTY WebSocket lifecycle.
 *
 * Sizing policy: deliberate, discrete resizes only — no ResizeObserver.
 * Earlier revisions refitted on every ResizeObserver tick and hit a
 * recurring artifact: growing an xterm whose scrollback held blank
 * padding from an earlier shrink pulled those blank rows back into the
 * active area, leaving a wall of black above the prompt. The rules that
 * keep that from recurring:
 *
 *   1. The PTY is spawned from a DOM-probe estimate of the container
 *      (measureCellGrid) and xterm is constructed with those exact
 *      dims — no mount-time shrink.
 *   2. One fit() runs right after open(), while the buffer is still
 *      empty — an empty-buffer resize has no scrollback to reshuffle.
 *   3. Every later resize is a one-shot, event-driven refit() (header
 *      button, tab activation, panel-drag end, window-resize settle)
 *      that no-ops when dims are unchanged and scrolls to the bottom
 *      after applying so the prompt stays in view.
 *   4. The PTY resize rides terminal.onResize, so the shell answers
 *      exactly one SIGWINCH per client resize.
 */

export interface UseTerminalOptions {
  sessionId: string;
  /** Initial cols/rows derived from the container at session-create time. */
  initialCols: number;
  initialRows: number;
  onData?: (data: string) => void;
  onDisconnect?: () => void;
  onReconnect?: () => void;
}

export interface UseTerminalReturn {
  terminalRef: (el: HTMLDivElement | null) => void;
  isConnected: boolean;
  hasConnected: boolean;
  sendInput: (data: string) => void;
  /** Deliberate one-shot refit: measures the container and resizes xterm + PTY. */
  refit: () => void;
}

interface FitAddonLike {
  fit: () => void;
  proposeDimensions: () => { cols: number; rows: number } | undefined;
}

/**
 * Estimate cols/rows for the PTY spawn by measuring the real rendered
 * font. This is only a seed — the authoritative grid comes from
 * fitAddon.fit() right after open(), which uses xterm's own measured
 * cell size. The probe's `line-height: normal` box is slightly taller
 * than xterm's cell, so the rows estimate errs low and the post-open
 * fit only ever grows an empty buffer (which cannot shuffle scrollback).
 */
export function measureCellGrid(el: HTMLElement | null): { cols: number; rows: number } {
  if (!el) return { cols: 80, rows: 24 };
  const probe = document.createElement('span');
  probe.style.cssText =
    'position:absolute;top:-9999px;visibility:hidden;white-space:pre;font-size:14px;line-height:normal;';
  probe.style.fontFamily =
    getComputedStyle(document.documentElement).getPropertyValue('--font-mono').trim() ||
    'Menlo, Monaco, "Courier New", monospace';
  probe.textContent = 'W'.repeat(32);
  el.appendChild(probe);
  const rect = probe.getBoundingClientRect();
  el.removeChild(probe);
  const cellW = rect.width / 32 || 8.4;
  const cellH = rect.height || 17;
  return {
    cols: Math.max(20, Math.floor(el.clientWidth / cellW)),
    rows: Math.max(5, Math.floor(el.clientHeight / cellH)),
  };
}

/** xterm theme derived from the app's CSS tokens, read live so it follows the
 *  active theme. Re-applied by a MutationObserver on theme switch. The visible
 *  background is additionally pinned in terminal.css (xterm v6 ignores
 *  theme.background for the DOM viewport), so this mainly drives text/cursor. */
function readXtermTheme(): {
  background: string;
  foreground: string;
  cursor: string;
  cursorAccent: string;
  selectionBackground: string;
} {
  const tokens = getComputedStyle(document.documentElement);
  const t = (name: string, fallback: string) => tokens.getPropertyValue(name).trim() || fallback;
  return {
    background: t('--bg-inset', '#0d0d0f'),
    foreground: t('--text-primary', '#e4e2de'),
    cursor: t('--accent', '#e2a336'),
    cursorAccent: t('--bg-inset', '#0d0d0f'),
    selectionBackground: t('--accent-dim', 'rgba(226,163,54,0.15)'),
  };
}

export function useTerminal(options: UseTerminalOptions): UseTerminalReturn {
  const { sessionId, initialCols, initialRows, onData, onDisconnect, onReconnect } = options;

  const [isConnected, setIsConnected] = useState(false);
  const [hasConnected, setHasConnected] = useState(false);

  const terminalInstanceRef = useRef<unknown>(null);
  const fitAddonRef = useRef<FitAddonLike | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const themeObserverRef = useRef<MutationObserver | null>(null);
  const mountedRef = useRef(true);
  // Synchronous re-entry guard. React 19 StrictMode in dev calls the ref
  // callback as ref(div), ref(null), ref(div) on initial mount — without
  // this flag, two parallel async xterm initialisations spawn (visible in
  // the server logs as two WebSocket connections per session) and the
  // resulting xterm receives only half the PTY bytes, leaving the panel
  // black. Held for the whole async setup; cleared in the finally so a
  // genuine remount can retry.
  const initializingRef = useRef(false);

  const onDataRef = useRef(onData);
  const onDisconnectRef = useRef(onDisconnect);
  const onReconnectRef = useRef(onReconnect);
  // Keep the latest callbacks in refs without re-subscribing the socket.
  // Assign in an effect (not during render) per the React Compiler rules.
  useEffect(() => {
    onDataRef.current = onData;
    onDisconnectRef.current = onDisconnect;
    onReconnectRef.current = onReconnect;
  }, [onData, onDisconnect, onReconnect]);

  const sendInput = useCallback((data: string) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(data);
    }
  }, []);

  const sendResize = useCallback((cols: number, rows: number) => {
    // Non-integer dims crash the server's struct.pack and kill the socket.
    if (!Number.isInteger(cols) || !Number.isInteger(rows)) return;
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      // The server routes resize frames by sniffing the literal prefix
      // {"type":"resize" — "type" must serialize first.
      wsRef.current.send(JSON.stringify({ type: 'resize', cols, rows }));
    }
  }, []);

  const refit = useCallback(() => {
    // Synchronous on purpose: every caller runs after React has committed
    // the layout change (effects, mouseup, debounced resize), and reading
    // dimensions forces a reflow. An rAF here would defer the refit
    // indefinitely in a hidden/throttled window.
    const term = terminalInstanceRef.current as {
      cols: number;
      rows: number;
      scrollToBottom: () => void;
    } | null;
    const fit = fitAddonRef.current;
    const el = containerRef.current;
    // display:none tabs are unmeasurable; they catch up on activation.
    if (!term || !fit || !el || el.offsetParent === null) return;
    const dims = fit.proposeDimensions();
    if (!dims || !Number.isInteger(dims.cols) || !Number.isInteger(dims.rows)) return;
    if (dims.cols === term.cols && dims.rows === term.rows) return;
    fit.fit();
    // Growing rows pulls scrollback into the viewport; keep the prompt visible.
    term.scrollToBottom();
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      if (themeObserverRef.current) {
        themeObserverRef.current.disconnect();
        themeObserverRef.current = null;
      }
      if (wsRef.current) {
        wsRef.current.onopen = null;
        wsRef.current.onclose = null;
        wsRef.current.onmessage = null;
        wsRef.current.onerror = null;
        try {
          wsRef.current.close();
        } catch {
          /* already closed */
        }
        wsRef.current = null;
      }
      const term = terminalInstanceRef.current as { dispose?: () => void } | null;
      if (term?.dispose) {
        term.dispose();
      }
      terminalInstanceRef.current = null;
      fitAddonRef.current = null;
    };
  }, []);

  const terminalRef = useCallback(
    (el: HTMLDivElement | null) => {
      if (!el) {
        containerRef.current = null;
        return;
      }
      containerRef.current = el;

      // Re-entrant guard. If async setup is already running (or already
      // produced a terminal), this call is the synthetic-remount half of
      // StrictMode's double-invoke — bail. The in-flight setup will finish
      // and attach to the (same) container.
      if (initializingRef.current || terminalInstanceRef.current) return;
      initializingRef.current = true;

      void (async () => {
        try {
          const [{ Terminal }, { FitAddon }] = await Promise.all([
            import('@xterm/xterm'),
            import('@xterm/addon-fit'),
          ]);
          await import('@xterm/xterm/css/xterm.css');
          await document.fonts?.ready?.catch(() => undefined);

          if (!containerRef.current) return;
          if (terminalInstanceRef.current) return;

          // Theme xterm from the app's CSS tokens so the terminal follows the
          // active theme. readXtermTheme() reads them live; the observer below
          // re-applies on theme switch.
          const fontFamily =
            getComputedStyle(document.documentElement).getPropertyValue('--font-mono').trim() ||
            'Menlo, Monaco, "Courier New", monospace';

          // Construct xterm with the exact cols/rows the PTY was started
          // with, so there is no mount-time shrink; the post-open fit
          // below corrects the estimate against xterm's measured cells.
          const terminal = new Terminal({
            cursorBlink: true,
            fontSize: 14,
            fontFamily,
            cols: initialCols,
            rows: initialRows,
            scrollback: 5000,
            theme: readXtermTheme(),
          });

          const fitAddon = new FitAddon();
          terminal.loadAddon(fitAddon);
          terminal.open(containerRef.current);
          terminalInstanceRef.current = terminal;
          fitAddonRef.current = fitAddon as FitAddonLike;

          // Recolor an already-open terminal when the app theme changes, so
          // text/cursor track the theme live instead of only on reopen.
          const themeObserver = new MutationObserver(() => {
            const term = terminalInstanceRef.current as { options?: { theme?: unknown } } | null;
            if (term?.options) term.options.theme = readXtermTheme();
          });
          themeObserver.observe(document.documentElement, {
            attributes: true,
            attributeFilter: ['data-theme'],
          });
          themeObserverRef.current = themeObserver;

          terminal.onData((data: string) => {
            sendInput(data);
            onDataRef.current?.(data);
          });

          // PTY resize rides every xterm resize — the single source of
          // xterm/PTY lockstep.
          terminal.onResize(({ cols, rows }) => sendResize(cols, rows));

          // One-shot fit while the buffer is still empty. Snaps the
          // spawn-time estimate to xterm's real measured cell size before
          // any PTY bytes arrive; an empty-buffer resize cannot produce
          // the blank-rows artifact (there is no scrollback to reshuffle).
          fitAddon.fit();

          if (!sessionId) return;

          const ws = createTerminalSocket(sessionId);
          wsRef.current = ws;

          ws.onopen = () => {
            if (!mountedRef.current) return;
            setIsConnected(true);
            setHasConnected(true);
            // The post-open fit ran before the socket existed, so its
            // onResize send was a no-op — sync the PTY to what xterm
            // actually renders (also covers reconnect drift).
            const term = terminalInstanceRef.current as { cols?: number; rows?: number } | null;
            if (typeof term?.cols === 'number' && typeof term?.rows === 'number') {
              sendResize(term.cols, term.rows);
            }
            onReconnectRef.current?.();
          };

          ws.onmessage = (event: MessageEvent) => {
            if (!mountedRef.current) return;
            const term = terminalInstanceRef.current as {
              write?: (data: string | Uint8Array) => void;
            } | null;
            if (!term?.write) return;
            const data = event.data as string | ArrayBuffer | Blob;
            if (typeof data === 'string') {
              term.write(data);
            } else if (data instanceof ArrayBuffer) {
              term.write(new Uint8Array(data));
            } else if (data instanceof Blob) {
              void data.arrayBuffer().then((buf) => {
                if (mountedRef.current && term.write) {
                  term.write(new Uint8Array(buf));
                }
              });
            }
          };

          ws.onclose = () => {
            if (!mountedRef.current) return;
            setIsConnected(false);
            onDisconnectRef.current?.();
            const term = terminalInstanceRef.current as { write?: (data: string) => void } | null;
            if (term?.write) {
              term.write('\r\n\x1b[90m[Session ended]\x1b[0m\r\n');
            }
          };

          ws.onerror = () => {
            // onclose fires after onerror; don't double-handle.
          };
        } catch (err) {
          console.warn('useTerminal: failed to initialise terminal', err);
          // Surface the failure in the panel instead of leaving a silent blank
          // pane. A broken/stale frontend build (gitignored static/dist or an
          // incomplete node_modules) makes the dynamic xterm import throw here;
          // without this the user just sees "nothing, nada" with no clue why.
          const container = containerRef.current;
          if (container && !terminalInstanceRef.current) {
            const msg = err instanceof Error ? err.message : String(err);
            container.textContent = '';
            const box = document.createElement('div');
            box.setAttribute('role', 'alert');
            box.style.cssText =
              'padding:12px;font:13px/1.5 ui-monospace,monospace;color:var(--text-muted,#999);white-space:pre-wrap;';
            box.textContent =
              `Terminal failed to load: ${msg}\n\n` +
              'This usually means a stale or incomplete frontend build. ' +
              'Rebuild with `npm install && npm run build`, then hard-refresh the page.';
            container.appendChild(box);
          }
        } finally {
          initializingRef.current = false;
        }
      })();
    },
    [sessionId, initialCols, initialRows, sendInput, sendResize],
  );

  return {
    terminalRef,
    isConnected,
    hasConnected,
    sendInput,
    refit,
  };
}
