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
import { beforeEach } from 'vitest';
import { useComposerDraftStore } from '@/stores/composerDraftStore';

beforeEach(() => {
  useComposerDraftStore.setState({ bySession: {} });
});
