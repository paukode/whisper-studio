import { useEffect, useState } from 'react';
import type { editor, languages, IDisposable } from 'monaco-editor';
import type {
  LspCompletionResult,
  LspHoverResult,
  LspPublishDiagnosticsParams,
} from '@/types/lsp';
import { JsonRpcConnection } from './lsp/jsonrpc';
import {
  diagnosticsToMarkers,
  mapCompletionResult,
  mapHoverResult,
  monacoToLspPosition,
} from './lsp/mapping';
import { fileUriFor, fileUriMatches, pathToFileUri } from './lsp/uri';

/** Monaco instance type (what @monaco-editor/react hands to onMount). */
type Monaco = typeof import('monaco-editor');

/** Connection status surfaced to the UI for the status dot. */
export type LspStatus = 'connecting' | 'connected' | 'error' | 'closed';

/** Marker owner used with setModelMarkers so we only ever clear our own markers. */
const LSP_MARKER_OWNER = 'lsp';

/** Debounce for didChange notifications (ms). */
const DID_CHANGE_DEBOUNCE_MS = 250;

/** Monaco language ids the backend proxy can serve, in LSP languageId form. */
const SUPPORTED_LANGUAGES = new Set(['python', 'typescript', 'javascript']);

/**
 * Map a Monaco language id to the proxy's LSP language segment, or null when the
 * language has no language server (the hook then no-ops).
 */
export function toLspLanguage(monacoLanguage: string): string | null {
  return SUPPORTED_LANGUAGES.has(monacoLanguage) ? monacoLanguage : null;
}

export interface UseLspParams {
  /** The Monaco namespace (from onMount). Null until the editor has mounted. */
  monaco: Monaco | null;
  /** The mounted editor. Null until the editor has mounted. */
  editorInstance: editor.IStandaloneCodeEditor | null;
  /** Monaco language id of the active file (e.g. 'python'). */
  language: string;
  /** Workspace-relative path of the active file (e.g. 'src/app.py'). */
  filePath: string;
  /** Absolute workspace root (uiStore.wsPath). Empty when no workspace. */
  workspacePath: string;
  /** Whether a workspace is connected. When false the hook no-ops. */
  enabled: boolean;
}

export interface UseLspReturn {
  /** Connection status for the status indicator. */
  status: LspStatus;
  /** Whether a language server is applicable here (supported language + workspace). */
  active: boolean;
}

/** Capabilities we advertise. Full-text sync keeps didChange trivial and robust. */
function clientCapabilities(): Record<string, unknown> {
  return {
    textDocument: {
      synchronization: { dynamicRegistration: false, didSave: false, willSave: false },
      completion: {
        dynamicRegistration: false,
        completionItem: { snippetSupport: false, documentationFormat: ['markdown', 'plaintext'] },
      },
      hover: { dynamicRegistration: false, contentFormat: ['markdown', 'plaintext'] },
      publishDiagnostics: { relatedInformation: false },
    },
    workspace: { configuration: true, workspaceFolders: true },
  };
}

/**
 * A Monaco language client that tunnels to the backend LSP proxy
 * (server/lsp_proxy.py) over a WebSocket. It performs the JSON-RPC handshake,
 * keeps the document in sync, surfaces diagnostics as Monaco markers, and
 * registers completion + hover providers. Everything is torn down on unmount,
 * language change, file change, or socket close.
 *
 * The proxy speaks raw JSON-RPC bodies on the socket (Content-Length framing is
 * handled on its stdio side), so we send/receive plain JSON strings here.
 */
