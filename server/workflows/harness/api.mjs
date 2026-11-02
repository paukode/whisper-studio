// The workflow API globals injected into the vm context.
//
// Every side-effecting call rides the RPC bridge back to the Python
// orchestrator (`rpcCall`) or emits a notification (`notify`); pipeline() and
// parallel() are plain-JS control flow. Real concurrency is throttled
// server-side (the Python semaphore gates each agent RPC), so the harness may
// keep unlimited promises in flight.

export function makeApi(rpcCall, notify, meta, args) {
  const phases = Array.isArray(meta?.phases) ? meta.phases : [];

  // Resolves to {text, output, usage, status, agent_id, stop_reason,
  // stopped_early}. status is "completed" (finished), "incomplete" (a round,
  // time or cost limit stopped the agent; text is partial and stop_reason
  // names the limit) or "failed". It never rejects for the agent's own
  // failure, so the script decides what to do with a partial result.
  async function agent(prompt, opts = {}) {
    if (typeof prompt !== "string" || !prompt.trim()) {
      throw new Error("agent(prompt, opts): prompt must be a non-empty string");
    }
    return rpcCall("agent", { prompt, opts: opts || {} });
  }

  // Programmatic tool calling: `tool(name, args)` (or the ergonomic
  // `tools.<name>(args)` proxy) runs ONE read-only host tool via RPC and
  // resolves to its string output. Non-read-only tools are refused server-side.
  // The program orchestrates reads/searches, keeping every intermediate
  // result in its own variables instead of the model's context.
  async function tool(name, toolArgs = {}) {
    if (typeof name !== "string" || !name.trim()) {
      throw new Error("tool(name, args): name must be a non-empty string");
    }
    if (toolArgs === null || typeof toolArgs !== "object" || Array.isArray(toolArgs)) {
      throw new Error("tool args must be an object");
    }
    const r = await rpcCall("tool", { name, args: toolArgs });
    return r?.output ?? "";
  }
  const tools = new Proxy(Object.create(null), {
    get(_t, prop) {
      if (typeof prop !== "string") return undefined;
      return (toolArgs = {}) => tool(prop, toolArgs);
    },
  });

  // Errors that end the whole run, not one item: the budget or agent cap is
  // spent, or the run was cancelled. pipeline() and parallel() rethrow them so
  // the script can catch them; any other item error becomes null and is
  // logged to the run log.
  const RUN_ERRORS = new Set(["BudgetExceededError", "AgentCapError", "CancelledError"]);

  function itemFailed(fn, index, e) {
    if (e && (RUN_ERRORS.has(e.type) || e.message === "CancelledError")) throw e;
    const message = e && e.message ? e.message : String(e);
    notify("log", { message: `[error] ${fn} item ${index}: ${message}` });
    return null;
  }

  // Each item runs through ALL stages independently, with NO barrier between
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
        } catch (e) {
          return itemFailed("pipeline", index, e);
        }
      }),
    );
  }

  // Barrier: awaits all thunks. A thunk that throws resolves to null so
  // callers can filter(Boolean).
  async function parallel(thunks) {
    if (!Array.isArray(thunks)) throw new Error("parallel(thunks): thunks must be an array");
    return Promise.all(
      thunks.map((t, index) =>
        Promise.resolve()
          .then(() => t())
          .catch((e) => itemFailed("parallel", index, e)),
      ),
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

  // budget.total is the run's output-token cap (default 600k, set server-side);
  // spent()/remaining() are live RPC reads of the run's output-token ledger.
  const budgetTotal = args.__budget_total__;
  const budget = {
    total: budgetTotal,
    async spent() {
      const r = await rpcCall("budget_spent", {});
      return r?.spent ?? 0;
    },
    async remaining() {
      const r = await rpcCall("budget_spent", {});
      return Math.max(0, budgetTotal - (r?.spent ?? 0));
    },
  };

  const frozenArgs = args?.value !== undefined ? Object.freeze(args.value) : undefined;

  return { agent, tool, tools, pipeline, parallel, phase, log, workflow, budget, args: frozenArgs };
}
