/**
 * Resizable columns for rendered markdown tables (utils/tableResize.ts).
 *
 * The user's table crushed its short columns to a letter a line and had no
 * way to give a column more room. Every table now sits in a scrolling frame
 * with a handle on each header divider; the first drag freezes the widths
 * the browser chose, so moving one divider changes that column only and the
 * table takes up the difference. jsdom has no layout, so the widths the
 * browser would report are stubbed on the cells.
 */
import { afterEach, describe, expect, it } from 'vitest';
import { attachTableResizers, frameTables, KEY_STEP_PX, MIN_COLUMN_PX } from './tableResize';

const TABLE = `
  <p>Confirmed bugs:</p>
  <table>
    <thead><tr><th>ID</th><th>Priority</th><th>Bug and verified behavior</th><th>Source</th></tr></thead>
    <tbody><tr><td>B1</td><td>High</td><td>The bubble overflows</td><td><code>public/style.css:106-110</code></td></tr></tbody>
  </table>`;

/** Natural widths the stubbed browser gives the four columns, and their total. */
const WIDTHS = [37, 67, 336, 249];
const TOTAL = 690;

function stubWidth(el: Element, width: number, bottom = 20): void {
  el.getBoundingClientRect = () =>
    ({ width, height: bottom, top: 0, left: 0, right: width, bottom, x: 0, y: 0, toJSON: () => ({}) }) as DOMRect;
}

function mount(html = TABLE): HTMLElement {
  const container = document.createElement('div');
  container.className = 'markdown-content';
  container.innerHTML = html;
  document.body.appendChild(container);
  return container;
}

function parts(container: HTMLElement, index = 0) {
  const table = container.querySelectorAll('table')[index];
  const cells = Array.from(table.tHead!.rows[0].cells);
  const handles = Array.from(table.querySelectorAll<HTMLElement>('.md-col-resize'));
  return { table, cells, handles };
}

function stubNatural(container: HTMLElement): void {
  const { table, cells } = parts(container);
  stubWidth(table, TOTAL);
  cells.forEach((th, i) => stubWidth(th, WIDTHS[i]));
}

function drag(handle: HTMLElement, dx: number): void {
  handle.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, button: 0, clientX: 200, pointerId: 1 }));
  window.dispatchEvent(new PointerEvent('pointermove', { clientX: 200 + dx / 2, pointerId: 1 }));
  window.dispatchEvent(new PointerEvent('pointermove', { clientX: 200 + dx, pointerId: 1 }));
  window.dispatchEvent(new PointerEvent('pointerup', { clientX: 200 + dx, pointerId: 1 }));
}

function press(handle: HTMLElement, key: string): KeyboardEvent {
  const event = new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true });
  handle.dispatchEvent(event);
  return event;
}

const px = (el: HTMLElement) => parseFloat(el.style.width);

/** The aria values of a divider, as numbers. */
function values(handle: HTMLElement) {
  return {
    now: Number(handle.getAttribute('aria-valuenow')),
    min: Number(handle.getAttribute('aria-valuemin')),
    max: Number(handle.getAttribute('aria-valuemax')),
  };
}

const nextFrame = () => new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));

afterEach(() => {
  document.body.innerHTML = '';
  document.body.removeAttribute('style');
  document.body.className = '';
});

