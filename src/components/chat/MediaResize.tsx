import React, { useCallback, useId, useLayoutEffect, useRef, useState } from 'react';
import type { ChatMessage, MediaSize } from '@/types/chat';
import { getActiveChatStore } from '@/stores/sessionRuntimes';
import { useSessionStore } from '@/stores/sessionStore';
import { suppressEmbeddedPointerEvents } from '@/utils/dragGuards';

/**
 * Drag-to-resize for the pictures in a chat message: diagrams, charts and
 * preview screenshots. A grip in the picture's bottom-right corner resizes it
 * and a double click on the grip returns it to its natural size. The size is
 * saved on the message (`mediaSizes`), so the session reopens with it.
 *
 * A picture can grow past the message's text column, up to the width of the
 * chat. It widens its own message to fit rather than spilling out of it, so
 * it stays inside the message's box and nothing that clips or contains the
 * message (content-visibility, overflow) can cut it off.
 */

export const MIN_W = 160;
export const MIN_H = 120;
/** A press that moves less than this is a click, and saves nothing. */
const DRAG_SLOP = 3;

/**
 * The size a drag of (dx, dy) from `start` asks for, within the limits: never
 * under the minimum, never wider than `maxW`. A picture scales by the axis the
 * pointer moved further in proportion to its size, so its corner tracks the
 * pointer exactly on that axis, it shrinks as readily as it grows, and the
 * switch between axes happens where both give the same size. A chart's width
 * and height follow the pointer on their own.
 */
export function draggedSize(
  start: { w: number; h: number },
  dx: number,
  dy: number,
  keepRatio: boolean,
  maxW: number,
): MediaSize {
  const w0 = Math.max(1, start.w);
  const h0 = Math.max(1, start.h);
  const clampW = (w: number) => Math.round(Math.max(MIN_W, Math.min(w, Math.max(MIN_W, maxW))));
  if (keepRatio) {
    const sx = (w0 + dx) / w0;
    const sy = (h0 + dy) / h0;
    return { w: clampW(w0 * (Math.abs(sx - 1) >= Math.abs(sy - 1) ? sx : sy)) };
  }
  return { w: clampW(w0 + dx), h: Math.round(Math.max(MIN_H, h0 + dy)) };
}

/* Every sized picture in a message states how wide the message must be to
   hold it; the message takes the widest claim, capped at the chat's width. */
const wrapClaims = new WeakMap<HTMLElement, Map<string, number>>();

function claimWrapWidth(wrap: HTMLElement, id: string, px: number | null): void {
  let claims = wrapClaims.get(wrap);
  if (!claims) {
    claims = new Map();
    wrapClaims.set(wrap, claims);
  }
  if (px === null) claims.delete(id);
  else claims.set(id, px);
  const widest = Math.max(0, ...claims.values());
  wrap.style.minWidth = widest > 0 ? `min(100%, ${Math.ceil(widest)}px)` : '';
}

interface MediaResizeOptions {
  /** The saved size, or undefined for the natural one. */
  saved: MediaSize | undefined;
  /** Diagrams and screenshots keep their aspect ratio, so the width alone
   *  sizes them; a chart takes a width and a height of its own. */
  keepRatio: boolean;
  /** The height a chart shows now, where a drag starts from. */
  currentHeight?: number;
  /** A finished drag's size, or null to go back to the natural size. */
  onChange: (size: MediaSize | null) => void;
}

