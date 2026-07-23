/**
 * Code block enhancement for markdown rendering.
 *
 * Provides:
 * - Styled code block wrappers matching the vanilla CSS (.code-block-wrap)
 * - Copy button for all code blocks
 * - Preview / Open / Download buttons for HTML code blocks
 * - Preview overlay modal for HTML preview
 *
 * Works with `marked` v18 custom renderer and event delegation.
 *
 * Security note: HTML from a code block is rendered ONLY inside a sandboxed
 * iframe (allow-scripts, no allow-same-origin) — both the inline Preview overlay
 * and the "Open"/"Open in Tab" paths (via openHtmlSandboxed). Model output is
 * attacker-influenceable via prompt injection, so it must never run same-origin
 * with the app; a blob: window would inherit the app origin and reach the local
 * backend API. The overlay DOM is constructed using safe DOM methods.
 */

import hljs from 'highlight.js/lib/core';
import type { LanguageFn } from 'highlight.js';
import bash from 'highlight.js/lib/languages/bash';
import c from 'highlight.js/lib/languages/c';
import cpp from 'highlight.js/lib/languages/cpp';
import csharp from 'highlight.js/lib/languages/csharp';
import css from 'highlight.js/lib/languages/css';
import diff from 'highlight.js/lib/languages/diff';
import go from 'highlight.js/lib/languages/go';
import ini from 'highlight.js/lib/languages/ini';
import java from 'highlight.js/lib/languages/java';
import javascript from 'highlight.js/lib/languages/javascript';
import json from 'highlight.js/lib/languages/json';
import markdown from 'highlight.js/lib/languages/markdown';
import php from 'highlight.js/lib/languages/php';
import python from 'highlight.js/lib/languages/python';
import ruby from 'highlight.js/lib/languages/ruby';
import rust from 'highlight.js/lib/languages/rust';
import shell from 'highlight.js/lib/languages/shell';
import sql from 'highlight.js/lib/languages/sql';
import typescript from 'highlight.js/lib/languages/typescript';
import xml from 'highlight.js/lib/languages/xml';
import yaml from 'highlight.js/lib/languages/yaml';
import { openHtmlSandboxed } from '@/utils/openHtmlSandboxed';

// Register a curated language set (core build keeps the bundle lean vs. the
// all-languages default). Each module registers its own aliases too — e.g.
// `javascript` covers js/jsx, `xml` covers html, `shell` covers console.
const HLJS_LANGUAGES: Record<string, LanguageFn> = {
  bash, c, cpp, csharp, css, diff, go, ini, java, javascript, json,
  markdown, php, python, ruby, rust, shell, sql, typescript, xml, yaml,
};
for (const [name, fn] of Object.entries(HLJS_LANGUAGES)) {
  hljs.registerLanguage(name, fn);
}
// A few extra aliases marked's fence labels commonly use that don't ship
// with the language module itself.
hljs.registerAliases(['sh', 'zsh'], { languageName: 'bash' });
hljs.registerAliases(['yml'], { languageName: 'yaml' });
hljs.registerAliases(['toml'], { languageName: 'ini' });
hljs.registerAliases(['jsx'], { languageName: 'javascript' });
hljs.registerAliases(['tsx'], { languageName: 'typescript' });
hljs.registerAliases(['html', 'xhtml', 'svg'], { languageName: 'xml' });

// Store raw code by ID so event handlers can retrieve it
const codeStore = new Map<string, string>();

// Per-parse ID state. The counter resets at the top of each parse so
// re-parsing the same source (which happens on every streamed token)
// produces the same IDs — that's what lets the scroll-position memory
// in StreamingMarkdown survive the destroy-and-recreate DOM cycle.
// Each consumer supplies a unique `prefix` (typically React's useId)
// so different message bubbles don't collide on the same counter
// values in `codeStore`.
let currentPrefix = 'cb';
let parseIdCounter = 0;

/**
 * Reset the per-parse counter and set a namespace prefix. Must be
 * called synchronously immediately before invoking `marked.parse`
 * (or `renderMarkdownSafe`) so that the renderer's `nextId()` picks
 * up the new state. Marked's parse is synchronous, so there's no
 * interleaving risk.
 */
export function beginCodeBlockParse(prefix: string): void {
  currentPrefix = prefix || 'cb';
  parseIdCounter = 0;
}

function nextId(): string {
  return `${currentPrefix}-${++parseIdCounter}`;
}

