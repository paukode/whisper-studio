import { useId, useMemo, useRef, useEffect } from 'react';
import { marked } from 'marked';
import { renderCodeBlock, attachCodeBlockHandlers, beginCodeBlockParse } from '@/utils/codeBlockEnhancer';
import { renderMarkdownSafe } from '@/utils/sanitizeHtml';
import { attachWsFileHandlers } from '@/utils/wsFileLinks';
import { toStepNarration } from '@/utils/stepNarration';

// Configure marked with custom code block renderer
const renderer = new marked.Renderer();
renderer.code = renderCodeBlock;

export interface MarkdownRendererProps {
  content: string;
  className?: string;
  /** Reformat run-together step narration ("Let me…", "Now…") into an
   *  activity log. Opt-in — set only on assistant/agent narration, never on
   *  tool results or documents. No-op unless the text is multi-step prose. */
  stepFormat?: boolean;
}

/**
 * Static markdown rendering component.
 * Uses `marked` to parse markdown to HTML with custom code block rendering
 * (copy, preview, open, download for HTML blocks).
 * Memoizes the parsed HTML to avoid unnecessary re-parsing on re-renders.
 *
 * Content sources are NOT all trusted: web_fetch results, MCP tool
 * outputs, attached documents, etc. flow through this renderer.
 * `renderMarkdownSafe` runs DOMPurify over the marked output so a
 * prompt-injected `<img onerror>` cannot fire inside the SPA's
 * same-origin context.
 */
export const MarkdownRenderer: React.FC<MarkdownRendererProps> = ({ content, className, stepFormat }) => {
  const containerRef = useRef<HTMLDivElement>(null);
  // Unique per-instance prefix so code blocks in different message
  // bubbles never collide on the same ID inside the shared codeStore.
  const instanceId = useId();

  const html = useMemo(() => {
    if (!content) return '';
    beginCodeBlockParse(instanceId);
    const src = stepFormat ? toStepNarration(content) : content;
    return renderMarkdownSafe(src, { markedOptions: { renderer } });
  }, [content, instanceId, stepFormat]);

  // Attach code block action handlers via event delegation
  useEffect(() => {
    if (!containerRef.current) return;
    return attachCodeBlockHandlers(containerRef.current);
  }, [html]);

  // Make workspace_semantic_search "source" links (#wsfile=…) reveal in Finder.
  useEffect(() => {
    if (!containerRef.current) return;
    return attachWsFileHandlers(containerRef.current);
  }, [html]);

  // html is sanitized by DOMPurify inside renderMarkdownSafe above —
  // safe to inject as raw HTML.
  return (
    <div
      ref={containerRef}
      className={`markdown-content${stepFormat ? ' step-narration' : ''}${className ? ` ${className}` : ''}`}
      dangerouslySetInnerHTML={{ __html: html }}
    />
  );
};
