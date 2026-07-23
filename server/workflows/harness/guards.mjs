// Determinism + isolation guards for the workflow vm context.
//
// The context is built from an object we fully control, so Node globals
// (require/process/fetch/fs/child_process/timers) are simply ABSENT. This
// module additionally neuters the ECMAScript builtins that would let a script
// depend on wall-clock or randomness, so a resume re-hashes identically:
//   - Math.random()            -> throws
//   - Date.now(), new Date()   -> throw (Date WITH explicit args still works —
//                                 parsing a fixed timestamp is deterministic)
//   - setTimeout/setInterval   -> throw (not present anyway; explicit for a
//                                 clear error instead of "not defined")
//
// Run this source string inside the context (vm.runInContext) before the
// user script. It patches the context's own realm only.

export const GUARD_SRC = `
(() => {
  const die = (what) => { throw new Error(
    "DeterminismError: " + what + " is not allowed in a workflow — derive from " +
    "agent() results, not wall-clock time or randomness."); };

  Math.random = () => die("Math.random()");

  const _RealDate = Date;
  function GuardedDate(...args) {
    if (!new.target) die("Date() called as a function");
    if (args.length === 0) die("new Date() with no arguments");
    return new _RealDate(...args);
  }
  GuardedDate.prototype = _RealDate.prototype;
  GuardedDate.parse = _RealDate.parse.bind(_RealDate);
  GuardedDate.UTC = _RealDate.UTC.bind(_RealDate);
  GuardedDate.now = () => die("Date.now()");
  // Redirect the prototype's constructor too, else
  // (new Date('2026')).constructor.now() / .constructor() reach the un-guarded
  // real Date and defeat the determinism guard. Safe: this realm is isolated.
  Object.defineProperty(_RealDate.prototype, "constructor", {
    value: GuardedDate, writable: true, configurable: true,
  });
  globalThis.Date = GuardedDate;

  globalThis.setTimeout = () => die("setTimeout()");
  globalThis.setInterval = () => die("setInterval()");
  globalThis.setImmediate = () => die("setImmediate()");
  globalThis.queueMicrotask = (fn) => Promise.resolve().then(fn); // deterministic, keep
})();
`;
