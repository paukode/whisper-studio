import { describe, it, expect, vi } from 'vitest';
import { JsonRpcConnection } from './lsp/jsonrpc';
import {
  lspToMonacoPosition,
  monacoToLspPosition,
  lspToMonacoRange,
  lspToMonacoSeverity,
  diagnosticsToMarkers,
  lspToMonacoCompletionKind,
  mapCompletionResult,
  mapHoverResult,
  hoverContentsToMarkdown,
  type MarkerSeverityEnum,
  type CompletionItemKindEnum,
} from './lsp/mapping';
import { toLspLanguage } from './useLsp';
import { fileUriFor, pathToFileUri, joinWorkspacePath, fileUriMatches } from './lsp/uri';

// Monaco enum stand-ins (the real values Monaco exposes at runtime).
const MARKER_SEVERITY: MarkerSeverityEnum = { Hint: 1, Info: 2, Warning: 4, Error: 8 };
const COMPLETION_KIND: CompletionItemKindEnum = {
  Method: 0,
  Function: 1,
  Constructor: 2,
  Field: 3,
  Variable: 4,
  Class: 5,
  Struct: 6,
  Interface: 7,
  Module: 8,
  Property: 9,
  Event: 10,
  Operator: 11,
  Unit: 12,
  Value: 13,
  Constant: 14,
  Enum: 15,
  EnumMember: 16,
  Keyword: 17,
  Text: 18,
  Color: 19,
  File: 20,
  Reference: 21,
  Folder: 23,
  TypeParameter: 24,
  Snippet: 27,
};

describe('JsonRpcConnection', () => {
  it('resolves a request when the matching response arrives', async () => {
    const sent: string[] = [];
    const conn = new JsonRpcConnection((data) => sent.push(data));

    const promise = conn.request('textDocument/hover', { line: 1 });

    // One frame went out with an auto-assigned id.
    expect(sent).toHaveLength(1);
    const outbound = JSON.parse(sent[0]);
    expect(outbound).toMatchObject({ jsonrpc: '2.0', method: 'textDocument/hover' });
    expect(typeof outbound.id).toBe('number');

    // Server replies with the same id.
    conn.handleMessage(JSON.stringify({ jsonrpc: '2.0', id: outbound.id, result: { contents: 'hi' } }));

    await expect(promise).resolves.toEqual({ contents: 'hi' });
    expect(conn.pendingCount).toBe(0);
  });

  it('ignores a response whose id does not match any pending request', async () => {
    const conn = new JsonRpcConnection(() => {});
    const promise = conn.request('initialize');
    const settled = vi.fn();
    void promise.then(settled, settled);

    conn.handleMessage(JSON.stringify({ jsonrpc: '2.0', id: 9999, result: {} }));
    await Promise.resolve();

    expect(settled).not.toHaveBeenCalled();
    expect(conn.pendingCount).toBe(1);
  });

  it('rejects a request when an error response arrives', async () => {
    const sent: string[] = [];
    const conn = new JsonRpcConnection((data) => sent.push(data));

    const promise = conn.request('textDocument/completion');
    const { id } = JSON.parse(sent[0]);

    conn.handleMessage(
      JSON.stringify({ jsonrpc: '2.0', id, error: { code: -32601, message: 'Method not found' } }),
    );

    await expect(promise).rejects.toThrow('Method not found');
    expect(conn.pendingCount).toBe(0);
  });

  it('rejects a request on timeout', async () => {
    vi.useFakeTimers();
    const conn = new JsonRpcConnection(() => {}, { timeoutMs: 1000 });
    const promise = conn.request('slow');
    const assertion = expect(promise).rejects.toThrow('timed out');
    vi.advanceTimersByTime(1000);
    await assertion;
    vi.useRealTimers();
  });

  it('assigns unique, incrementing ids across requests', () => {
    const sent: string[] = [];
    const conn = new JsonRpcConnection((data) => sent.push(data));
    conn.request('a');
    conn.request('b');
    const ids = sent.map((s) => JSON.parse(s).id);
    expect(ids[0]).not.toBe(ids[1]);
    expect(ids[1]).toBe(ids[0] + 1);
  });

  it('routes a publishDiagnostics notification to onNotification', () => {
    const conn = new JsonRpcConnection(() => {});
    const onNotification = vi.fn();
    conn.onNotification = onNotification;

    const params = { uri: 'file:///x.py', diagnostics: [] };
    conn.handleMessage(JSON.stringify({ jsonrpc: '2.0', method: 'textDocument/publishDiagnostics', params }));

    expect(onNotification).toHaveBeenCalledWith('textDocument/publishDiagnostics', params);
  });

  it('routes a server-initiated request (id + method) to onServerRequest', () => {
    const conn = new JsonRpcConnection(() => {});
    const onServerRequest = vi.fn();
    const onNotification = vi.fn();
    conn.onServerRequest = onServerRequest;
    conn.onNotification = onNotification;

    conn.handleMessage(JSON.stringify({ jsonrpc: '2.0', id: 7, method: 'workspace/configuration', params: {} }));

    expect(onServerRequest).toHaveBeenCalledWith(7, 'workspace/configuration', {});
    expect(onNotification).not.toHaveBeenCalled();
  });

  it('ignores malformed (non-JSON) frames without throwing', () => {
    const conn = new JsonRpcConnection(() => {});
    conn.onNotification = vi.fn();
    expect(() => conn.handleMessage('not json {')).not.toThrow();
    expect(conn.onNotification).not.toHaveBeenCalled();
  });

  it('rejects all in-flight requests on dispose', async () => {
    const conn = new JsonRpcConnection(() => {});
    const promise = conn.request('pending');
    conn.dispose();
    await expect(promise).rejects.toThrow('closed');
    expect(conn.pendingCount).toBe(0);
  });

  it('rejects when the transport throws while sending', async () => {
    const conn = new JsonRpcConnection(() => {
      throw new Error('socket closed');
    });
    await expect(conn.request('x')).rejects.toThrow('socket closed');
    expect(conn.pendingCount).toBe(0);
  });
});

