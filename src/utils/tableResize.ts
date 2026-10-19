import { suppressEmbeddedPointerEvents } from '@/utils/dragGuards';

/**
 * Resizable columns for rendered markdown tables.
 *
 * Runs on a markdown container once its sanitized HTML is in the DOM, so
 * nothing here goes through the sanitizer. Every table gets a
 * `.md-table-wrap` frame that scrolls sideways when the table is wider than
 * its column (`frameTables`, which a streaming reply also runs on every
 * token), and every header cell but the last gets a handle on its right edge
 * (styles in static/modules/markdown.css). Until a column is resized the
 * browser sizes the table from its words. The first drag or arrow key freezes
 * every column's current width and switches the table to fixed layout, so
 * moving one divider changes that column only and the table grows or shrinks
 * inside its frame. A double-click on a handle, or Escape while it has focus,
 * gives the sizing back to the browser.
 *
 * The DOM work is idempotent: a second call on the same DOM (StrictMode, a
 * re-run effect) reuses the frames, labels and handles. The returned function
 * removes every listener and ends a drag in progress.
 */

const WRAP_CLASS = 'md-table-wrap';
const LABEL_CLASS = 'md-th-label';
const HANDLE_CLASS = 'md-col-resize';
const RESIZED_CLASS = 'md-table-resized';
const DRAGGING_CLASS = 'is-dragging';
/** How far the accent line of a moving or focused handle runs below the
 *  header row (markdown.css), so it marks the whole column line. */
const LINE_VAR = '--md-col-line';

/** A drag or an arrow key never takes a column below this width. */
export const MIN_COLUMN_PX = 48;
/** One Left or Right arrow press moves a divider this far. */
export const KEY_STEP_PX = 16;

/** Ids for the header labels, unique across every container on the page. */
let labelSeq = 0;

/** Put every table in `container` in a `.md-table-wrap` frame, once. A
 *  table wider than its column then scrolls sideways in the frame instead
 *  of spilling out of the message. */
export function frameTables(container: HTMLElement): void {
  container.querySelectorAll('table').forEach((table) => {
    if (table.parentElement?.classList.contains(WRAP_CLASS)) return;
    const wrap = document.createElement('div');
    wrap.className = WRAP_CLASS;
    table.before(wrap);
    wrap.appendChild(table);
  });
}

/** The header cells that map one to one onto columns, or none when a cell
 *  spans columns or the table sets its own <col> widths (raw HTML). */
function headerCells(table: HTMLTableElement): HTMLTableCellElement[] {
  const row = table.tHead?.rows[0];
  if (!row || table.querySelector(':scope > colgroup')) return [];
  const cells = Array.from(row.cells);
  return cells.some((cell) => cell.colSpan !== 1) ? [] : cells;
}

function handleOf(th: HTMLTableCellElement): HTMLElement | null {
  return th.querySelector<HTMLElement>(`:scope > .${HANDLE_CLASS}`);
}

function addHandles(table: HTMLTableElement): void {
  const cells = headerCells(table);
  cells.forEach((th, i) => {
    // The header text sits in its own block, so a resized column can end a
    // word that does not fit in an ellipsis without clipping the handle. The
    // cell takes its name from that block only: a handle inside the cell
    // would otherwise add its value to the header ("Priority 50").
    let label = th.querySelector<HTMLElement>(`:scope > .${LABEL_CLASS}`);
    if (!label) {
      label = document.createElement('span');
      label.className = LABEL_CLASS;
      label.append(...Array.from(th.childNodes));
      th.appendChild(label);
    }
    // A label the reply's own HTML brought along may have no id.
    if (!label.id) label.id = `md-th-label-${++labelSeq}`;
    th.setAttribute('aria-labelledby', label.id);
    if (i === cells.length - 1 || handleOf(th)) return;
    const handle = document.createElement('span');
    handle.className = HANDLE_CLASS;
    handle.setAttribute('role', 'separator');
    handle.setAttribute('aria-orientation', 'vertical');
    handle.setAttribute('aria-label', 'Resize column');
    handle.tabIndex = 0;
    th.appendChild(handle);
  });
}

