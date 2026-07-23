import { useId, useRef, useEffect, useMemo } from 'react';
import { marked, Marked } from 'marked';
import {
  renderCodeBlock,
  attachCodeBlockHandlers,
  beginCodeBlockParse,
} from '@/utils/codeBlockEnhancer';
import { renderMarkdownSafe } from '@/utils/sanitizeHtml';
import { attachWsFileHandlers } from '@/utils/wsFileLinks';
import { toStepNarration } from '@/utils/stepNarration';

// Local Marked instance with `breaks: true` so newlines become <br>
// during streaming. Previously this was a module-scope
// `marked.setOptions({ breaks: true })` call which mutated the global
// marked config and bled into MarkdownRenderer, MarkdownPreview, etc.
// Local instance keeps the option scoped to this component.
const streamingParser = new Marked({ breaks: true });
const renderer = new marked.Renderer();
renderer.code = renderCodeBlock;

export interface StreamingMarkdownProps {
  content: string;
  isStreaming: boolean;
  className?: string;
  /** Reformat run-together step narration into an activity log (opt-in). */
  stepFormat?: boolean;
}

/**
 * Incremental markdown rendering during SSE streaming.
 * During streaming, updates DOM directly via ref to avoid full React re-renders.
 * Handles unclosed code fences during streaming by closing them before parsing.
 *
 * Security: Content comes from the AI model (trusted source via server SSE), not
 * user input. The marked library handles HTML escaping for code blocks and inline code.
 * HTML preview is rendered in sandboxed iframes only.
 */
