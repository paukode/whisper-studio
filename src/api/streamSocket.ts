/**
 * The app's long requests, carried over a WebSocket each (server/stream_tunnel.py).
 *
 * The Mac app's web view keeps at most 6 HTTP connections to the app, and a
 * 7th request waits until one is free (measured in a WKWebView on
 * 2026-11-10). WebSockets do not count against that limit. Every long
 * request goes through here: the chat replies and approval resumes, the
 * session, git and workflow event streams, model loads, /compact and the
 * subagent stream. The HTTP connections then serve only short requests and
 * uploads, so loading a session never waits behind three running replies.
 *
 * streamFetch is fetch() for one request: it resolves to a real Response
 * whose body streams in as the server writes it, and its signal aborts like
 * fetch's (an AbortError, and the socket closes, which the server reads as
 * the client going away). SocketEventSource is EventSource over the same
 * transport, with EventSource's reconnect.
 */

/** Characters per text frame of a request body: a long chat history stays
 *  well under the server's frame size limit. */
const BODY_CHUNK = 256 * 1024;

export interface StreamInit {
  method?: string;
  headers?: Record<string, string>;
  body?: string;
  signal?: AbortSignal | null;
}

function socketUrl(): string {
  const scheme = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${scheme}//${window.location.host}/ws/stream`;
}

const abortError = () => new DOMException('The operation was aborted.', 'AbortError');

/** Split a body into frames without cutting a surrogate pair in two. */
function bodyChunks(text: string): string[] {
  const chunks: string[] = [];
  for (let i = 0; i < text.length;) {
    let end = Math.min(i + BODY_CHUNK, text.length);
    const last = text.charCodeAt(end - 1);
    if (end < text.length && last >= 0xd800 && last <= 0xdbff) end -= 1;
    chunks.push(text.slice(i, end));
    i = end;
  }
  return chunks;
}

const NULL_BODY_STATUS = new Set([101, 204, 205, 304]);

/** fetch() over a WebSocket. Rejects like fetch: a TypeError when the app
 *  cannot be reached, an AbortError when `signal` aborts. */
export function streamFetch(path: string, init: StreamInit = {}): Promise<Response> {
  return new Promise<Response>((resolve, reject) => {
    const { signal } = init;
    if (signal?.aborted) {
      reject(abortError());
      return;
    }
    const ws = new WebSocket(socketUrl());
    ws.binaryType = 'arraybuffer';
    let body: ReadableStreamDefaultController<Uint8Array> | null = null;
    let headed = false;
    let done = false;

    const finish = (err?: Error) => {
      if (done) return;
      done = true;
      signal?.removeEventListener('abort', onAbort);
      if (!headed) reject(err ?? new TypeError('Failed to fetch'));
      else if (err) body?.error(err);
      else body?.close();
    };
    const onAbort = () => {
      finish(abortError());
      ws.close();
    };
    signal?.addEventListener('abort', onAbort, { once: true });

    ws.onopen = () => {
      const chunks = bodyChunks(init.body ?? '');
      ws.send(JSON.stringify({
        method: init.method ?? 'GET',
        path,
        headers: init.headers ?? {},
        chunks: chunks.length,
      }));
      for (const chunk of chunks) ws.send(chunk);
    };
    ws.onmessage = (event: MessageEvent) => {
      if (done) return;
      if (typeof event.data === 'string') {
        if (headed) return;
        const head = JSON.parse(event.data) as { status: number; headers: [string, string][] };
        headed = true;
        const stream = new ReadableStream<Uint8Array>({
          start(controller) { body = controller; },
          // The reader let go (a cancelled read): nothing more is wanted.
          cancel() { done = true; ws.close(); },
        });
        resolve(new Response(NULL_BODY_STATUS.has(head.status) ? null : stream, {
          status: head.status,
          headers: head.headers,
        }));
        return;
      }
      body?.enqueue(new Uint8Array(event.data as ArrayBuffer));
    };
    ws.onclose = (event: CloseEvent) => {
      // 1000 after the head: the server wrote the whole response.
      if (headed && event.code === 1000) finish();
      else finish(new TypeError(headed ? `The stream ended early (code ${event.code})` : 'Failed to fetch'));
    };
  });
}

/** One event of a text/event-stream body: its data lines, the event type and
 *  a new reconnect delay when the server sent one. */
interface SseEvent {
  data: string;
  type: string;
  retry?: number;
}

/** Read a text/event-stream body event by event (the EventSource parsing
 *  rules: data lines join with a newline, comment lines are dropped). */
async function readEvents(body: ReadableStream<Uint8Array>, onEvent: (ev: SseEvent) => void): Promise<void> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let data: string[] = [];
  let type = '';
  let retry: number | undefined;
  const line = (raw: string) => {
    if (raw === '') {
      if (data.length > 0 || retry !== undefined) onEvent({ data: data.join('\n'), type, retry });
      data = [];
      type = '';
      retry = undefined;
      return;
    }
    if (raw.startsWith(':')) return;
    const colon = raw.indexOf(':');
    const field = colon < 0 ? raw : raw.slice(0, colon);
    let value = colon < 0 ? '' : raw.slice(colon + 1);
    if (value.startsWith(' ')) value = value.slice(1);
    if (field === 'data') data.push(value);
    else if (field === 'event') type = value;
    else if (field === 'retry' && /^\d+$/.test(value)) retry = Number(value);
  };
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split(/\r\n|\r|\n/);
    buffer = lines.pop() ?? '';
    lines.forEach(line);
  }
}

/** The EventSource interface the app uses, over streamFetch. Reconnects like
 *  EventSource: after a dropped or ended stream it waits (3 s, or the
 *  server's `retry:`) and opens it again; a refused request (not 2xx) closes
 *  it for good. */
export class SocketEventSource {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSED = 2;

  readyState: number = SocketEventSource.CONNECTING;
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onerror: (() => void) | null = null;

  private abort: AbortController | null = null;
  private retryMs = 3000;
  private timer: ReturnType<typeof setTimeout> | null = null;

  constructor(readonly url: string) {
    this.connect();
  }

  close(): void {
    this.readyState = SocketEventSource.CLOSED;
    this.abort?.abort();
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
  }

  private connect(): void {
    const abort = new AbortController();
    this.abort = abort;
    void (async () => {
      try {
        const response = await streamFetch(this.url, { signal: abort.signal, headers: { Accept: 'text/event-stream' } });
        if (this.readyState === SocketEventSource.CLOSED) return;
        if (!response.ok || !response.body) {
          this.readyState = SocketEventSource.CLOSED;
          this.onerror?.();
          return;
        }
        this.readyState = SocketEventSource.OPEN;
        this.onopen?.();
        await readEvents(response.body, (ev) => {
          if (ev.retry !== undefined) this.retryMs = ev.retry;
          if (this.readyState !== SocketEventSource.OPEN) return;
          if (ev.type === '' || ev.type === 'message') this.onmessage?.({ data: ev.data });
        });
      } catch {
        // Dropped: reconnect below, unless it was closed on purpose.
      }
      if (this.readyState === SocketEventSource.CLOSED) return;
      this.readyState = SocketEventSource.CONNECTING;
      this.onerror?.();
      if (this.readyState === SocketEventSource.CLOSED) return;
      this.timer = setTimeout(() => {
        this.timer = null;
        if (this.readyState !== SocketEventSource.CLOSED) this.connect();
      }, this.retryMs);
    })();
  }
}