/** An element's width: the frozen one once the reader has resized the
 *  table, the browser's before. */
function widthOf(el: HTMLElement, frozen: boolean): number {
  return frozen ? parseFloat(el.style.width) : el.getBoundingClientRect().width;
}

/** Tell assistive technology where each divider is: the width in px of the
 *  column on its left, between the floor and the table's width. */
function syncValues(table: HTMLTableElement, cells: HTMLTableCellElement[]): void {
  const frozen = table.classList.contains(RESIZED_CLASS);
  const total = Math.round(widthOf(table, frozen));
  cells.forEach((th) => {
    const handle = handleOf(th);
    if (!handle) return;
    const now = Math.round(widthOf(th, frozen));
    handle.setAttribute('aria-valuenow', String(now));
    // A column the browser made narrower than the floor may stay that narrow.
    handle.setAttribute('aria-valuemin', String(Math.min(MIN_COLUMN_PX, now)));
    handle.setAttribute('aria-valuemax', String(Math.max(total, now)));
  });
}

/** Pin every column at the width the browser gave it, and the table at its
 *  total, so a later change to one column moves nothing else. */
function freeze(table: HTMLTableElement, cells: HTMLTableCellElement[]): void {
  if (table.classList.contains(RESIZED_CLASS)) return;
  const widths = cells.map((th) => th.getBoundingClientRect().width);
  table.style.width = `${table.getBoundingClientRect().width}px`;
  cells.forEach((th, i) => {
    th.style.width = `${widths[i]}px`;
  });
  table.classList.add(RESIZED_CLASS);
}

function unfreeze(table: HTMLTableElement, cells: HTMLTableCellElement[]): void {
  table.classList.remove(RESIZED_CLASS);
  table.style.removeProperty('width');
  cells.forEach((th) => th.style.removeProperty('width'));
}

/** Set one frozen column's width; the table takes up the difference. A
 *  column that was already narrower than the floor when the change began
 *  (`from`) may stay that narrow, never narrower. */
function setColumnWidth(
  table: HTMLTableElement,
  th: HTMLTableCellElement,
  width: number,
  from: number,
): void {
  const current = parseFloat(th.style.width);
  const next = Math.max(Math.min(MIN_COLUMN_PX, from), width);
  table.style.width = `${parseFloat(table.style.width) + next - current}px`;
  th.style.width = `${next}px`;
}

interface Target {
  handle: HTMLElement;
  table: HTMLTableElement;
  cells: HTMLTableCellElement[];
  th: HTMLTableCellElement;
}

function targetOf(event: Event, container: HTMLElement): Target | null {
  if (!(event.target instanceof Element)) return null;
  const handle = event.target.closest<HTMLElement>(`.${HANDLE_CLASS}`);
  const th = handle?.parentElement;
  const table = th?.closest('table');
  if (!handle || !(th instanceof HTMLTableCellElement) || !table || !container.contains(table)) {
    return null;
  }
  const cells = headerCells(table);
  return cells.includes(th) ? { handle, table, cells, th } : null;
}

/** Run the accent line of `handle` from the header row down to the bottom
 *  of the table (the handle itself covers the header cell only). */
function showLine({ table, handle }: Target): void {
  const below = table.getBoundingClientRect().bottom - handle.getBoundingClientRect().bottom;
  table.style.setProperty(LINE_VAR, `${Math.max(0, below)}px`);
}

function hideLine({ table }: Target): void {
  table.style.removeProperty(LINE_VAR);
}

/** After a change to a table's widths: new values on its dividers, and a
 *  line that still reaches the bottom when the rows wrapped to a new height. */
function afterChange(target: Target): void {
  syncValues(target.table, target.cells);
  const { handle } = target;
  if (handle.classList.contains(DRAGGING_CLASS) || handle === document.activeElement) {
    showLine(target);
  }
}

