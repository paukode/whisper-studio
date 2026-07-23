import { afterEach } from 'vitest';

/**
 * BroadcastChannel mock for jsdom test environment.
 *
 * jsdom does not implement BroadcastChannel. This stub provides the
 * constructor and instance methods so components that use the
 * BroadcastChannel guard can be tested without errors.
 */

class BroadcastChannelMock {
  name: string;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onmessageerror: ((event: MessageEvent) => void) | null = null;

  private static channels = new Map<string, Set<BroadcastChannelMock>>();

  constructor(name: string) {
    this.name = name;
    const group = BroadcastChannelMock.channels.get(name) ?? new Set();
    group.add(this);
    BroadcastChannelMock.channels.set(name, group);
  }

  postMessage(message: unknown): void {
    const group = BroadcastChannelMock.channels.get(this.name);
    if (!group) return;
    const event = new MessageEvent('message', { data: message });
    for (const channel of group) {
      if (channel !== this && channel.onmessage) {
        channel.onmessage(event);
      }
    }
  }

  close(): void {
    const group = BroadcastChannelMock.channels.get(this.name);
    if (group) {
      group.delete(this);
      if (group.size === 0) {
        BroadcastChannelMock.channels.delete(this.name);
      }
    }
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

  /** Test helper: reset all channels between tests */
  static _resetAll(): void {
    BroadcastChannelMock.channels.clear();
  }
}

Object.defineProperty(globalThis, 'BroadcastChannel', {
  value: BroadcastChannelMock,
  writable: true,
});

afterEach(() => {
  BroadcastChannelMock._resetAll();
});

export { BroadcastChannelMock };
