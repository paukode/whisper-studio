/**
 * The app theme handed to model-authored HTML pages (create_artifact cards).
 *
 * The page runs in a sandboxed iframe on an opaque origin, so it can neither
 * read the app's stylesheet nor load the bundled fonts by URL. The card adds
 * one <style> block instead: the active palette and mode as --ws-* custom
 * properties, and the bundled fonts as data: URLs. The model styles the page
 * with those variables (server/ask_user.py CREATE_ARTIFACT_TOOL names them,
 * tests/test_artifact_theme_contract.py pins the two lists together), so one
 * page follows whichever palette and mode the user picked.
 *
 * The stored HTML never changes: the block is added when the card previews,
 * opens or downloads the page, and a download keeps the values on screen at
 * that moment, like the SVG and PNG exports of diagrams and charts.
 */

/** Page variable, app token it is read from. */
export const ARTIFACT_VARS: ReadonlyArray<readonly [string, string]> = [
  ['--ws-scheme', 'color-scheme'],
  ['--ws-bg', '--bg-primary'],
  ['--ws-surface', '--bg-surface'],
  ['--ws-surface-2', '--bg-elevated'],
  ['--ws-border', '--border'],
  ['--ws-text', '--text-primary'],
  ['--ws-heading', '--text-heading'],
  ['--ws-muted', '--text-muted'],
  ['--ws-accent', '--accent'],
  ['--ws-on-accent', '--on-accent'],
  ['--ws-accent-soft', '--accent-dim'],
  ['--ws-accent-2', '--accent-2'],
  ['--ws-success', '--success'],
  ['--ws-warning', '--warning'],
  ['--ws-danger', '--danger'],
  ['--ws-c1', '--speaker-1'],
  ['--ws-c2', '--speaker-2'],
  ['--ws-c3', '--speaker-3'],
  ['--ws-c4', '--speaker-4'],
  ['--ws-c5', '--speaker-5'],
  ['--ws-c6', '--speaker-6'],
  ['--ws-font', '--font-sans'],
  ['--ws-mono', '--font-mono'],
  ['--ws-radius', '--radius-md'],
  ['--ws-shadow', '--shadow-sm'],
];

export const ARTIFACT_THEME_STYLE_ID = 'ws-theme';

/** The --ws-* variables as one :root rule, read live from the app. */
export function readArtifactVars(root: HTMLElement = document.documentElement): string {
  const style = getComputedStyle(root);
  const decls: string[] = [];
  for (const [name, source] of ARTIFACT_VARS) {
    const value = style.getPropertyValue(source).trim();
    if (value) decls.push(`${name}:${value}`);
  }
  return `:root{${decls.join(';')}}`;
}

/** @font-face rules of the fonts bundled under /static/fonts. */
function bundledFontFaces(): CSSFontFaceRule[] {
  const faces: CSSFontFaceRule[] = [];
  for (const sheet of Array.from(document.styleSheets)) {
    let rules: CSSRuleList;
    try {
      rules = sheet.cssRules;
    } catch {
      continue; // a cross-origin sheet keeps its rules to itself
    }
    for (const rule of Array.from(rules)) {
      if (rule instanceof CSSFontFaceRule && rule.cssText.includes('/static/fonts/')) {
        faces.push(rule);
      }
    }
  }
  return faces;
}

const FONT_URL_RE = /url\((['"]?)([^'")]+)\1\)/g;

function toDataUrl(url: string): Promise<string> {
  return fetch(url)
    .then((r) => (r.ok ? r.blob() : Promise.reject(new Error(url))))
    .then(
      (blob) =>
        new Promise<string>((resolve, reject) => {
          const reader = new FileReader();
          reader.onload = () => resolve(String(reader.result));
          reader.onerror = () => reject(reader.error);
          reader.readAsDataURL(blob);
        }),
    );
}

async function inlineFontFace(rule: CSSFontFaceRule): Promise<string> {
  const urls = Array.from(rule.cssText.matchAll(FONT_URL_RE), (m) => m[2]);
  const data = await Promise.all(urls.map(toDataUrl));
  let i = 0;
  return rule.cssText.replace(FONT_URL_RE, () => `url("${data[i++]}")`);
}

let fontCssPromise: Promise<string> | null = null;

/** The bundled fonts as self-contained @font-face rules, read once per app
 *  session. Never rejects: without them the page falls back to the system
 *  fonts named after the bundled ones in --ws-font and --ws-mono. */
export function loadArtifactFonts(): Promise<string> {
  fontCssPromise ??= Promise.all(bundledFontFaces().map(inlineFontFace))
    .then((rules) => rules.join('\n'))
    .catch(() => {
      fontCssPromise = null;
      return '';
    });
  return fontCssPromise;
}

/** The whole theme block for a page: fonts plus the variables right now. */
export async function artifactThemeCss(): Promise<string> {
  return (await loadArtifactFonts()) + readArtifactVars();
}

/**
 * Put the theme block at the top of the page's <head>, so the page's own
 * styles come later and win when they redefine a variable (a page built in
 * a look the user asked for). It goes after <meta charset> when there is one,
 * which keeps the charset inside the first 1024 bytes of a downloaded file.
 */
export function withArtifactTheme(html: string, css: string): string {
  const block = `<style id="${ARTIFACT_THEME_STYLE_ID}">${css}</style>`;
  const headEnd = html.search(/<\/head\s*>/i);
  const head = headEnd >= 0 ? html.slice(0, headEnd) : html;
  const anchor =
    /<meta\s[^>]*charset[^>]*>/i.exec(head) ??
    /<head(\s[^>]*)?>/i.exec(html) ??
    /<html(\s[^>]*)?>/i.exec(html) ??
    /<!doctype[^>]*>/i.exec(html);
  if (!anchor) return block + html;
  const at = anchor.index + anchor[0].length;
  return html.slice(0, at) + block + html.slice(at);
}