export function attachTableResizers(container: HTMLElement): () => void {
  frameTables(container);
  const tables = Array.from(container.querySelectorAll('table'));
  tables.forEach(addHandles);
  // The dividers' values need the browser's layout. Every message reads its
  // tables in the same frame, so a long history forces one layout, not one
  // a message (older messages skip layout while they are off screen).
  const firstValues = requestAnimationFrame(() => {
    tables.forEach((table) => syncValues(table, headerCells(table)));
  });

  let endDrag: (() => void) | null = null;

  const onPointerDown = (e: PointerEvent) => {
    const target = targetOf(e, container);
    if (!target || e.button !== 0) return;
    e.preventDefault();
    endDrag?.();
    const { handle, table, cells, th } = target;
    try {
      // Keeps the resize cursor, and the pointer stream, while the pointer
      // is off the handle.
      handle.setPointerCapture(e.pointerId);
    } catch {
      // A pointer that is already gone: the window listeners follow it.
    }
    const startX = e.clientX;
    let startWidth: number | null = null;
    const releaseFrames = suppressEmbeddedPointerEvents();
    handle.classList.add(DRAGGING_CLASS);
    showLine(target);
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';

    const move = (ev: PointerEvent) => {
      if (ev.pointerId !== e.pointerId) return;
      const dx = ev.clientX - startX;
      if (startWidth === null) {
        // A press without movement leaves the browser's sizing alone.
        if (dx === 0) return;
        freeze(table, cells);
        startWidth = parseFloat(th.style.width);
      }
      setColumnWidth(table, th, startWidth + dx, startWidth);
      afterChange(target);
    };
    const end = (ev?: PointerEvent) => {
      if (ev && ev.pointerId !== e.pointerId) return;
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', end);
      window.removeEventListener('pointercancel', end);
      releaseFrames();
      handle.classList.remove(DRAGGING_CLASS);
      if (handle !== document.activeElement) hideLine(target);
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
      endDrag = null;
    };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', end);
    window.addEventListener('pointercancel', end);
    endDrag = () => end();
  };

  const onKeyDown = (e: KeyboardEvent) => {
    const target = targetOf(e, container);
    if (!target) return;
    const { table, cells, th } = target;
    if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
      e.preventDefault();
      freeze(table, cells);
      const width = parseFloat(th.style.width);
      setColumnWidth(table, th, width + (e.key === 'ArrowRight' ? KEY_STEP_PX : -KEY_STEP_PX), width);
      afterChange(target);
    } else if (e.key === 'Escape' && table.classList.contains(RESIZED_CLASS)) {
      e.preventDefault();
      e.stopPropagation();
      unfreeze(table, cells);
      afterChange(target);
    }
  };

  const onDoubleClick = (e: MouseEvent) => {
    const target = targetOf(e, container);
    if (!target) return;
    e.preventDefault();
    unfreeze(target.table, target.cells);
    afterChange(target);
  };

  // A focused divider reads its column afresh (a panel resize reflows a
  // table the reader has not resized) and draws its line down the column.
  const onFocusIn = (e: FocusEvent) => {
    const target = targetOf(e, container);
    if (!target) return;
    syncValues(target.table, target.cells);
    showLine(target);
  };
  const onFocusOut = (e: FocusEvent) => {
    const target = targetOf(e, container);
    if (target && !target.handle.classList.contains(DRAGGING_CLASS)) hideLine(target);
  };

  container.addEventListener('pointerdown', onPointerDown);
  container.addEventListener('keydown', onKeyDown);
  container.addEventListener('dblclick', onDoubleClick);
  container.addEventListener('focusin', onFocusIn);
  container.addEventListener('focusout', onFocusOut);
  return () => {
    cancelAnimationFrame(firstValues);
    endDrag?.();
    container.removeEventListener('pointerdown', onPointerDown);
    container.removeEventListener('keydown', onKeyDown);
    container.removeEventListener('dblclick', onDoubleClick);
    container.removeEventListener('focusin', onFocusIn);
    container.removeEventListener('focusout', onFocusOut);
  };
}
