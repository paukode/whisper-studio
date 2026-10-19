import { useId, useMemo, useRef, useEffect, useLayoutEffect } from 'react';
import { marked } from 'marked';
import { renderCodeBlock, attachCodeBlockHandlers, beginCodeBlockParse } from '@/utils/codeBlockEnhancer';
import { renderMarkdownSafe } from '@/utils/sanitizeHtml';
import { attachWsFileHandlers } from '@/utils/wsFileLinks';
import { attachTableResizers } from '@/utils/tableResize';
import { linkifyPaths } from '@/utils/pathLinks';
import { useHtmlProp } from '@/hooks/useHtmlProp';
import { useUIStore } from '@/stores/uiStore';

// Configure marked with custom code block renderer
const renderer = new marked.Renderer();
renderer.code = renderCodeBlock;

export interface MarkdownRendererProps {
  content: string;
  /** Link plain path mentions to the editor against the workspace root
   *  (utils/pathLinks.ts). Off for a file's own markdown (a preview, a
   *  notebook cell), whose relative paths read against that file's folder,
   *  not the root. */
  linkPaths?: boolean;
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
export const MarkdownRenderer: React.FC<MarkdownRendererProps> = ({
  content,
  linkPaths = true,
}) => {
  const containerRef = useRef<HTMLDivElement>(null);
  // Unique per-instance prefix so code blocks in different message
  // bubbles never collide on the same ID inside the shared codeStore.
  const instanceId = useId();
  // Plain path mentions link to the file against the connected workspace
  // (utils/pathLinks.ts); with none connected the text is left alone.
  const wsPath = useUIStore((s) => s.wsPath);
  const linkRoot = linkPaths ? wsPath : '';

  const html = useMemo(() => {
    if (!content) return '';
    beginCodeBlockParse(instanceId);
    return renderMarkdownSafe(linkifyPaths(content, linkRoot), { markedOptions: { renderer } });
  }, [content, instanceId, linkRoot]);
  const innerHtml = useHtmlProp(html);

  // Attach code block action handlers via event delegation
  useEffect(() => {
    if (!containerRef.current) return;
    return attachCodeBlockHandlers(containerRef.current);
  }, [html]);

  // Route #wsfile= links (search sources, write results, path mentions) to
  // the side panel or the OS app, and the localhost ones to the Live pane.
  useEffect(() => {
    if (!containerRef.current) return;
    return attachWsFileHandlers(containerRef.current);
  }, [html]);

  // Frame every table (wide ones scroll sideways) and give its header cells
  // column resize handles (utils/tableResize.ts). A layout effect, so a wide
  // table is never painted outside its frame for a frame.
  useLayoutEffect(() => {
    if (!containerRef.current) return;
    return attachTableResizers(containerRef.current);
  }, [html]);

  // html is sanitized by DOMPurify inside renderMarkdownSafe above, so it
  // is safe to inject as raw HTML.
  return (
    <div
      ref={containerRef}
      className="markdown-content"
      dangerouslySetInnerHTML={innerHtml}
    />
  );
};
