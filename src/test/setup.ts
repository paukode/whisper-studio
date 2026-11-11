/**
 * Vitest global test setup.
 *
 * - Registers @testing-library/jest-dom matchers (toBeInTheDocument, etc.)
 * - Imports browser API mocks so every test runs in a realistic jsdom environment
 */
import '@testing-library/jest-dom';

// Browser API mocks
import './mocks/localStorage';
import './mocks/broadcastChannel';
import './mocks/webSocket';
import './mocks/matchMedia';
import './mocks/resizeObserver';

// jsdom does not implement scrollIntoView; stub it so components that
// scroll a row into view (the composer's autocomplete, the command palette)
// don't throw during tests.
if (typeof Element.prototype.scrollIntoView !== 'function') {
  Element.prototype.scrollIntoView = () => {};
}

// Composer drafts live in a store that outlives a component (per-session
// drafts, src/stores/composerDraftStore.ts). Every test starts with none, as
// a freshly mounted composer did when the text was component state.
import { beforeEach, vi } from 'vitest';

// The app's long requests travel over a WebSocket (src/api/streamSocket.ts),
// behind the same Response and EventSource interfaces. Tests drive them
// through those interfaces with fetch and EventSource stand-ins; the
// transport itself is tested in src/api/streamSocket.test.ts.
vi.mock('@/api/streamSocket', () => ({
  streamFetch: (path: string, init?: RequestInit) => globalThis.fetch(path, init),
  SocketEventSource: class {
    static readonly CONNECTING = 0;
    static readonly OPEN = 1;
    static readonly CLOSED = 2;
    constructor(url: string) {
      return new globalThis.EventSource(url);
    }
  },
}));
import { useComposerDraftStore } from '@/stores/composerDraftStore';

beforeEach(() => {
  useComposerDraftStore.setState({ bySession: {} });
});
