import DOMPurify from 'dompurify';
import { marked } from 'marked';

/**
 * The ONLY path that should produce HTML strings consumed by React's
 * raw-HTML escape hatch in this app. Every existing render site was
 * piping `marked.parse(...)` directly into that prop with no
 * sanitization, which let prompt-injected web pages (via web_fetch)
 * fire `<img onerror>` and similar payloads inside the SPA's
 * same-origin context — chained with the `/api/workspace/shell`
 * `user_approved` bypass that was a full RCE.
 *
 * Render path:
 *   markdown text -> marked.parse -> DOMPurify.sanitize -> safe HTML
 *
 * Pass `parser` to use a custom Marked instance (e.g. the
 * `breaks: true` instance used by StreamingMarkdown) instead of the
 * module default. This keeps consumers from mutating the global
 * `marked` config and stepping on each other.
 *
 * DOMPurify is loaded as a direct dep (see package.json). The
 * `USE_PROFILES: { html: true }` setting allows standard HTML
 * elements and attributes but strips `<script>`, `on*` handlers,
 * `javascript:` URLs, and other XSS vectors.
 */
/** Minimal interface so both the `marked` module default and a
 *  `new Marked()` instance satisfy the same parser shape. Marked's
 *  overload signatures don't unify cleanly when typed via
 *  `typeof marked.parse`, so we use a structural type instead. */
export interface MarkdownParser {
  parse(src: string, options?: Record<string, unknown>): string | Promise<string>;
}

export interface RenderMarkdownOptions {
  /** Custom Marked instance to use instead of the module default. */
  parser?: MarkdownParser;
  /** Any additional options forwarded to marked.parse. */
  markedOptions?: Record<string, unknown>;
}

export function renderMarkdownSafe(
  content: string,
  options?: RenderMarkdownOptions,
): string {
  const parser: MarkdownParser = options?.parser ?? marked;
  const raw = parser.parse(content ?? '', {
    async: false,
    ...options?.markedOptions,
  }) as string;
  return DOMPurify.sanitize(raw, { USE_PROFILES: { html: true } });
}

/**
 * Sanitize already-HTML content that did NOT come from marked
 * (e.g. mammoth's .docx -> HTML conversion in WordViewer).
 */
export function sanitizeHtml(html: string): string {
  return DOMPurify.sanitize(html ?? '', { USE_PROFILES: { html: true } });
}
