import React, { useEffect, useRef, useState } from 'react';
import { get } from '@/api/client';
import { sourceFileRawUrl } from '@/api/workspace';
import { getLangForPath } from '@/utils/languageDetection';
import { MarkdownRenderer } from '@/components/markdown/MarkdownRenderer';
import { MonacoEditor } from '@/components/workspace/MonacoEditor';
import { WordViewer } from '@/components/viewers/WordViewer';
import { PDFViewer } from '@/components/viewers/PDFViewer';
import { SpreadsheetViewer } from '@/components/viewers/SpreadsheetViewer';
import { ImageViewer } from '@/components/viewers/ImageViewer';
import { CSVViewer } from '@/components/viewers/CSVViewer';
import { NotebookViewer } from '@/components/viewers/NotebookViewer';
import { MarkdownPreview } from '@/components/viewers/MarkdownPreview';

/**
 * FilePanel — renders a chat "source" file opened in the dock (from an index
 * answer's #wsfile source link), with the same viewer coverage as the
 * workspace panel: mammoth docx, browser pdf, exceljs grid, zoomable images,
 * CSV table, notebook cells, markdown preview, and read-only Monaco with
 * syntax highlighting for code. Content comes from /api/workspace/source-file,
 * which resolves the absolute paths grounded citations use against the
 * *indexed* folders, not just the connected workspace. Formats with no native
 * viewer (.doc/.xls/.pptx/.epub/…) fall back to server-side text extraction —
 * richer than the workspace tab, which shows only a "binary file" notice.
 */
type SourceFile =
  | { kind: 'markdown' | 'text'; content: string; name?: string; path?: string }
  | { kind: 'image'; name?: string; path?: string }
  | { kind: 'unsupported'; message: string; name?: string; path?: string };

/** How to render the fetched text content. */
type TextMode = 'csv' | 'notebook' | 'markdown' | 'code' | 'extraction';

// Native viewers parse the modern formats only: mammoth can't read legacy
// .doc, exceljs can't read .xls — those route to the extraction fallback.
// TIFF is excluded: only Safari decodes it in <img>; it gets a reveal hint.
const IMAGE_EXTS = new Set(['png', 'jpg', 'jpeg', 'gif', 'bmp', 'webp', 'svg', 'ico']);
const MARKDOWN_EXTS = new Set(['md', 'markdown', 'mdx']);
// Rich documents the server extracts to markdown (no native viewer exists).
const EXTRACTION_EXTS = new Set(['doc', 'xls', 'pptx', 'ppt', 'html', 'htm', 'epub', 'rtf', 'odt', 'odp', 'ods']);

// exceljs parses on the main thread and the grid renders unvirtualized, so a
// huge cited workbook would freeze the tab. Checked via a HEAD probe below.
const XLSX_MAX_BYTES = 30 * 1024 * 1024;

const REVEAL_HINT = 'Preview isn’t available for this file type here. Cmd/Ctrl-click the source link to reveal it in Finder.';

function extOf(path: string): string {
  const base = path.split('/').pop() ?? path;
  const i = base.lastIndexOf('.');
  return i >= 0 ? base.slice(i + 1).toLowerCase() : '';
}

const MUTED: React.CSSProperties = { color: 'var(--text-muted, #888)', fontSize: 13, padding: 12 };

/** Size-guard wrapper: HEAD the raw URL first and refuse to hand a huge
 *  workbook to exceljs (indexed folders can hold spreadsheets far bigger than
 *  anything a workspace tab would open). */
const GuardedSpreadsheet: React.FC<{ path: string; rawUrl: string }> = ({ path, rawUrl }) => {
  const [state, setState] = useState<'checking' | 'ok' | 'too-big'>('checking');

  useEffect(() => {
    let alive = true;
    fetch(rawUrl, { method: 'HEAD' })
      .then((res) => {
        // Trust content-length only on a successful probe; on any failure fall
        // through to the viewer so it can surface the real fetch error.
        const size = res.ok ? Number(res.headers.get('content-length') || 0) : 0;
        if (alive) setState(size > XLSX_MAX_BYTES ? 'too-big' : 'ok');
      })
      .catch(() => { if (alive) setState('ok'); });
    return () => { alive = false; };
  }, [rawUrl]);

  if (state === 'checking') return <div style={MUTED}>Loading…</div>;
  if (state === 'too-big') {
    return <div style={MUTED}>This spreadsheet is too large to preview here. Cmd/Ctrl-click the source link to reveal it in Finder.</div>;
  }
  return <SpreadsheetViewer filePath={path} rawUrl={rawUrl} />;
};

/** A cited line range to reveal in the viewer, carried from a #wsfile citation. */
export interface LineTarget {
  startLine?: number;
  endLine?: number;
  lineRev?: number;
}