describe('position & range mapping (0-based LSP <-> 1-based Monaco)', () => {
  it('converts an LSP position to a Monaco position', () => {
    expect(lspToMonacoPosition({ line: 0, character: 0 })).toEqual({ lineNumber: 1, column: 1 });
    expect(lspToMonacoPosition({ line: 4, character: 2 })).toEqual({ lineNumber: 5, column: 3 });
  });

  it('converts a Monaco position back to an LSP position', () => {
    expect(monacoToLspPosition(1, 1)).toEqual({ line: 0, character: 0 });
    expect(monacoToLspPosition(5, 3)).toEqual({ line: 4, character: 2 });
  });

  it('round-trips a position', () => {
    const monaco = { lineNumber: 12, column: 7 };
    const lsp = monacoToLspPosition(monaco.lineNumber, monaco.column);
    expect(lspToMonacoPosition(lsp)).toEqual(monaco);
  });

  it('converts an LSP range to a 1-based Monaco range', () => {
    expect(
      lspToMonacoRange({ start: { line: 2, character: 4 }, end: { line: 2, character: 9 } }),
    ).toEqual({ startLineNumber: 3, startColumn: 5, endLineNumber: 3, endColumn: 10 });
  });
});

describe('diagnostic severity mapping', () => {
  it('maps LSP severities 1..4 to Monaco Error/Warning/Info/Hint', () => {
    expect(lspToMonacoSeverity(1, MARKER_SEVERITY)).toBe(MARKER_SEVERITY.Error);
    expect(lspToMonacoSeverity(2, MARKER_SEVERITY)).toBe(MARKER_SEVERITY.Warning);
    expect(lspToMonacoSeverity(3, MARKER_SEVERITY)).toBe(MARKER_SEVERITY.Info);
    expect(lspToMonacoSeverity(4, MARKER_SEVERITY)).toBe(MARKER_SEVERITY.Hint);
  });

  it('treats an absent severity as Error', () => {
    expect(lspToMonacoSeverity(undefined, MARKER_SEVERITY)).toBe(MARKER_SEVERITY.Error);
  });

  it('maps a diagnostic to a Monaco marker with 1-based coordinates', () => {
    const markers = diagnosticsToMarkers(
      [
        {
          range: { start: { line: 0, character: 0 }, end: { line: 0, character: 5 } },
          severity: 1,
          message: 'undefined name',
          source: 'pyflakes',
          code: 'F821',
        },
      ],
      MARKER_SEVERITY,
    );
    expect(markers).toEqual([
      {
        startLineNumber: 1,
        startColumn: 1,
        endLineNumber: 1,
        endColumn: 6,
        severity: MARKER_SEVERITY.Error,
        message: 'undefined name',
        source: 'pyflakes',
        code: 'F821',
      },
    ]);
  });

  it('stringifies a numeric diagnostic code', () => {
    const [marker] = diagnosticsToMarkers(
      [{ range: { start: { line: 0, character: 0 }, end: { line: 0, character: 1 } }, message: 'x', code: 42 }],
      MARKER_SEVERITY,
    );
    expect(marker.code).toBe('42');
  });
});

