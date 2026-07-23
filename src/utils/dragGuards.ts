/**
 * Plugin/guest frames (<iframe>, <embed> — the live-browser panel, PDF viewers)
 * swallow mouse events, so a splitter drag that crosses them loses its
 * mousemove stream and never sees mouseup: the handle sticks to the cursor.
 * Call at drag start to make embedded content transparent to the pointer for
 * the duration of the drag; invoke the returned restore function on release.
 *
 * Implemented as a body class (styled in static/style.css) rather than
 * per-node inline styles, so it's idempotent and survives iframe remounts.
 *
 * Self-healing: the class must NEVER get stuck on, or every iframe/embed in the
 * app becomes permanently unclickable. A drag whose mouseup lands outside the
 * window is never delivered to us, so besides the caller's restore() we also
 * clear the guard on the next window mouseup/pointerup (capture), on window
 * blur, and on any mouse move with no button held — i.e. the moment it's proven
 * no drag is in progress.
 */
const GUARD_CLASS = 'ws-embed-drag';
let active = 0;

function clearGuard(): void {
  active = 0;
  document.body.classList.remove(GUARD_CLASS);
  window.removeEventListener('mouseup', clearGuard, true);
  window.removeEventListener('pointerup', clearGuard, true);
  window.removeEventListener('blur', clearGuard);
  window.removeEventListener('mousemove', onMoveFailsafe, true);
}

function onMoveFailsafe(e: MouseEvent): void {
  // A move with no buttons pressed means the (possibly lost) mouseup already
  // happened — release as soon as the cursor is back over the window.
  if (e.buttons === 0) clearGuard();
}

export function suppressEmbeddedPointerEvents(): () => void {
  active++;
  document.body.classList.add(GUARD_CLASS);
  if (active === 1) {
    window.addEventListener('mouseup', clearGuard, true);
    window.addEventListener('pointerup', clearGuard, true);
    window.addEventListener('blur', clearGuard);
    window.addEventListener('mousemove', onMoveFailsafe, true);
  }
  let released = false;
  return () => {
    if (released) return;
    released = true;
    active = Math.max(0, active - 1);
    if (active === 0) clearGuard();
  };
}
