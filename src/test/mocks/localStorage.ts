import { afterEach } from 'vitest';

/**
 * localStorage mock for jsdom test environment.
 *
 * jsdom provides a basic localStorage implementation, but this mock
 * adds a `clear()` between tests and exposes the internal store for
 * assertions when needed.
 */

const store = new Map<string, string>();

const localStorageMock: Storage = {
  getItem: (key: string) => store.get(key) ?? null,
  setItem: (key: string, value: string) => {
    store.set(key, String(value));
  },
  removeItem: (key: string) => {
    store.delete(key);
  },
  clear: () => {
    store.clear();
  },
  get length() {
    return store.size;
  },
  key: (index: number) => {
    const keys = Array.from(store.keys());
    return keys[index] ?? null;
  },
};

Object.defineProperty(globalThis, 'localStorage', {
  value: localStorageMock,
  writable: true,
});

// Reset between tests
afterEach(() => {
  store.clear();
});

export { localStorageMock };
