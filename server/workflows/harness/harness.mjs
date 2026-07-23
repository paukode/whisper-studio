// Workflow harness bootstrap (Node 24, zero npm deps).
//
// A child process launched by server/workflows/runtime.py. It speaks ndjson
// JSON-RPC over stdio: it EMITS requests ({id, method, params}: agent, workflow,
// budget_spent) and notifications ({method, params}: phase, log, meta, done,
// fatal); it RECEIVES control ({method: start|cancel}) and responses
// ({id, result} | {id, error}). stdout carries ONLY protocol JSON — everything
// else goes to stderr.
//
// Two modes (from the `start` message):
//   parse — compile the source (syntax check) and extract+validate the
//           `export const meta` object literal WITHOUT executing the script,
//           then emit a `meta` notification.
//   run   — evaluate the script in a locked-down vm context (guards + API
//           globals), await its returned result, emit `done`.

import vm from "node:vm";
import { makeApi } from "./api.mjs";
import { GUARD_SRC } from "./guards.mjs";

// ── stdio protocol ───────────────────────────────────────────────────────────

function emit(obj) {
  process.stdout.write(JSON.stringify(obj) + "\n");
}
function notify(method, params) {
  emit({ method, params: params ?? {} });
}

let _rpcId = 0;
const _pending = new Map();
let _cancelled = false;

function rpcCall(method, params) {
  if (_cancelled) return Promise.reject(new Error("CancelledError"));
  const id = ++_rpcId;
  emit({ id, method, params: params ?? {} });
  return new Promise((resolve, reject) => _pending.set(id, { resolve, reject }));
}

function handleResponse(msg) {
  const p = _pending.get(msg.id);
  if (!p) return;
  _pending.delete(msg.id);
  if (msg.error) {
    const e = new Error(msg.error.message || "rpc error");
    e.type = msg.error.type;
    p.reject(e);
  } else {
    p.resolve(msg.result);
  }
}

function cancelAll() {
  _cancelled = true;
  for (const { reject } of _pending.values()) reject(new Error("CancelledError"));
  _pending.clear();
}

// ── meta extraction (no execution) ────────────────────────────────────────────

function extractMetaLiteral(source) {
  const m = /\bexport\s+const\s+meta\b/.exec(source);
  if (!m) throw new Error("script must start with `export const meta = { ... }`");
  let i = source.indexOf("=", m.index + m[0].length);
  if (i === -1) throw new Error("malformed meta declaration (no =)");
  i = source.indexOf("{", i);
  if (i === -1) throw new Error("meta must be an object literal");
  let depth = 0;
  let inStr = null;
  for (let j = i; j < source.length; j++) {
    const c = source[j];
    if (inStr) {
      if (c === "\\") j++;
      else if (c === inStr) inStr = null;
    } else if (c === '"' || c === "'" || c === "`") {
      inStr = c;
    } else if (c === "{") {
      depth++;
    } else if (c === "}") {
      depth--;
      if (depth === 0) return source.slice(i, j + 1);
    }
  }
  throw new Error("unterminated meta object literal");
}

function parseMeta(source) {
  const literal = extractMetaLiteral(source);
  // Evaluate ONLY the literal in an EMPTY context: any function call or free
  // identifier throws, mechanically enforcing "pure literal".
  const ctx = vm.createContext(Object.create(null));
  let meta;
  try {
    meta = vm.runInContext("(" + literal + ")", ctx, { timeout: 1000 });
  } catch (e) {
    throw new Error("meta must be a pure object literal: " + e.message);
  }
  if (!meta || typeof meta !== "object") throw new Error("meta must be an object");
  if (typeof meta.name !== "string" || !meta.name.trim())
    throw new Error("meta.name must be a non-empty string");
  if (typeof meta.description !== "string" || !meta.description.trim())
    throw new Error("meta.description must be a non-empty string");
  const phases = (meta.phases || []).map((p) => (typeof p === "object" ? p.title : p)).filter(Boolean);
  return { name: meta.name, description: meta.description, phases };
}

// ── run mode ──────────────────────────────────────────────────────────────────

async function runScript({ source, args, meta }) {
  // Strip the `export` so `const meta = {...}` is a valid statement inside the
  // wrapper, and the script's top-level `await`/`return` work in the async IIFE.
  const body = source.replace(/\bexport\s+const\s+meta\b/, "const meta");

  const api = makeApi(rpcCall, notify, meta, {
    value: args,
    __budget_total__: args?.__budget_total__ ?? null,
  });

  const sandbox = Object.assign(Object.create(null), {
    agent: api.agent,
    pipeline: api.pipeline,
    parallel: api.parallel,
    phase: api.phase,
    log: api.log,
    workflow: api.workflow,
    budget: api.budget,
    args: api.args,
    // Safe intrinsics the vm realm already provides (Object/Array/JSON/Math/
    // Promise/etc.) stay available; these are just explicit helpers.
    console: { log: (...a) => api.log(...a), error: (...a) => api.log(...a) },
    __wf_result__: undefined,
    __wf_error__: undefined,
  });

  const context = vm.createContext(sandbox);
  vm.runInContext(GUARD_SRC, context); // determinism guards

  const wrapped =
    "__wf_result__ = (async () => {\n" + body + "\n})().catch(e => { __wf_error__ = e; throw e; });";
  vm.runInContext(wrapped, context, { filename: "workflow.mjs" });

  const result = await sandbox.__wf_result__;
  return result === undefined ? null : result;
}

// ── control loop ──────────────────────────────────────────────────────────────

async function onStart(params) {
  try {
    if (params.mode === "parse") {
      const meta = parseMeta(params.source);
      notify("meta", { meta });
      flushAndExit(0);
      return;
    }
    // run
    const meta = parseMeta(params.source); // also validates before executing
    const result = await runScript({ source: params.source, args: params.args, meta });
    notify("done", { result });
    flushAndExit(0);
  } catch (e) {
    notify("fatal", { error: String(e && e.message ? e.message : e), stack: (e && e.stack) || "" });
    flushAndExit(1);
  }
}

function flushAndExit(code) {
  // Give stdout a tick to flush the final line before exiting.
  process.stdout.write("", () => process.exit(code));
}

// Line-buffered stdin reader.
let _buf = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (chunk) => {
  _buf += chunk;
  let nl;
  while ((nl = _buf.indexOf("\n")) !== -1) {
    const line = _buf.slice(0, nl).trim();
    _buf = _buf.slice(nl + 1);
    if (!line) continue;
    let msg;
    try {
      msg = JSON.parse(line);
    } catch {
      continue;
    }
    if (msg.method === "start") {
      onStart(msg.params || {});
    } else if (msg.method === "cancel") {
      cancelAll();
    } else if (msg.id != null) {
      handleResponse(msg);
    }
  }
});
process.stdin.on("end", () => {
  if (_pending.size === 0 && !_cancelled) process.exit(0);
});
