/**
 * WebSocket mock for jsdom test environment.
 *
 * jsdom does not implement WebSocket. This stub provides the
 * constructor and basic lifecycle so WebSocket-using code (useWebSocket,
 * the chat stream, the Header audio pipeline) can be unit-tested.
 */

class WebSocketMock {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSING = 2;
  static readonly CLOSED = 3;

  readonly CONNECTING = 0;
  readonly OPEN = 1;
  readonly CLOSING = 2;
  readonly CLOSED = 3;

  url: string;
  readyState: number = WebSocketMock.CONNECTING;
  protocol = '';
  extensions = '';
  bufferedAmount = 0;
  binaryType: BinaryType = 'blob';

  onopen: ((event: Event) => void) | null = null;
  onclose: ((event: CloseEvent) => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;

  private sentMessages: (string | ArrayBuffer)[] = [];

  constructor(url: string, _protocols?: string | string[]) {
    this.url = url;
    // Auto-open on next tick to mimic real WebSocket
    setTimeout(() => {
      this.readyState = WebSocketMock.OPEN;
      this.onopen?.(new Event('open'));
    }, 0);
  }

  send(data: string | ArrayBuffer): void {
    if (this.readyState !== WebSocketMock.OPEN) {
      throw new DOMException('WebSocket is not open', 'InvalidStateError');
    }
    this.sentMessages.push(data);
  }

  close(code?: number, reason?: string): void {
    this.readyState = WebSocketMock.CLOSED;
    this.onclose?.(new CloseEvent('close', { code: code ?? 1000, reason: reason ?? '' }));
  }

  addEventListener(_type: string, _listener: EventListener): void {
    // Stub — extend if needed
  }

  removeEventListener(_type: string, _listener: EventListener): void {
    // Stub — extend if needed
  }

  dispatchEvent(_event: Event): boolean {
    return true;
  }

  /** Test helper: simulate receiving a message from the server */
  _receiveMessage(data: string | object): void {
    const payload = typeof data === 'object' ? JSON.stringify(data) : data;
    this.onmessage?.(new MessageEvent('message', { data: payload }));
  }

  /** Test helper: get all messages sent via send() */
  _getSentMessages(): (string | ArrayBuffer)[] {
    return [...this.sentMessages];
  }
}

Object.defineProperty(globalThis, 'WebSocket', {
  value: WebSocketMock,
  writable: true,
});

export { WebSocketMock };
