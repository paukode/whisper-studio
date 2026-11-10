import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// The real transport (src/test/setup.ts stands in a fetch-backed one).
vi.unmock('@/api/streamSocket');

import { SocketEventSource, streamFetch } from './streamSocket';

/** A WebSocket the test plays the server for. */
class FakeSocket {
  static instances: FakeSocket[] = [];
  binaryType = 'blob';
  sent: string[] = [];
  closed = false;
  onopen: (() => void) | null = null;
  onmessage: ((ev: { data: unknown }) => void) | null = null;
  onclose: ((ev: { code: number }) => void) | null = null;

  constructor(readonly url: string) {
    FakeSocket.instances.push(this);
  }
  send(data: string) { this.sent.push(data); }
  close() {
    if (this.closed) return;
    this.closed = true;
    this.onclose?.({ code: 1005 });
  }
  // The server side.
  open() { this.onopen?.(); }
  head(status: number, headers: [string, string][] = [['content-type', 'text/event-stream']]) {
    this.onmessage?.({ data: JSON.stringify({ status, headers }) });
  }
  chunk(text: string) { this.onmessage?.({ data: new TextEncoder().encode(text).buffer }); }
  end(code = 1000) { this.closed = true; this.onclose?.({ code }); }
}

const flush = () => new Promise((r) => setTimeout(r, 0));
const last = () => FakeSocket.instances[FakeSocket.instances.length - 1];

beforeEach(() => {
  FakeSocket.instances = [];
  vi.stubGlobal('WebSocket', FakeSocket);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe('streamFetch', () => {
  it('sends the request head and body, and streams the response back', async () => {
    const pending = streamFetch('/api/chat', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{"q":1}' });
    const ws = last();
    expect(ws.url).toMatch(/^ws:\/\/[^/]+\/ws\/stream$/);
    ws.open();
    expect(JSON.parse(ws.sent[0])).toEqual({ method: 'POST', path: '/api/chat', headers: { 'Content-Type': 'application/json' }, chunks: 1 });
    expect(ws.sent[1]).toBe('{"q":1}');

    ws.head(200);
    const response = await pending;
    ws.chunk('data: {"a":1}\n\n');
    ws.chunk('data: [DONE]\n\n');
    ws.end();

    expect(response.status).toBe(200);
    expect(response.headers.get('content-type')).toBe('text/event-stream');
    expect(await response.text()).toBe('data: {"a":1}\n\ndata: [DONE]\n\n');
  });

  it('keeps an error status and its JSON body, as fetch does', async () => {
    const pending = streamFetch('/api/chat', { method: 'POST', body: '{}' });
    last().open();
    last().head(409, [['content-type', 'application/json']]);
    last().chunk('{"error":"busy"}');
    last().end();

    const response = await pending;
    expect(response.ok).toBe(false);
    expect(await response.json()).toEqual({ error: 'busy' });
  });

  it('splits a long body into frames without cutting a character in two', async () => {
    const body = `${'a'.repeat(256 * 1024 - 1)}😀${'b'.repeat(10)}`;
    void streamFetch('/api/chat', { method: 'POST', body });
    last().open();

    const head = JSON.parse(last().sent[0]) as { chunks: number };
    const frames = last().sent.slice(1);
    expect(frames).toHaveLength(head.chunks);
    expect(frames.join('')).toBe(body);
    expect(frames[0].endsWith('a')).toBe(true);
  });

  it('rejects like fetch when the app cannot be reached', async () => {
    const pending = streamFetch('/api/chat');
    last().end(1006);
    await expect(pending).rejects.toBeInstanceOf(TypeError);
  });

  it('aborts with an AbortError and closes the socket, before and after the head', async () => {
    const early = new AbortController();
    const before = streamFetch('/api/chat', { signal: early.signal });
    early.abort();
    await expect(before).rejects.toMatchObject({ name: 'AbortError' });
    expect(last().closed).toBe(true);

    const late = new AbortController();
    const after = streamFetch('/api/chat', { signal: late.signal });
    last().open();
    last().head(200);
    const reader = (await after).body!.getReader();
    late.abort();
    await expect(reader.read()).rejects.toMatchObject({ name: 'AbortError' });
    expect(last().closed).toBe(true);
  });

  it('fails the body when the stream ends without a normal close', async () => {
    const pending = streamFetch('/api/chat');
    last().open();
    last().head(200);
    const response = await pending;
    last().chunk('data: 1\n\n');
    last().end(1011);
    await expect(response.text()).rejects.toBeInstanceOf(TypeError);
  });
});

describe('SocketEventSource', () => {
  it('opens, delivers each event, and skips comments and named events', async () => {
    const es = new SocketEventSource('/api/sessions/events');
    const opened = vi.fn();
    const got: string[] = [];
    es.onopen = opened;
    es.onmessage = (ev) => got.push(ev.data);
    last().open();
    last().head(200);
    await flush();
    last().chunk(': keep-alive\n\ndata: {"a":1}\n\nevent: other\ndata: x\n\ndata: line1\ndata: line2\n\n');
    await flush();

    expect(opened).toHaveBeenCalledTimes(1);
    expect(es.readyState).toBe(SocketEventSource.OPEN);
    expect(got).toEqual(['{"a":1}', 'line1\nline2']);
    es.close();
  });

  it('reconnects after a drop, waiting the retry the server set', async () => {
    vi.useFakeTimers();
    const es = new SocketEventSource('/api/git/events');
    const errors = vi.fn();
    es.onerror = errors;
    last().open();
    last().head(200);
    await vi.advanceTimersByTimeAsync(0);
    last().chunk('retry: 500\n\n');
    await vi.advanceTimersByTimeAsync(0);
    last().end(1006);
    await vi.advanceTimersByTimeAsync(0);

    expect(errors).toHaveBeenCalledTimes(1);
    expect(es.readyState).toBe(SocketEventSource.CONNECTING);
    expect(FakeSocket.instances).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(500);
    expect(FakeSocket.instances).toHaveLength(2);
    es.close();
  });

  it('closes for good on a refused request, and close() stops reconnects', async () => {
    vi.useFakeTimers();
    const refused = new SocketEventSource('/api/sessions/events');
    last().open();
    last().head(403, [['content-type', 'application/json']]);
    await vi.advanceTimersByTimeAsync(0);
    expect(refused.readyState).toBe(SocketEventSource.CLOSED);

    const closed = new SocketEventSource('/api/git/events');
    closed.close();
    await vi.advanceTimersByTimeAsync(10_000);
    expect(FakeSocket.instances).toHaveLength(2);
    expect(last().closed).toBe(true);
  });
});
