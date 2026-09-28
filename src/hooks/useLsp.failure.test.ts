import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, renderHook } from '@testing-library/react';
import type { editor } from 'monaco-editor';
import { useLsp, type UseLspParams } from './useLsp';
import { closeFailureReason, proxyFailureMessage } from './lsp/failure';

/** What server/lsp_proxy.py sends before it closes a socket it cannot serve. */
const PROXY_FAILURE = {
  jsonrpc: '2.0',
  method: 'window/showMessage',
  params: {
    type: 1,
    message:
      'typescript-language-server is not installed. The app does not ship it: install it with `npm install -g typescript-language-server typescript`, then reopen the file.',
    source: 'whisper-studio',
  },
};

/** A live server's own error popup: the same method and type, no marker. */
const SERVER_ERROR_POPUP = {
  jsonrpc: '2.0',
  method: 'window/showMessage',
  params: { type: 1, message: 'Request textDocument/hover failed.' },
};

class FakeSocket {
  static OPEN = 1;
  static instances: FakeSocket[] = [];
  readyState = 0;
  url: string;
  sent: string[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((ev: { data: string }) => void) | null = null;
  onclose: ((ev: { code: number; reason: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  constructor(url: string) {
    this.url = url;
    FakeSocket.instances.push(this);
  }
  send(data: string) {
    this.sent.push(data);
  }
  close() {
    this.readyState = 3;
  }
}

function lspParams(language = 'typescript'): UseLspParams {
  const model = {
    getValue: () => '',
    onDidChangeContent: () => ({ dispose() {} }),
  };
  const monaco = {
    editor: { setModelMarkers: vi.fn() },
    languages: {
      registerCompletionItemProvider: () => ({ dispose() {} }),
      registerHoverProvider: () => ({ dispose() {} }),
      CompletionItemKind: {},
    },
    MarkerSeverity: { Hint: 1, Info: 2, Warning: 4, Error: 8 },
  };
  return {
    monaco: monaco as unknown as UseLspParams['monaco'],
    editorInstance: { getModel: () => model } as unknown as editor.IStandaloneCodeEditor,
    language,
    filePath: 'src/a.ts',
    workspacePath: '/Users/me/proj',
    enabled: true,
  };
}

beforeEach(() => {
  FakeSocket.instances = [];
  vi.stubGlobal('WebSocket', FakeSocket);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('language server failure reasons', () => {
  it("reads the proxy's marked error notification, and only that", () => {
    expect(proxyFailureMessage('window/showMessage', PROXY_FAILURE.params)).toBe(
      PROXY_FAILURE.params.message,
    );
    expect(proxyFailureMessage('window/showMessage', SERVER_ERROR_POPUP.params)).toBeNull();
    expect(
      proxyFailureMessage('window/showMessage', { ...PROXY_FAILURE.params, type: 3 }),
    ).toBeNull();
    expect(proxyFailureMessage('textDocument/publishDiagnostics', PROXY_FAILURE.params)).toBeNull();
  });

  it("leaves a connected server connected after the server's own error popup", async () => {
    const params = lspParams();
    const { result } = renderHook(() => useLsp(params));
    await act(async () => {});
    const socket = FakeSocket.instances[0];
    await act(async () => {
      socket.readyState = FakeSocket.OPEN;
      socket.onopen?.();
      const initialize = JSON.parse(socket.sent[0]) as { id: number; method: string };
      expect(initialize.method).toBe('initialize');
      socket.onmessage?.({
        data: JSON.stringify({ jsonrpc: '2.0', id: initialize.id, result: { capabilities: {} } }),
      });
    });
    expect(result.current.status).toBe('connected');

    act(() => {
      socket.onmessage?.({ data: JSON.stringify(SERVER_ERROR_POPUP) });
    });
    expect(result.current.status).toBe('connected');
    expect(result.current.failure).toBeNull();
  });

  it('treats a normal or silent close as no failure', () => {
    expect(closeFailureReason(1000, 'bye')).toBeNull();
    expect(closeFailureReason(1006, '')).toBeNull();
    expect(closeFailureReason(1011, 'pylsp is not installed')).toBe('pylsp is not installed');
  });

  it('surfaces the reason the proxy gave and keeps it through the close', async () => {
    // Stable params: fresh monaco/editor objects on every render would re-run
    // the effect and replace the socket.
    const params = lspParams();
    const { result } = renderHook(() => useLsp(params));
    await act(async () => {});
    expect(FakeSocket.instances).toHaveLength(1);
    const socket = FakeSocket.instances[0];
    expect(result.current.status).toBe('connecting');

    act(() => {
      socket.onmessage?.({ data: JSON.stringify(PROXY_FAILURE) });
      socket.onclose?.({ code: 1011, reason: 'typescript-language-server is not installed.' });
    });

    expect(result.current.status).toBe('error');
    // The full notification text, not the shortened close reason.
    expect(result.current.failure).toBe(PROXY_FAILURE.params.message);
  });

  it('falls back to the close reason when only the close arrives', async () => {
    const params = lspParams('python');
    const { result } = renderHook(() => useLsp(params));
    await act(async () => {});
    act(() => {
      FakeSocket.instances[0].onclose?.({ code: 1011, reason: 'The python language server stopped (exit status 3).' });
    });
    expect(result.current.status).toBe('error');
    expect(result.current.failure).toBe('The python language server stopped (exit status 3).');
  });

  it('reads a plain close as off, with no failure', async () => {
    const params = lspParams('python');
    const { result } = renderHook(() => useLsp(params));
    await act(async () => {});
    act(() => {
      FakeSocket.instances[0].onclose?.({ code: 1000, reason: '' });
    });
    expect(result.current.status).toBe('closed');
    expect(result.current.failure).toBeNull();
  });
});