function escapeHtml(str: string): string {
  return str
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

/**
 * Syntax-highlight `code` for `lang` using highlight.js, returning safe HTML
 * (highlight.js escapes text and emits `<span class="hljs-…">` tokens that the
 * markdown.css theme colors). Falls back to plain escaped text for unknown
 * languages. `ignoreIllegals` keeps partial/streaming snippets from throwing.
 */
function highlightCode(code: string, lang: string): string {
  if (lang && hljs.getLanguage(lang)) {
    try {
      return hljs.highlight(code, { language: lang, ignoreIllegals: true }).value;
    } catch {
      // Fall through to plain escaped text on any highlighter error.
    }
  }
  return escapeHtml(code);
}

/**
 * Custom marked renderer for code blocks.
 * Returns HTML string matching existing CSS classes.
 */
export function renderCodeBlock(token: { text: string; lang?: string }): string {
  const lang = (token.lang || '').toLowerCase();
  const code = token.text;
  const id = nextId();
  codeStore.set(id, code);

  const isHtml = lang === 'html';
  const highlighted = highlightCode(code, lang);

  let actionsHtml = `<button class="btn-sm" data-cb-action="copy" data-cb-id="${id}">Copy</button>`;
  if (isHtml) {
    actionsHtml +=
      `<button class="btn-preview" data-cb-action="preview" data-cb-id="${id}">Preview</button>` +
      `<button class="btn-sm" data-cb-action="open" data-cb-id="${id}">Open</button>` +
      `<button class="btn-sm" data-cb-action="download" data-cb-id="${id}">Download</button>`;
  }

  return (
    `<div class="code-block-wrap" data-cb-id="${id}">` +
      `<div class="code-block-header">` +
        `<span>${escapeHtml(lang || 'text')}</span>` +
        `<div class="code-block-actions">${actionsHtml}</div>` +
      `</div>` +
      `<pre><code class="hljs language-${escapeHtml(lang)}">${highlighted}</code></pre>` +
    `</div>`
  );
}

/**
 * Attach click event delegation to a container element for code block actions.
 * Returns a cleanup function to remove the listener.
 */
export function attachCodeBlockHandlers(container: HTMLElement): () => void {
  const handleClick = (e: MouseEvent) => {
    const btn = (e.target as HTMLElement).closest('[data-cb-action]') as HTMLElement | null;
    if (!btn) return;
    const action = btn.dataset.cbAction;
    const id = btn.dataset.cbId;
    if (!action || !id) return;
    const code = codeStore.get(id);
    if (!code) return;

    switch (action) {
      case 'copy':
        void navigator.clipboard.writeText(code);
        btn.textContent = 'Copied!';
        setTimeout(() => { btn.textContent = 'Copy'; }, 1500);
        break;
      case 'preview':
        showPreviewOverlay(code);
        break;
      case 'open':
        openHtmlSandboxed(code);
        break;
      case 'download': {
        const blob = new Blob([code], { type: 'text/html' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = 'program.html';
        a.click();
        URL.revokeObjectURL(url);
        break;
      }
    }
  };

  container.addEventListener('click', handleClick);
  return () => container.removeEventListener('click', handleClick);
}

/**
 * Show a full-screen preview overlay with the given HTML content.
 * Constructs DOM using safe methods — the only dynamic content
 * is loaded into a sandboxed iframe via srcdoc.
 */
function showPreviewOverlay(html: string): void {
  // Remove existing overlay
  document.querySelector('.preview-overlay')?.remove();

  // Build overlay DOM safely
  const overlay = document.createElement('div');
  overlay.className = 'preview-overlay';

  const modal = document.createElement('div');
  modal.className = 'preview-modal';

  // Header
  const header = document.createElement('div');
  header.className = 'preview-header';
  const titleSpan = document.createElement('span');
  titleSpan.textContent = 'Preview';
  header.appendChild(titleSpan);

  const actions = document.createElement('div');
  actions.className = 'preview-actions';

  const openBtn = document.createElement('button');
  openBtn.className = 'btn-sm';
  openBtn.textContent = 'Open in Tab';
  openBtn.addEventListener('click', () => {
    openHtmlSandboxed(html);
  });

  const downloadBtn = document.createElement('button');
  downloadBtn.className = 'btn-sm';
  downloadBtn.textContent = 'Download';
  downloadBtn.addEventListener('click', () => {
    const blob = new Blob([html], { type: 'text/html' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'program.html';
    a.click();
    URL.revokeObjectURL(url);
  });

  const closeBtn = document.createElement('button');
  closeBtn.className = 'btn-icon preview-close';
  closeBtn.textContent = '\u00D7';
  closeBtn.addEventListener('click', () => overlay.remove());

  actions.appendChild(openBtn);
  actions.appendChild(downloadBtn);
  actions.appendChild(closeBtn);
  header.appendChild(actions);
  modal.appendChild(header);

  // Iframe — sandboxed, content via srcdoc
  const iframe = document.createElement('iframe');
  iframe.className = 'preview-frame';
  iframe.sandbox.add('allow-scripts');
  iframe.srcdoc = html;
  modal.appendChild(iframe);

  overlay.appendChild(modal);
  document.body.appendChild(overlay);

  // Close on backdrop click
  overlay.addEventListener('click', (e) => {
    if (e.target === overlay) overlay.remove();
  });

  // Close on Escape
  const onKeyDown = (e: KeyboardEvent) => {
    if (e.key === 'Escape') {
      e.preventDefault();
      overlay.remove();
      document.removeEventListener('keydown', onKeyDown);
    }
  };
  document.addEventListener('keydown', onKeyDown);
}