describe('completion mapping', () => {
  it('maps LSP completion kinds to the differently-numbered Monaco kinds', () => {
    // LSP Function=3 -> Monaco Function=1; LSP Text=1 -> Monaco Text=18.
    expect(lspToMonacoCompletionKind(3, COMPLETION_KIND)).toBe(COMPLETION_KIND.Function);
    expect(lspToMonacoCompletionKind(1, COMPLETION_KIND)).toBe(COMPLETION_KIND.Text);
    expect(lspToMonacoCompletionKind(7, COMPLETION_KIND)).toBe(COMPLETION_KIND.Class);
    expect(lspToMonacoCompletionKind(undefined, COMPLETION_KIND)).toBe(COMPLETION_KIND.Text);
  });

  const range = { startLineNumber: 1, startColumn: 1, endLineNumber: 1, endColumn: 3 };

  it('maps a CompletionList to Monaco suggestions', () => {
    const items = mapCompletionResult(
      { isIncomplete: false, items: [{ label: 'print', kind: 3, detail: 'builtin' }] },
      COMPLETION_KIND,
      range,
    );
    expect(items).toEqual([
      {
        label: 'print',
        kind: COMPLETION_KIND.Function,
        insertText: 'print',
        detail: 'builtin',
        documentation: undefined,
        range,
      },
    ]);
  });

  it('maps a bare CompletionItem[] and falls back insertText to label', () => {
    const items = mapCompletionResult([{ label: 'os' }], COMPLETION_KIND, range);
    expect(items[0].insertText).toBe('os');
    expect(items[0].kind).toBe(COMPLETION_KIND.Text);
  });

  it('returns [] for a null completion result', () => {
    expect(mapCompletionResult(null, COMPLETION_KIND, range)).toEqual([]);
  });
});

describe('hover mapping', () => {
  it('flattens string, MarkupContent, and arrays into markdown', () => {
    expect(hoverContentsToMarkdown('plain')).toBe('plain');
    expect(hoverContentsToMarkdown({ kind: 'markdown', value: '**bold**' })).toBe('**bold**');
    expect(
      hoverContentsToMarkdown(['a', { kind: 'markdown', value: 'b' }]),
    ).toBe('a\n\nb');
  });

  it('maps a hover result to a Monaco Hover with range', () => {
    const hover = mapHoverResult({
      contents: { kind: 'markdown', value: 'docs' },
      range: { start: { line: 1, character: 0 }, end: { line: 1, character: 4 } },
    });
    expect(hover).toEqual({
      contents: [{ value: 'docs' }],
      range: { startLineNumber: 2, startColumn: 1, endLineNumber: 2, endColumn: 5 },
    });
  });

  it('returns null for an empty or missing hover', () => {
    expect(mapHoverResult(null)).toBeNull();
    expect(mapHoverResult({ contents: '' })).toBeNull();
  });
});

describe('toLspLanguage', () => {
  it('passes through supported languages', () => {
    expect(toLspLanguage('python')).toBe('python');
    expect(toLspLanguage('typescript')).toBe('typescript');
    expect(toLspLanguage('javascript')).toBe('javascript');
  });

  it('returns null for unsupported languages', () => {
    expect(toLspLanguage('markdown')).toBeNull();
    expect(toLspLanguage('plaintext')).toBeNull();
    expect(toLspLanguage('json')).toBeNull();
  });
});

describe('file URI construction', () => {
  it('joins a workspace root with a relative path', () => {
    expect(joinWorkspacePath('/home/me/proj', 'src/app.py')).toBe('/home/me/proj/src/app.py');
    expect(joinWorkspacePath('/home/me/proj/', '/src/app.py')).toBe('/home/me/proj/src/app.py');
  });

  it('builds a percent-encoded file:// URI', () => {
    expect(pathToFileUri('/home/me/a b.py')).toBe('file:///home/me/a%20b.py');
    expect(fileUriFor('/home/me/proj', 'src/app.py')).toBe('file:///home/me/proj/src/app.py');
  });

  it('matches URIs tolerantly across encoding differences', () => {
    expect(fileUriMatches('file:///a/b.py', 'file:///a/b.py')).toBe(true);
    // Same file, different space encoding (%20 vs literal space).
    expect(fileUriMatches('file:///a/x%20y.py', 'file:///a/x y.py')).toBe(true);
    expect(fileUriMatches('file:///a/b.py', 'file:///a/c.py')).toBe(false);
  });
});