export function useLsp(params: UseLspParams): UseLspReturn {
  const { monaco, editorInstance, language, filePath, workspacePath, enabled } = params;
  // Tracks only the live connection lifecycle. The inactive 'closed' state is
  // derived below so the effect never sets state synchronously in its body.
  const [connStatus, setConnStatus] = useState<LspStatus>('closed');

  const lspLanguage = toLspLanguage(language);
  const active = Boolean(enabled && workspacePath && lspLanguage);

  useEffect(() => {
    if (!active || !monaco || !editorInstance || !filePath || !lspLanguage) {
      return;
    }
    const model = editorInstance.getModel();
    if (!model) {
      return;
    }

    let disposed = false;
    let initialized = false;
    let version = 1;
    let debounceTimer: ReturnType<typeof setTimeout> | undefined;
    const disposables: IDisposable[] = [];

    const fileUri = fileUriFor(workspacePath, filePath);
    const rootUri = pathToFileUri(workspacePath);

    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const url = `${proto}//${window.location.host}/ws/lsp/${lspLanguage}?workspace=${encodeURIComponent(workspacePath)}`;

    // Announce 'connecting' off the synchronous effect path (a fresh connection
    // may follow a stale 'error'/'closed' from a prior run within this mount).
    queueMicrotask(() => {
      if (!disposed) setConnStatus('connecting');
    });
    const socket = new WebSocket(url);

    const conn = new JsonRpcConnection((data) => {
      if (socket.readyState === WebSocket.OPEN) socket.send(data);
    });

    conn.onNotification = (method, rawParams) => {
      if (method !== 'textDocument/publishDiagnostics') return;
      const p = rawParams as LspPublishDiagnosticsParams | undefined;
      if (!p || !fileUriMatches(p.uri, fileUri)) return; // ignore diagnostics for other documents
      const markers = diagnosticsToMarkers(p.diagnostics ?? [], monaco.MarkerSeverity);
      monaco.editor.setModelMarkers(model, LSP_MARKER_OWNER, markers as editor.IMarkerData[]);
    };

    // Servers may issue requests during startup (registerCapability,
    // workspace/configuration, progress). Answer them so the server isn't
    // blocked; we don't consume the results.
    conn.onServerRequest = (id, method) => {
      if (method === 'workspace/configuration') {
        conn.respond(id, [null]);
      } else {
        conn.respond(id, null);
      }
    };

    socket.onmessage = (ev: MessageEvent) => {
      conn.handleMessage(typeof ev.data === 'string' ? ev.data : String(ev.data));
    };
    socket.onerror = () => {
      if (!disposed) setConnStatus('error');
    };
    socket.onclose = () => {
      if (!disposed) setConnStatus('closed');
    };
    socket.onopen = () => {
      conn
        .request('initialize', {
          processId: null,
          clientInfo: { name: 'whisper-studio' },
          rootUri,
          rootPath: workspacePath,
          workspaceFolders: [{ uri: rootUri, name: 'workspace' }],
          capabilities: clientCapabilities(),
        })
        .then(() => {
          if (disposed) return;
          conn.notify('initialized', {});
          initialized = true;
          conn.notify('textDocument/didOpen', {
            textDocument: {
              uri: fileUri,
              languageId: lspLanguage,
              version,
              text: model.getValue(),
            },
          });
          setConnStatus('connected');
        })
        .catch(() => {
          if (!disposed) setConnStatus('error');
        });
    };

    // Full-document sync on edits, debounced.
    const changeSub = model.onDidChangeContent(() => {
      if (!initialized) return;
      if (debounceTimer) clearTimeout(debounceTimer);
      debounceTimer = setTimeout(() => {
        version += 1;
        conn.notify('textDocument/didChange', {
          textDocument: { uri: fileUri, version },
          contentChanges: [{ text: model.getValue() }],
        });
      }, DID_CHANGE_DEBOUNCE_MS);
    });
    disposables.push(changeSub);

    // Completion provider (scoped to this model).
    const completionProvider = monaco.languages.registerCompletionItemProvider(language, {
      triggerCharacters: ['.'],
      provideCompletionItems: async (m, position) => {
        if (m !== model || disposed) return { suggestions: [] };
        try {
          const result = await conn.request<LspCompletionResult>('textDocument/completion', {
            textDocument: { uri: fileUri },
            position: monacoToLspPosition(position.lineNumber, position.column),
          });
          const word = m.getWordUntilPosition(position);
          const range = {
            startLineNumber: position.lineNumber,
            startColumn: word.startColumn,
            endLineNumber: position.lineNumber,
            endColumn: word.endColumn,
          };
          const suggestions = mapCompletionResult(
            result,
            monaco.languages.CompletionItemKind as unknown as Record<string, number>,
            range,
          );
          return { suggestions: suggestions as unknown as languages.CompletionItem[] };
        } catch {
          return { suggestions: [] };
        }
      },
    });
    disposables.push(completionProvider);

    // Hover provider (scoped to this model).
    const hoverProvider = monaco.languages.registerHoverProvider(language, {
      provideHover: async (m, position) => {
        if (m !== model || disposed) return null;
        try {
          const result = await conn.request<LspHoverResult | null>('textDocument/hover', {
            textDocument: { uri: fileUri },
            position: monacoToLspPosition(position.lineNumber, position.column),
          });
          return mapHoverResult(result) as languages.Hover | null;
        } catch {
          return null;
        }
      },
    });
    disposables.push(hoverProvider);

    return () => {
      disposed = true;
      if (debounceTimer) clearTimeout(debounceTimer);
      for (const d of disposables) {
        try {
          d.dispose();
        } catch {
          /* provider already gone */
        }
      }
      try {
        monaco.editor.setModelMarkers(model, LSP_MARKER_OWNER, []);
      } catch {
        /* model already disposed */
      }
      // Best-effort graceful shutdown. Queued frames are flushed before the
      // socket closes, so we don't await the shutdown response.
      if (socket.readyState === WebSocket.OPEN && initialized) {
        try {
          socket.send(JSON.stringify({ jsonrpc: '2.0', id: -1, method: 'shutdown' }));
          socket.send(JSON.stringify({ jsonrpc: '2.0', method: 'exit' }));
        } catch {
          /* socket already closing */
        }
      }
      conn.dispose();
      try {
        socket.close();
      } catch {
        /* already closed */
      }
    };
  }, [active, monaco, editorInstance, language, lspLanguage, filePath, workspacePath]);

  // When inactive the connection is definitionally closed; otherwise reflect the
  // live socket lifecycle.
  const status: LspStatus = active ? connStatus : 'closed';
  return { status, active };
}
