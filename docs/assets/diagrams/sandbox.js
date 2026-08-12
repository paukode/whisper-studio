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
    { id: "cmd", group: "server", col: 0, row: 0, label: "Shell / code / terminal", sub: "shell · run_python · PTY · aws", desc: "Every path that spawns a shell or runs code eventually reaches the OS sandbox, but run_python skips the command validator entirely (only its AWS read-only guard applies)." },
    { id: "appr", group: "security", col: 1, row: 0, label: "Approval", sub: "server-side pause", desc: "A gated action pauses the turn in paused_sessions before any validation runs; nothing executes until the client posts the action to /api/approval/execute. A real consent boundary." },
    { id: "val", group: "security", col: 2, row: 0, label: "Command validator", sub: "regex deny-list", desc: "validate_command() re-checks the approved command - splits compound commands and rejects rm -rf /, mkfs, dd of=/dev/..., dangerous pipes, and sensitive-path reads - as a defense-in-depth re-check at execute time, not a pre-filter the user's approval was based on." },
    { id: "sbox", group: "security", col: 3, row: 0, label: "run_sandboxed", sub: "sandbox-exec / bwrap", desc: "The OS sandbox: a macOS sandbox-exec profile or Linux bwrap mount. The strongest layer, and the one to trust last and most." },
    { id: "deny", group: "security", col: 3, row: 1, label: "Deny secret paths", sub: "_DENIED_PATHS", desc: "The sandbox is allow-default plus deny rules for a curated secret-store list: SSH/GPG keys, cloud creds, git creds, browser cookies, shell history, password stores." },
    { id: "proc", group: "tools", col: 2, row: 1, label: "Subprocess", sub: "runs", desc: "The command runs with the deny-list applied. Reads of files not on the list still succeed, since default is allow." },
    { id: "net", group: "external", kind: "external", col: 1, row: 1, label: "Network (open by default)", sub: "opt-in egress policy", desc: "Network is open by default: sandboxed git, pip, npm, aws, and build commands all need it. The opt-in network_policy tiers (curated / restrictive) restrict egress via an SNI-filtering proxy plus profile deny rules." }
  ],
  edges: [
    { from: "cmd", to: "appr" },
    { from: "appr", to: "val", label: "approved" },
    { from: "val", to: "sbox", label: "passes" },
    { from: "sbox", to: "deny", label: "profile" },
    { from: "deny", to: "proc" },
    { from: "proc", to: "net" }
  ]
});