export function useMediaResize({ saved, keepRatio, currentHeight, onChange }: MediaResizeOptions) {
  const frameRef = useRef<HTMLDivElement>(null);
  const [live, setLive] = useState<MediaSize | null>(null);
  const id = useId();
  const size = live ?? saved ?? null;
  const width = size?.w;

  // Widen the message to the picture's width plus the padding around it.
  // The padding is measured on the left and assumed on the right too.
  useLayoutEffect(() => {
    const frame = frameRef.current;
    const wrap = frame?.closest<HTMLElement>('.chat-msg-wrap');
    if (!frame || !wrap || width === undefined) return;
    const inset = frame.getBoundingClientRect().left - wrap.getBoundingClientRect().left;
    claimWrapWidth(wrap, id, width + 2 * inset);
    return () => claimWrapWidth(wrap, id, null);
  }, [width, id]);

  const onPointerDown = useCallback(
    (e: React.PointerEvent<HTMLElement>) => {
      const frame = frameRef.current;
      if (!frame || e.button !== 0) return;
      e.preventDefault();
      e.stopPropagation();
      try {
        // Keeps the resize cursor, and the stream, while the pointer is off
        // the grip.
        e.currentTarget.setPointerCapture(e.pointerId);
      } catch {
        // A pointer that is already gone: the window listeners follow it.
      }

      const rect = frame.getBoundingClientRect();
      const w0 = rect.width;
      const h0 = keepRatio ? rect.height : (currentHeight ?? rect.height);
      // Room to grow: to the chat's inner right edge, keeping the message's
      // padding on that side.
      const wrap = frame.closest<HTMLElement>('.chat-msg-wrap');
      const list = frame.closest<HTMLElement>('.chat-messages');
      const inset = wrap ? rect.left - wrap.getBoundingClientRect().left : 0;
      let maxW = Number.POSITIVE_INFINITY;
      if (list) {
        const listRect = list.getBoundingClientRect();
        const padRight = parseFloat(getComputedStyle(list).paddingRight) || 0;
        maxW = listRect.left + list.clientLeft + list.clientWidth - padRight - rect.left - inset;
      }
      const sx = e.clientX;
      const sy = e.clientY;
      let moved = false;
      let last: MediaSize | null = null;
      // Charts are iframes, which would swallow the drag's pointer stream.
      const releaseFrames = suppressEmbeddedPointerEvents();

      const move = (ev: PointerEvent) => {
        const dx = ev.clientX - sx;
        const dy = ev.clientY - sy;
        if (!moved && Math.abs(dx) < DRAG_SLOP && Math.abs(dy) < DRAG_SLOP) return;
        moved = true;
        last = draggedSize({ w: w0, h: h0 }, dx, dy, keepRatio, maxW);
        setLive(last);
      };
      const up = () => {
        window.removeEventListener('pointermove', move);
        window.removeEventListener('pointerup', up);
        window.removeEventListener('pointercancel', up);
        releaseFrames();
        setLive(null);
        if (moved && last) onChange(last);
      };
      window.addEventListener('pointermove', move);
      window.addEventListener('pointerup', up);
      window.addEventListener('pointercancel', up);
    },
    [keepRatio, currentHeight, onChange],
  );

  const onDoubleClick = useCallback(
    (e: React.MouseEvent) => {
      e.preventDefault();
      e.stopPropagation();
      onChange(null);
    },
    [onChange],
  );

  return {
    frameRef,
    size,
    resizing: live !== null,
    gripProps: { onPointerDown, onDoubleClick },
  };
}

/** The corner handle. It stays out of the way until the picture is hovered. */
export const MediaResizeGrip: React.FC<{
  onPointerDown: (e: React.PointerEvent<HTMLElement>) => void;
  onDoubleClick: (e: React.MouseEvent) => void;
}> = (props) => (
  <span
    className="media-grip"
    title="Drag to resize. Double-click to reset."
    aria-hidden="true"
    {...props}
  />
);

/** A sized picture's width, for the `.media-sized` rule in viz.css, which
 *  never lets it outgrow its column. */
export function mediaWidthStyle(size: MediaSize | null): React.CSSProperties | undefined {
  return size ? ({ '--media-w': `${size.w}px` } as React.CSSProperties) : undefined;
}

/**
 * Save (or, with null, clear) a picture's size on its message in the active
 * session. The message count does not change, so the runtime's own watcher
 * would not notice; the save is asked for here.
 */
export function saveMediaSize(message: ChatMessage, key: string, size: MediaSize | null): void {
  const store = getActiveChatStore();
  const messages = store.getState().messages;
  const idx = messages.indexOf(message);
  if (idx < 0) return;
  const sizes = { ...message.mediaSizes };
  if (size) sizes[key] = size;
  else delete sizes[key];
  const updated = [...messages];
  updated[idx] = { ...message, mediaSizes: Object.keys(sizes).length > 0 ? sizes : undefined };
  store.getState().setMessages(updated);
  const sid = useSessionStore.getState().currentSessionId;
  if (sid) useSessionStore.getState().debouncedSave(sid);
}