describe('attachTableResizers', () => {
  it('frames each table once, however often it runs', () => {
    const container = mount();
    const first = attachTableResizers(container);
    first();
    const second = attachTableResizers(container);
    attachTableResizers(container);

    expect(container.querySelectorAll('.md-table-wrap')).toHaveLength(1);
    const { table, cells, handles } = parts(container);
    expect(table.parentElement?.classList.contains('md-table-wrap')).toBe(true);
    expect(handles).toHaveLength(cells.length - 1);
    expect(container.querySelectorAll('.md-th-label')).toHaveLength(cells.length);
    expect(container.querySelector('.md-th-label .md-th-label')).toBeNull();
    second();
  });

  it('puts one handle on every header divider but the last, keeping the header text', () => {
    const container = mount();
    attachTableResizers(container);
    const { cells } = parts(container);

    cells.forEach((th, i) => {
      const handle = th.querySelector(':scope > .md-col-resize');
      expect(Boolean(handle)).toBe(i < cells.length - 1);
    });
    expect(cells.map((th) => th.textContent)).toEqual(['ID', 'Priority', 'Bug and verified behavior', 'Source']);
    const handle = cells[0].querySelector('.md-col-resize')!;
    expect(handle.getAttribute('role')).toBe('separator');
    expect(handle.getAttribute('aria-orientation')).toBe('vertical');
    expect(handle.getAttribute('aria-label')).toBe('Resize column');
    expect((handle as HTMLElement).tabIndex).toBe(0);
  });

  it('frames a streaming table without handles, once however many tokens arrive', () => {
    const container = mount();
    frameTables(container);
    frameTables(container);

    expect(container.querySelectorAll('.md-table-wrap')).toHaveLength(1);
    expect(container.querySelector('.md-table-wrap > table')).not.toBeNull();
    expect(container.querySelector('.md-col-resize')).toBeNull();
    expect(container.querySelector('.md-th-label')).toBeNull();
  });

  it('names each header by its own text, not by the handle inside it', () => {
    const container = mount();
    const other = mount();
    attachTableResizers(container);
    attachTableResizers(container);
    attachTableResizers(other);
    const { cells } = parts(container);

    const names = cells.map((th) => {
      const id = th.getAttribute('aria-labelledby');
      const label = id ? document.getElementById(id) : null;
      expect(label?.parentElement).toBe(th);
      expect(label?.classList.contains('md-th-label')).toBe(true);
      return label?.textContent;
    });
    expect(names).toEqual(['ID', 'Priority', 'Bug and verified behavior', 'Source']);
    // Ids stay unique across messages.
    const ids = Array.from(document.querySelectorAll('.md-th-label'), (el) => el.id);
    expect(new Set(ids).size).toBe(2 * cells.length);

    // A label the reply's HTML already carried gets an id too.
    const raw = mount('<table><thead><tr><th><span class="md-th-label">Own</span></th><th>B</th></tr></thead></table>');
    attachTableResizers(raw);
    const th = raw.querySelector('th')!;
    expect(document.getElementById(th.getAttribute('aria-labelledby') ?? '')?.textContent).toBe('Own');
  });

  it('gives each divider its column width as a value, and keeps it current', async () => {
    const container = mount();
    attachTableResizers(container);
    stubNatural(container);
    const { cells, handles } = parts(container);

    // The browser's widths are read in the next frame, after layout.
    await nextFrame();
    handles.forEach((handle, i) => {
      expect(handle.getAttribute('role')).toBe('separator');
      expect(values(handle)).toEqual({ now: WIDTHS[i], min: Math.min(MIN_COLUMN_PX, WIDTHS[i]), max: TOTAL });
    });

    press(handles[1], 'ArrowRight');
    expect(values(handles[1])).toEqual({ now: WIDTHS[1] + KEY_STEP_PX, min: MIN_COLUMN_PX, max: TOTAL + KEY_STEP_PX });
    // Every divider of the table knows the new total.
    expect(values(handles[0]).max).toBe(TOTAL + KEY_STEP_PX);

    drag(handles[2], -100);
    expect(values(handles[2]).now).toBe(WIDTHS[2] - 100);

    handles[1].dispatchEvent(new MouseEvent('dblclick', { bubbles: true, cancelable: true }));
    expect(values(handles[1]).now).toBe(WIDTHS[1]);
    expect(values(handles[2]).now).toBe(WIDTHS[2]);

    // A focused divider reads its column again (a panel resize reflows it).
    stubWidth(cells[1], 90);
    handles[1].focus();
    expect(values(handles[1]).now).toBe(90);
  });

  it('runs the accent line down the whole column while a divider moves or has focus', () => {
    const container = mount();
    attachTableResizers(container);
    stubNatural(container);
    const { table, handles } = parts(container);
    stubWidth(table, TOTAL, 140);
    stubWidth(handles[1], 8, 30);
    const line = () => table.style.getPropertyValue('--md-col-line');

    handles[1].dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, button: 0, clientX: 200, pointerId: 1 }));
    expect(line()).toBe('110px');
    // The rows wrap to a new height as the column narrows.
    stubWidth(table, TOTAL, 180);
    window.dispatchEvent(new PointerEvent('pointermove', { clientX: 180, pointerId: 1 }));
    expect(line()).toBe('150px');
    window.dispatchEvent(new PointerEvent('pointerup', { clientX: 180, pointerId: 1 }));
    expect(line()).toBe('');

    handles[1].focus();
    expect(line()).toBe('150px');
    handles[1].blur();
    expect(line()).toBe('');
  });

  it('decorates only the tables inside its container', () => {
    const container = mount();
    const other = mount();
    attachTableResizers(container);

    expect(container.querySelector('.md-table-wrap')).not.toBeNull();
    expect(other.querySelector('.md-table-wrap')).toBeNull();
    expect(other.querySelector('.md-col-resize')).toBeNull();
  });

  it('frames a table whose header spans columns, but gives it no handles', () => {
    const container = mount(
      '<table><thead><tr><th colspan="2">Both</th></tr></thead><tbody><tr><td>a</td><td>b</td></tr></tbody></table>',
    );
    attachTableResizers(container);
    expect(container.querySelector('.md-table-wrap > table')).not.toBeNull();
    expect(container.querySelector('.md-col-resize')).toBeNull();
  });

  it('freezes the widths on the first drag and changes only the dragged column', () => {
    const container = mount();
    attachTableResizers(container);
    stubNatural(container);
    const { table, cells, handles } = parts(container);

    drag(handles[1], 40);

    expect(table.classList.contains('md-table-resized')).toBe(true);
    expect(cells.map(px)).toEqual([WIDTHS[0], WIDTHS[1] + 40, WIDTHS[2], WIDTHS[3]]);
    expect(px(table)).toBe(TOTAL + 40);

    // A second drag starts from the frozen widths, not the browser's.
    drag(handles[2], -100);
    expect(cells.map(px)).toEqual([WIDTHS[0], WIDTHS[1] + 40, WIDTHS[2] - 100, WIDTHS[3]]);
    expect(px(table)).toBe(TOTAL - 60);
  });

  it('leaves the browser sizing alone for a press without movement', () => {
    const container = mount();
    attachTableResizers(container);
    stubNatural(container);
    const { table, cells, handles } = parts(container);

    drag(handles[1], 0);

    expect(table.classList.contains('md-table-resized')).toBe(false);
    expect(table.style.width).toBe('');
    expect(cells.every((th) => th.style.width === '')).toBe(true);
  });

  it('never takes a column below the floor, and the table shrinks by what the column lost', () => {
    const container = mount();
    attachTableResizers(container);
    stubNatural(container);
    const { table, cells, handles } = parts(container);

    drag(handles[2], -1000);

    expect(px(cells[2])).toBe(MIN_COLUMN_PX);
    expect(px(table)).toBe(TOTAL - (WIDTHS[2] - MIN_COLUMN_PX));
  });

  it('keeps a column that is already narrower than the floor at its own width', () => {
    const container = mount();
    attachTableResizers(container);
    stubNatural(container);
    const { cells, handles } = parts(container);

    drag(handles[0], -20);
    expect(px(cells[0])).toBe(WIDTHS[0]);
    press(handles[0], 'ArrowLeft');
    expect(px(cells[0])).toBe(WIDTHS[0]);
  });

  it('moves a divider by the key step with the Left and Right arrows', () => {
    const container = mount();
    attachTableResizers(container);
    stubNatural(container);
    const { table, cells, handles } = parts(container);

    const right = press(handles[1], 'ArrowRight');
    press(handles[1], 'ArrowRight');
    expect(right.defaultPrevented).toBe(true);
    expect(px(cells[1])).toBe(WIDTHS[1] + 2 * KEY_STEP_PX);
    expect(px(table)).toBe(TOTAL + 2 * KEY_STEP_PX);

    press(handles[1], 'ArrowLeft');
    expect(px(cells[1])).toBe(WIDTHS[1] + KEY_STEP_PX);
    for (let i = 0; i < 10; i++) press(handles[1], 'ArrowLeft');
    expect(px(cells[1])).toBe(MIN_COLUMN_PX);
    expect(cells.filter((_, i) => i !== 1).map(px)).toEqual([WIDTHS[0], WIDTHS[2], WIDTHS[3]]);
  });

  it('gives the sizing back to the browser on a double-click', () => {
    const container = mount();
    attachTableResizers(container);
    stubNatural(container);
    const { table, cells, handles } = parts(container);

    drag(handles[1], 40);
    handles[1].dispatchEvent(new MouseEvent('dblclick', { bubbles: true, cancelable: true }));

    expect(table.classList.contains('md-table-resized')).toBe(false);
    expect(table.style.width).toBe('');
    expect(cells.every((th) => th.style.width === '')).toBe(true);
  });

  it('resets on Escape and keeps the key from the rest of the app only when there was something to reset', () => {
    const container = mount();
    attachTableResizers(container);
    stubNatural(container);
    const { table, handles } = parts(container);
    const seen: string[] = [];
    const onDocKey = (e: KeyboardEvent) => seen.push(e.key);
    document.addEventListener('keydown', onDocKey);

    press(handles[1], 'Escape');
    expect(seen).toEqual(['Escape']);

    press(handles[1], 'ArrowRight');
    const escape = press(handles[1], 'Escape');
    expect(escape.defaultPrevented).toBe(true);
    expect(table.classList.contains('md-table-resized')).toBe(false);
    expect(seen).toEqual(['Escape', 'ArrowRight']);
    document.removeEventListener('keydown', onDocKey);
  });

  it('shows the drag while it lasts and restores the page after it', () => {
    const container = mount();
    attachTableResizers(container);
    stubNatural(container);
    const { handles } = parts(container);

    handles[1].dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, button: 0, clientX: 200, pointerId: 1 }));
    expect(handles[1].classList.contains('is-dragging')).toBe(true);
    expect(document.body.style.cursor).toBe('col-resize');

    window.dispatchEvent(new PointerEvent('pointerup', { clientX: 200, pointerId: 1 }));
    expect(handles[1].classList.contains('is-dragging')).toBe(false);
    expect(document.body.style.cursor).toBe('');
    expect(document.body.style.userSelect).toBe('');
    expect(document.body.classList.contains('ws-embed-drag')).toBe(false);
  });

  it('removes its listeners on cleanup and ends a drag in progress', () => {
    const container = mount();
    const cleanup = attachTableResizers(container);
    stubNatural(container);
    const { table, cells, handles } = parts(container);

    handles[1].dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, button: 0, clientX: 200, pointerId: 1 }));
    cleanup();
    expect(handles[1].classList.contains('is-dragging')).toBe(false);
    expect(document.body.style.cursor).toBe('');

    window.dispatchEvent(new PointerEvent('pointermove', { clientX: 260, pointerId: 1 }));
    drag(handles[1], 40);
    press(handles[1], 'ArrowRight');
    handles[1].focus();
    expect(table.classList.contains('md-table-resized')).toBe(false);
    expect(cells.every((th) => th.style.width === '')).toBe(true);
    expect(table.style.getPropertyValue('--md-col-line')).toBe('');
  });
});