export const StreamingMarkdown: React.FC<StreamingMarkdownProps> = ({
  content,
  isStreaming,
  className,
  stepFormat,
}) => {
  const containerRef = useRef<HTMLDivElement>(null);
  const lastContentRef = useRef<string>('');
  const cleanupRef = useRef<(() => void) | null>(null);
  // Stable namespace for code block IDs in this component instance. The
  // counter inside codeBlockEnhancer resets to 1 every parse, so re-
  // parsing the same source on each streamed token produces the same
  // `${instanceId}-1`, `${instanceId}-2`, ... — which is what makes
  // the per-block scroll memory below work across re-renders.
  const instanceId = useId();
  // Per-code-block scroll memory keyed by data-cb-id. Survives the
  // destroy-and-recreate cycle of innerHTML replacement during
  // streaming so a user who scrolled up inside a code block doesn't
  // get yanked back to the top on every token tick.
  const codeBlockScrollRef = useRef<Map<string, { atBottom: boolean; scrollTop: number }>>(
    new Map(),
  );

  // Parse markdown to HTML — memoized for the non-streaming (final) render.
  // renderMarkdownSafe = marked + DOMPurify. Same sanitization path used
  // by MarkdownRenderer; necessary because the streamed content can
  // include web_fetch results / MCP tool output that an attacker can
  // shape via prompt injection.
  const finalHtml = useMemo(() => {
    if (isStreaming) return '';
    if (!content) return '';
    beginCodeBlockParse(instanceId);
    return renderMarkdownSafe(stepFormat ? toStepNarration(content) : content, {
      parser: streamingParser,
      markedOptions: { renderer },
    });
  }, [content, isStreaming, instanceId, stepFormat]);

  // Capture per-block scroll state before innerHTML replaces the DOM.
  // Used as a snapshot so the post-replace restore step can decide
  // "follow the tail" vs "preserve where the user parked".
  const snapshotCodeBlockScroll = (container: HTMLElement) => {
    const blocks = container.querySelectorAll<HTMLElement>('.code-block-wrap');
    blocks.forEach((wrap) => {
      const id = wrap.dataset.cbId;
      if (!id) return;
      const pre = wrap.querySelector<HTMLElement>('pre');
      if (!pre) return;
      const distance = pre.scrollHeight - pre.scrollTop - pre.clientHeight;
      codeBlockScrollRef.current.set(id, {
        atBottom: distance < 8,
        scrollTop: pre.scrollTop,
      });
    });
  };

  // After innerHTML replace, walk the fresh DOM and either snap each
  // <pre> to the tail (user was at bottom, or block is new this tick)
  // or restore the saved scrollTop (user had scrolled up to read).
  const applyCodeBlockScroll = (container: HTMLElement) => {
    const blocks = container.querySelectorAll<HTMLElement>('.code-block-wrap');
    const seen = new Set<string>();
    blocks.forEach((wrap) => {
      const id = wrap.dataset.cbId;
      if (!id) return;
      seen.add(id);
      const pre = wrap.querySelector<HTMLElement>('pre');
      if (!pre) return;
      const saved = codeBlockScrollRef.current.get(id);
      if (!saved || saved.atBottom) {
        // No prior entry (block is brand-new this tick) or user was
        // at the bottom — follow the tail.
        pre.scrollTop = pre.scrollHeight;
      } else {
        // User parked above the tail — preserve their position.
        pre.scrollTop = saved.scrollTop;
      }
    });
    // Garbage-collect entries for blocks that no longer exist (e.g.
    // a re-parse dropped a block because its fence closed differently).
    for (const id of codeBlockScrollRef.current.keys()) {
      if (!seen.has(id)) codeBlockScrollRef.current.delete(id);
    }
  };

  // Toggle the `.streaming` accent class on the most recently emitted
  // code block so the live one is visually distinguishable + picks up
  // the 400 px max-height override in static/style.css.
  const markActiveStreamingBlock = (container: HTMLElement) => {
    const blocks = container.querySelectorAll<HTMLElement>('.code-block-wrap');
    blocks.forEach((el) => el.classList.remove('streaming'));
    const last = blocks[blocks.length - 1];
    if (last) last.classList.add('streaming');
  };

  // During streaming, update DOM directly to avoid full React re-renders.
  // Content is sanitized via DOMPurify in renderMarkdownSafe (necessary
  // because streams can carry web_fetch / MCP-tool output shaped by an
  // attacker through prompt injection).
  useEffect(() => {
    if (!isStreaming || !containerRef.current) return;
    if (content === lastContentRef.current) return;
    lastContentRef.current = content;

    const container = containerRef.current;
    // Snapshot scroll positions BEFORE the destroy-and-recreate.
    snapshotCodeBlockScroll(container);

    // Step-format first (a no-op while the text still has an open fence, which
    // it bails on), then close any unclosed fence for safe mid-stream parsing.
    const base = stepFormat ? toStepNarration(content) : content;
    const safeContent = closeOpenFences(base);
    beginCodeBlockParse(instanceId);
    const html = safeContent
      ? renderMarkdownSafe(safeContent, { parser: streamingParser, markedOptions: { renderer } })
      : '';
    container.innerHTML = html;

    // Restore scroll positions on the fresh DOM, tag the active block,
    // and re-attach delegated click handlers (Copy/Preview/Open/...).
    applyCodeBlockScroll(container);
    markActiveStreamingBlock(container);
    cleanupRef.current?.();
    cleanupRef.current = attachCodeBlockHandlers(container);
  }, [content, isStreaming, instanceId, stepFormat]);

  // Track user scroll inside any <pre> via event delegation on the
  // container. capture:true is required because `scroll` events do not
  // bubble — capture phase is the only way a parent listener sees them.
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const onScroll = (e: Event) => {
      const target = e.target as HTMLElement | null;
      if (!target || target.tagName !== 'PRE') return;
      const wrap = target.closest<HTMLElement>('.code-block-wrap');
      const id = wrap?.dataset.cbId;
      if (!id) return;
      const distance = target.scrollHeight - target.scrollTop - target.clientHeight;
      codeBlockScrollRef.current.set(id, {
        atBottom: distance < 8,
        scrollTop: target.scrollTop,
      });
    };
    container.addEventListener('scroll', onScroll, { capture: true, passive: true });
    return () => container.removeEventListener('scroll', onScroll, { capture: true } as EventListenerOptions);
  }, []);

  // When streaming stops, clear the ref tracker so a fresh stream starts clean
  // and drop the .streaming accent class from any code blocks.
  useEffect(() => {
    if (!isStreaming) {
      lastContentRef.current = '';
      cleanupRef.current?.();
      cleanupRef.current = null;
      const container = containerRef.current;
      if (container) {
        container
          .querySelectorAll<HTMLElement>('.code-block-wrap.streaming')
          .forEach((el) => el.classList.remove('streaming'));
      }
    }
  }, [isStreaming]);

  // Attach handlers for the final (non-streaming) render
  useEffect(() => {
    if (isStreaming || !containerRef.current) return;
    return attachCodeBlockHandlers(containerRef.current);
  }, [finalHtml, isStreaming]);

  // Reveal-in-Finder for #wsfile= source links. Delegated on the container, so
  // the single listener survives the streaming innerHTML swaps.
  useEffect(() => {
    if (!containerRef.current) return;
    return attachWsFileHandlers(containerRef.current);
  }, []);

  // Cleanup on unmount
  useEffect(() => {
    return () => { cleanupRef.current?.(); };
  }, []);

  const containerClassName = `markdown-content${stepFormat ? ' step-narration' : ''}${className ? ` ${className}` : ''}`;

  if (isStreaming) {
    return <div ref={containerRef} className={containerClassName} />;
  }

  // finalHtml is sanitized via DOMPurify inside renderMarkdownSafe — safe.
  return (
    <div
      ref={containerRef}
      className={containerClassName}
      dangerouslySetInnerHTML={{ __html: finalHtml }}
    />
  );
};

/**
 * If the content has an unclosed code fence (odd number of ``` sequences),
 * close it so marked can parse the complete part correctly.
 */
function closeOpenFences(text: string): string {
  const fencePattern = /^```/gm;
  const matches = text.match(fencePattern);
  if (!matches || matches.length % 2 === 0) return text;
  return text + '\n```';
}
