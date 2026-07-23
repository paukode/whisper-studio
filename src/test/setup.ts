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
import './mocks/mediaRecorder';
import './mocks/matchMedia';
import './mocks/resizeObserver';

// jsdom does not implement scrollIntoView; stub it so components that
// auto-scroll (e.g. ChatPanel) don't throw during tests.
if (typeof Element.prototype.scrollIntoView !== 'function') {
  Element.prototype.scrollIntoView = () => {};
}
