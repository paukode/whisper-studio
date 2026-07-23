// The workflow API globals injected into the vm context.
//
// Every side-effecting call rides the RPC bridge back to the Python
// orchestrator (`rpcCall`) or emits a notification (`notify`); pipeline() and
// parallel() are plain-JS control flow. Real concurrency is throttled
// server-side (the Python semaphore gates each agent RPC), so the harness may
// keep unlimited promises in flight.

export function makeApi(rpcCall, notify, meta, args) {
  const phases = Array.isArray(meta?.phases) ? meta.phases : [];

  async function agent(prompt, opts = {}) {
    if (typeof prompt !== "string" || !prompt.trim()) {
      throw new Error("agent(prompt, opts): prompt must be a non-empty string");
    }
    return rpcCall("agent", { prompt, opts: opts || {} });
  }

  // Each item runs through ALL stages independently — NO barrier between
  // stages. A stage that throws drops that item to null and skips its rest.
  async function pipeline(items, ...stages) {
    if (!Array.isArray(items)) throw new Error("pipeline(items, ...stages): items must be an array");
    return Promise.all(
      items.map(async (item, index) => {
        let prev = item;
        try {
          for (const stage of stages) {
            prev = await stage(prev, item, index);
          }
          return prev;
        } catch {
          return null;
        }
      }),
    );
  }

  // Barrier: awaits all thunks. A thunk that throws resolves to null (never
  // rejects the whole call) so callers can filter(Boolean).
  async function parallel(thunks) {
    if (!Array.isArray(thunks)) throw new Error("parallel(thunks): thunks must be an array");
    return Promise.all(
      thunks.map((t) => Promise.resolve().then(() => t()).catch(() => null)),
    );
  }

  function phase(name) {
    if (phases.length && !phases.includes(name)) {
      notify("log", { message: `[warn] phase("${name}") is not declared in meta.phases` });
    }
    notify("phase", { name: String(name) });
  }

  function log(...parts) {
    notify("log", {
      message: parts
        .map((p) => (typeof p === "string" ? p : JSON.stringify(p)))
        .join(" "),
    });
  }

  async function workflow(nameOrRef, wfArgs) {
    const name = typeof nameOrRef === "string" ? nameOrRef : nameOrRef?.name;
    return rpcCall("workflow", { name, args: wfArgs ?? null });
  }

  // budget.total is the turn target (or null); spent()/remaining() are live RPC
  // reads of the run ledger.
  const budgetTotal = args?.__budget_total__ ?? null;
  const budget = {
    total: budgetTotal,
    async spent() {
      const r = await rpcCall("budget_spent", {});
      return r?.spent ?? 0;
    },
    async remaining() {
      if (budgetTotal == null) return Infinity;
      const r = await rpcCall("budget_spent", {});
      return Math.max(0, budgetTotal - (r?.spent ?? 0));
    },
  };

  const frozenArgs = args?.value !== undefined ? Object.freeze(args.value) : undefined;

  return { agent, pipeline, parallel, phase, log, workflow, budget, args: frozenArgs };
}
