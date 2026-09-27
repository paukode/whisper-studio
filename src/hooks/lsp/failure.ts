/**
 * Why a language server is not running, as the backend proxy reports it.
 *
 * server/lsp_proxy.py sends a JSON-RPC `window/showMessage` notification with
 * type 1 (Error) and `source: 'whisper-studio'` when the server cannot start
 * (pylsp missing, typescript-language-server not installed) or stops on its
 * own, then closes the socket with code 1011 and a shortened copy of the text
 * as the close reason. The notification carries the full text; the close
 * reason is the fallback when only the close arrives. A live language server
 * sends its own `window/showMessage` errors too (a failed request, say), and
 * those carry no marker: they are not the end of the connection.
 */

/** LSP MessageType.Error. */
const MESSAGE_TYPE_ERROR = 1;

/** The `source` the proxy puts on its own failure notification. */
export const PROXY_FAILURE_SOURCE = 'whisper-studio';

/** Normal closure: the editor or the server ended the session on purpose. */
const CLOSE_NORMAL = 1000;

/** The failure text of the proxy's failure notification, else null. */
export function proxyFailureMessage(method: string, params: unknown): string | null {
  if (method !== 'window/showMessage' || !params || typeof params !== 'object') return null;
  const { type, message, source } = params as {
    type?: unknown;
    message?: unknown;
    source?: unknown;
  };
  if (source !== PROXY_FAILURE_SOURCE || type !== MESSAGE_TYPE_ERROR) return null;
  if (typeof message !== 'string' || !message.trim()) return null;
  return message.trim();
}

/** The failure a socket close reports, else null (a normal or silent close). */
export function closeFailureReason(code: number, reason: string): string | null {
  if (code === CLOSE_NORMAL || !reason.trim()) return null;
  return reason.trim();
}
