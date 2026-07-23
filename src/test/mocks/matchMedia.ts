/**
 * matchMedia mock for jsdom test environment.
 *
 * jsdom does not implement window.matchMedia. This stub returns a
 * MediaQueryList-like object so the ThemeProvider's
 * `matchMedia('(prefers-color-scheme: light)')` call works in tests.
 */

Object.defineProperty(globalThis, 'matchMedia', {
  writable: true,
  value: (query: string): MediaQueryList => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => {},      // deprecated but still used by some libs
    removeListener: () => {},   // deprecated but still used by some libs
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => true,
  }),
});