/** Text-content branch: fetch the non-raw source-file JSON and render by mode. */
const TextContent: React.FC<{ path: string; mode: TextMode; reveal?: LineTarget }> = ({
  path,
  mode,
  reveal,
}) => {
  const [data, setData] = useState<SourceFile | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    // Each file is its own keyed dock panel, so `path` is stable for the life
    // of this instance — state starts null, no synchronous reset needed.
    let alive = true;
    get<SourceFile>(`/api/workspace/source-file?path=${encodeURIComponent(path)}`)
      .then((d) => { if (alive) setData(d); })
      .catch(() => { if (alive) setError('Could not load this file from the workspace or indexed folders.'); });
    return () => { alive = false; };
  }, [path]);

  if (error) return <div style={MUTED}>{error}</div>;
  if (data == null) return <div style={MUTED}>Loading…</div>;
  if (data.kind === 'unsupported') return <div style={MUTED}>{data.message}</div>;
  if (data.kind === 'image') {
    // Defensive: extension routing should catch images before this branch.
    return (
      <div style={{ padding: 12 }}>
        <img src={sourceFileRawUrl(path)} alt={data.name ?? ''} style={{ maxWidth: '100%', height: 'auto', display: 'block' }} />
      </div>
    );
  }
  if (mode === 'csv') return <CSVViewer filePath={path} content={data.content} />;
  if (mode === 'notebook') return <NotebookViewer filePath={path} content={data.content} />;
  if (mode === 'markdown') return <MarkdownPreview filePath={path} content={data.content} />;
  if (mode === 'code') {
    return (
      <MonacoEditor
        filePath={path}
        content={data.content}
        language={getLangForPath(path)}
        readOnly
        revealRange={
          reveal?.startLine
            ? { start: reveal.startLine, end: reveal.endLine ?? reveal.startLine }
            : undefined
        }
        revealRev={reveal?.lineRev}
      />
    );
  }
  // Extraction fallback for rich docs: server-converted markdown (or text).
  if (data.kind === 'markdown') {
    return (
      <div style={{ padding: 12 }}>
        <MarkdownRenderer content={data.content} />
      </div>
    );
  }
  return (
    <pre style={{ margin: 0, padding: 12, whiteSpace: 'pre-wrap', wordBreak: 'break-word', fontFamily: 'var(--font-mono, monospace)', fontSize: 12.5, color: 'var(--text-primary, #eee)' }}>
      {data.content}
    </pre>
  );
};

export const FilePanel: React.FC<{
  path: string;
  startLine?: number;
  endLine?: number;
  lineRev?: number;
}> = ({ path, startLine, endLine, lineRev }) => {
  const ext = extOf(path);
  const rawUrl = sourceFileRawUrl(path);
  const reveal: LineTarget = { startLine, endLine, lineRev };

  // Flash the panel when a citation targets it again (so with several panels
  // stacked the user sees which one answered the click). Skips the first mount.
  const [flash, setFlash] = useState(false);
  const seenRev = useRef<number | undefined>(lineRev);
  useEffect(() => {
    if (lineRev === undefined || lineRev === seenRev.current) return;
    seenRev.current = lineRev;
    setFlash(true);
    const t = setTimeout(() => setFlash(false), 700);
    return () => clearTimeout(t);
  }, [lineRev]);

  let body: React.ReactNode;
  if (ext === 'docx') {
    body = <WordViewer filePath={path} rawUrl={rawUrl} />;
  } else if (ext === 'pdf') {
    body = <PDFViewer filePath={path} rawUrl={rawUrl} />;
  } else if (ext === 'xlsx') {
    body = <GuardedSpreadsheet path={path} rawUrl={rawUrl} />;
  } else if (IMAGE_EXTS.has(ext)) {
    body = <ImageViewer filePath={path} rawUrl={rawUrl} />;
  } else if (ext === 'tiff' || ext === 'tif') {
    body = <div style={MUTED}>{REVEAL_HINT}</div>;
  } else if (ext === 'csv' || ext === 'tsv') {
    body = <TextContent path={path} mode="csv" reveal={reveal} />;
  } else if (ext === 'ipynb') {
    body = <TextContent path={path} mode="notebook" reveal={reveal} />;
  } else if (MARKDOWN_EXTS.has(ext)) {
    body = <TextContent path={path} mode="markdown" reveal={reveal} />;
  } else if (EXTRACTION_EXTS.has(ext)) {
    body = <TextContent path={path} mode="extraction" reveal={reveal} />;
  } else {
    // Code and plain text (incl. extensionless files): read-only Monaco with
    // syntax highlighting — same editor the workspace opens these in. Files
    // the server can't preview (binary/media/oversized) come back as kind
    // 'unsupported' and render the reveal hint instead.
    body = <TextContent path={path} mode="code" reveal={reveal} />;
  }

  return (
    <div
      style={{
        flex: '1 1 auto',
        minHeight: 0,
        overflow: 'auto',
        boxShadow: flash ? 'inset 0 0 0 2px var(--accent, #e2a336)' : 'none',
        transition: 'box-shadow 200ms ease',
      }}
    >
      {body}
    </div>
  );
};

export default FilePanel;
