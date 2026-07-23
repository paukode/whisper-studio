/**
 * A tiny JSON-RPC 2.0 layer over a message-passing transport (a WebSocket, in
 * practice). It is deliberately transport-agnostic: it takes a `send(data)`
 * function and is fed inbound frames via `handleMessage`. That keeps the request
 * correlation logic (id -> pending promise) pure and unit-testable without a
 * live socket.
 *
 * The LSP proxy (server/lsp_proxy.py) speaks raw JSON-RPC bodies over the socket
 * (no Content-Length framing on the browser side), so each frame here is a
 * single JSON object.
 */
import type { JsonRpcMessage } from '@/types/lsp';

/** Sends one serialized JSON-RPC frame over the transport. */
export type JsonRpcSend = (data: string) => void;

interface PendingRequest {
  resolve: (value: unknown) => void;
  reject: (reason: Error) => void;
  timer: ReturnType<typeof setTimeout> | undefined;
}

/** Called for a server -> client notification (a message with `method`, no `id`). */
export type NotificationHandler = (method: string, params: unknown) => void;

/** Called for a server -> client request (a message with both `method` and `id`). */
export type ServerRequestHandler = (id: number, method: string, params: unknown) => void;

export interface JsonRpcOptions {
  /** Per-request timeout in ms; 0 disables the timeout. Defaults to 15s. */
  timeoutMs?: number;
}

/**
 * Correlates outbound requests with their responses by JSON-RPC id and routes
 * inbound notifications / server-initiated requests to handlers.
 */
export class JsonRpcConnection {
  private nextId = 1;
  private pending = new Map<number, PendingRequest>();
  private readonly send: JsonRpcSend;
  private readonly timeoutMs: number;

  /** Set by the owner to receive server -> client notifications. */
  onNotification?: NotificationHandler;
  /** Set by the owner to receive server -> client requests. */
  onServerRequest?: ServerRequestHandler;

  constructor(send: JsonRpcSend, opts: JsonRpcOptions = {}) {
    this.send = send;
    this.timeoutMs = opts.timeoutMs ?? 15000;
  }

  /** Number of in-flight requests awaiting a response (exposed for tests). */
  get pendingCount(): number {
    return this.pending.size;
  }

  /**
   * Send a request and resolve with its `result` when the matching response
   * arrives. Rejects on an error response, on timeout, or if the transport
   * throws while sending.
   */
  request<T = unknown>(method: string, params?: unknown): Promise<T> {
    const id = this.nextId++;
    return new Promise<T>((resolve, reject) => {
      const timer =
        this.timeoutMs > 0
          ? setTimeout(() => {
              this.pending.delete(id);
              reject(new Error(`LSP request timed out: ${method}`));
            }, this.timeoutMs)
          : undefined;
      this.pending.set(id, {
        resolve: resolve as (value: unknown) => void,
        reject,
        timer,
      });
      try {
        this.send(JSON.stringify({ jsonrpc: '2.0', id, method, params }));
      } catch (err) {
        if (timer) clearTimeout(timer);
        this.pending.delete(id);
        reject(err instanceof Error ? err : new Error(String(err)));
      }
    });
  }

  /** Fire-and-forget notification (no id, no response expected). */
  notify(method: string, params?: unknown): void {
    this.send(JSON.stringify({ jsonrpc: '2.0', method, params }));
  }

  /** Reply to a server-initiated request. */
  respond(id: number, result: unknown): void {
    this.send(JSON.stringify({ jsonrpc: '2.0', id, result }));
  }

  /**
   * Dispatch one inbound frame. Accepts a raw JSON string or an already-parsed
   * object. Classification:
   *   - id present, no method  -> response  (resolve/reject the pending request)
   *   - id present, method too -> server request
   *   - method only            -> notification
   * Non-JSON strings and unrecognized shapes are ignored.
   */
  handleMessage(raw: string | JsonRpcMessage): void {
    let msg: JsonRpcMessage;
    if (typeof raw === 'string') {
      try {
        msg = JSON.parse(raw) as JsonRpcMessage;
      } catch {
        return;
      }
    } else {
      msg = raw;
    }

    const hasId = typeof msg.id === 'number';
    const hasMethod = typeof msg.method === 'string';

    if (hasId && !hasMethod) {
      const pending = this.pending.get(msg.id as number);
      if (!pending) return;
      this.pending.delete(msg.id as number);
      if (pending.timer) clearTimeout(pending.timer);
      if (msg.error) {
        pending.reject(new Error(msg.error.message || `LSP error ${msg.error.code}`));
      } else {
        pending.resolve(msg.result);
      }
      return;
    }

    if (hasId && hasMethod) {
      this.onServerRequest?.(msg.id as number, msg.method as string, msg.params);
      return;
    }

    if (hasMethod) {
      this.onNotification?.(msg.method as string, msg.params);
    }
  }

  /** Reject every in-flight request; used when the transport goes away. */
  dispose(reason = 'LSP connection closed'): void {
    for (const pending of this.pending.values()) {
      if (pending.timer) clearTimeout(pending.timer);
      pending.reject(new Error(reason));
    }
    this.pending.clear();
  }
}
