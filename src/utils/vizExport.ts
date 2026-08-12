/**
 * Export helpers for inline visuals (VizCard).
 *
 * Diagrams are styled entirely by CSS classes from viz.css, so a naive
 * `outerHTML` of the live node exports an unstyled black-on-transparent
 * skeleton. `standaloneSvg` resolves the computed style of every node into
 * presentation attributes first, which is what makes a downloaded file look
 * like what was on screen — including whichever theme was active.
 *
 * Charts do their own SVG/PNG serialisation inside the sandboxed chart host
 * (Vega's View API) and post the result back, so they never come through here.
 */

/** Style properties that carry a diagram's appearance. Copying only these
 *  keeps the exported file small; copying the whole computed style inflates a
 *  20KB diagram into megabytes of `font-variant-east-asian` noise. */
const CARRIED = [
  'fill',
  'fill-opacity',
  'stroke',
  'stroke-width',
  'stroke-opacity',
  'stroke-linecap',
  'stroke-linejoin',
  'stroke-dasharray',
  'font-family',
  'font-size',
  'font-weight',
  'font-style',
  'text-anchor',
  'opacity',
] as const;

export interface SvgSize {
  width: number;
  height: number;
}

/**
 * Browsers resolve the theme's `color-mix()` fills to CSS Color 4 syntax
 * (`color(srgb 0.24 0.19 0.16)`). Browsers read that back fine, but Figma,
 * Illustrator and Inkscape drop the fill entirely, so a downloaded diagram
 * would open as a set of unfilled outlines. Convert to plain rgb()/rgba().
 */
export function toLegacyColor(value: string): string {
  return value.replace(
    /color\(srgb\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)(?:\s*\/\s*([\d.]+))?\s*\)/gi,
    (_all, r: string, g: string, b: string, a?: string) => {
      const ch = (c: string) => Math.round(Math.min(1, Math.max(0, Number(c))) * 255);
      const rgb = `${ch(r)}, ${ch(g)}, ${ch(b)}`;
      return a === undefined || Number(a) >= 1 ? `rgb(${rgb})` : `rgba(${rgb}, ${a})`;
    },
  );
}

/** Intrinsic size from the viewBox, falling back to the rendered box. */
export function svgSize(svg: SVGSVGElement): SvgSize {
  const vb = svg.viewBox?.baseVal;
  if (vb && vb.width > 0 && vb.height > 0) return { width: vb.width, height: vb.height };
  const rect = svg.getBoundingClientRect();
  return { width: Math.max(1, rect.width), height: Math.max(1, rect.height) };
}

/**
 * A self-contained copy of `svg`: computed styles flattened onto every
 * element, explicit pixel dimensions, and an opaque background so the file
 * reads correctly in viewers that composite onto white (or onto black).
 */
export function standaloneSvg(svg: SVGSVGElement, background: string): SVGSVGElement {
  const { width, height } = svgSize(svg);
  const clone = svg.cloneNode(true) as SVGSVGElement;

  const sources = svg.querySelectorAll('*');
  const targets = clone.querySelectorAll('*');
  for (let i = 0; i < sources.length; i++) {
    const computed = window.getComputedStyle(sources[i]);
    const target = targets[i] as SVGElement;
    for (const prop of CARRIED) {
      const value = computed.getPropertyValue(prop);
      // `none` on fill/stroke is meaningful and must be kept; empty is not.
      if (value) target.setAttribute(prop, toLegacyColor(value));
    }
    target.removeAttribute('class');
  }

  clone.setAttribute('xmlns', 'http://www.w3.org/2000/svg');
  clone.setAttribute('width', String(width));
  clone.setAttribute('height', String(height));
  clone.setAttribute('viewBox', `0 0 ${width} ${height}`);

  const bg = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
  bg.setAttribute('x', '0');
  bg.setAttribute('y', '0');
  bg.setAttribute('width', String(width));
  bg.setAttribute('height', String(height));
  bg.setAttribute('fill', background);
  clone.insertBefore(bg, clone.firstChild);

  return clone;
}

export function serializeSvg(svg: SVGSVGElement): string {
  return new XMLSerializer().serializeToString(svg);
}

/** Rasterise SVG markup to a PNG blob at `scale`x for crisp retina output. */
export function svgToPngBlob(
  markup: string,
  size: SvgSize,
  scale = 2,
): Promise<Blob | null> {
  return new Promise((resolve) => {
    const image = new Image();
    // A data: URL keeps the image same-origin-clean, so the canvas stays
    // untainted and toBlob is allowed. A blob: URL would not.
    image.src = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(markup)}`;
    image.onload = () => {
      const canvas = document.createElement('canvas');
      canvas.width = Math.round(size.width * scale);
      canvas.height = Math.round(size.height * scale);
      const ctx = canvas.getContext('2d');
      if (!ctx) return resolve(null);
      ctx.drawImage(image, 0, 0, canvas.width, canvas.height);
      canvas.toBlob((blob) => resolve(blob), 'image/png');
    };
    image.onerror = () => resolve(null);
  });
}

/** Convert a `data:` URL (what Vega's toImageURL returns) into a Blob. */
export async function dataUrlToBlob(dataUrl: string): Promise<Blob | null> {
  try {
    return await (await fetch(dataUrl)).blob();
  } catch {
    return null;
  }
}

/** Put a PNG on the clipboard. Returns false when the browser refuses. */
export async function copyPngToClipboard(blob: Blob): Promise<boolean> {
  try {
    if (typeof ClipboardItem === 'undefined' || !navigator.clipboard?.write) return false;
    await navigator.clipboard.write([new ClipboardItem({ 'image/png': blob })]);
    return true;
  } catch {
    return false;
  }
}

/** `Session costs by model` -> `session-costs-by-model` */
export function slugify(title: string, fallback: string): string {
  return (
    title
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, '-')
      .replace(/(^-|-$)/g, '') || fallback
  );
}
