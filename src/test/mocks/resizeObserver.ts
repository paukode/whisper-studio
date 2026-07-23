/**
 * ResizeObserver mock for jsdom test environment.
 *
 * jsdom does not implement ResizeObserver. This stub is needed by
 * components that use ResizeObserver (e.g., TerminalTab with FitAddon,
 * Monaco Editor).
 */

class ResizeObserverMock {
  private callback: ResizeObserverCallback;

  constructor(callback: ResizeObserverCallback) {
    this.callback = callback;
  }

  observe(_target: Element, _options?: ResizeObserverOptions): void {
    // Stub — tests can call _trigger() to simulate resize
  }

  unobserve(_target: Element): void {
    // Stub
  }

  disconnect(): void {
    // Stub
  }

  /** Test helper: simulate a resize observation */
  _trigger(entries: ResizeObserverEntry[]): void {
    this.callback(entries, this);
  }
}

Object.defineProperty(globalThis, 'ResizeObserver', {
  value: ResizeObserverMock,
  writable: true,
});

export { ResizeObserverMock };
