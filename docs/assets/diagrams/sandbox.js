/* Architecture - sandbox & security: the boundary a shell command crosses */
WSDiagram.mount("sandbox-diagram", {
  title: "How a shell command reaches the OS sandbox",
  grid: { nodeW: 162, nodeH: 62, gapX: 46, gapY: 46 },
  groups: {
    server: { label: "Entry" },
    security: { label: "Security layers" },
    tools: { label: "Execution" },
    external: { label: "Left open" }
  },
  nodes: [
    { id: "cmd", group: "server", col: 0, row: 0, label: "Shell / code / terminal", sub: "shell · run_python · PTY · aws", desc: "Every path that spawns a shell or runs code funnels through the same layers before it executes." },
    { id: "val", group: "security", col: 1, row: 0, label: "Command validator", sub: "regex deny-list", desc: "validate_command() splits compound commands and rejects rm -rf /, mkfs, dd of=/dev/..., dangerous pipes, and sensitive-path reads. Defence-in-depth, bypassable on its own." },
    { id: "appr", group: "security", col: 2, row: 0, label: "Approval", sub: "server-side nonce", desc: "Risky commands pause the turn and wait for a client-confirmed nonce the server holds. A real consent boundary." },
    { id: "sbox", group: "security", col: 3, row: 0, label: "run_sandboxed", sub: "sandbox-exec / bwrap", desc: "The OS sandbox: a macOS sandbox-exec profile or Linux bwrap mount. The strongest layer, and the one to trust last and most." },
    { id: "deny", group: "security", col: 3, row: 1, label: "Deny secret paths", sub: "_DENIED_PATHS", desc: "The sandbox is allow-default plus deny rules for a curated secret-store list: SSH/GPG keys, cloud creds, git creds, browser cookies, shell history, password stores." },
    { id: "proc", group: "tools", col: 2, row: 1, label: "Subprocess", sub: "runs", desc: "The command runs with the deny-list applied. Reads of files not on the list still succeed, since default is allow." },
    { id: "net", group: "external", kind: "external", col: 1, row: 1, label: "Network (open)", sub: "git · pip · aws", desc: "Network is intentionally left open: sandboxed git, pip, npm, aws, and build commands all need it. Accepted residual risk." }
  ],
  edges: [
    { from: "cmd", to: "val" },
    { from: "val", to: "appr", label: "passes" },
    { from: "appr", to: "sbox", label: "approved" },
    { from: "sbox", to: "deny", label: "profile" },
    { from: "deny", to: "proc" },
    { from: "proc", to: "net" }
  ]
});
