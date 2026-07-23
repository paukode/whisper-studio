/* Architecture - git wrappers, the .git watcher, and the LSP proxy */
WSDiagram.mount("gitlsp-diagram", {
  title: "Git integration and LSP proxy",
  grid: { nodeW: 168, nodeH: 62, gapX: 54, gapY: 42 },
  groups: {
    tools: { label: "Tools" }, server: { label: "Git layer" }, security: { label: "Approval" },
    persist: { label: "Repository" }, local: { label: "Language servers" }, browser: { label: "SPA" }
  },
  nodes: [
    { id: "tool", group: "tools", col: 0, row: 0, label: "git_* / lsp_* tool", sub: "assistant call", desc: "The model calls a git or lsp tool; read tools run freely, write tools are approval-gated." },
    { id: "core", group: "server", col: 1, row: 0, label: "git core", sub: "find_git_root (cached)", desc: "server/git/core.py: find_git_root walks up to .git and is LRU-cached to ~50 entries; state queries read the loose ref files directly." },
    { id: "run", group: "server", col: 2, row: 0, label: "_run_git", sub: "creds off, 15s", desc: "Runs git via subprocess with stdin closed and credential prompts disabled (GIT_TERMINAL_PROMPT=0), a 15s timeout, and truncated output." },
    { id: "appr", group: "security", col: 3, row: 0, label: "Approval", sub: "writes", desc: "git_add_commit, git_push, and friends emit a [WS_APPROVAL] card; the real command runs only after you confirm." },
    { id: "repo", group: "persist", kind: "store", col: 4, row: 0, label: "Git repo", sub: ".git/", desc: "The workspace repository. Writes land here once approved; reads run without prompting." },
    { id: "watch", group: "server", col: 3, row: 1.4, label: "Watcher", sub: "SSE on HEAD change", desc: "GitFileWatcher polls .git/HEAD, config, and the current branch ref once a second and pushes a git-changed SSE event." },
    { id: "lsp", group: "local", kind: "external", col: 3, row: 2.4, label: "LSP proxy", sub: "pylsp / tsserver", desc: "A WebSocket-to-stdio tunnel that spawns python-lsp-server or typescript-language-server per connection." },
    { id: "spa", group: "browser", col: 4, row: 1.9, label: "SPA panels", sub: "git panel + editor", desc: "The React workspace: the Git changes panel refetches on each SSE event, and Monaco talks JSON-RPC through the LSP proxy." }
  ],
  edges: [
    { from: "tool", to: "core" },
    { from: "core", to: "run" },
    { from: "run", to: "appr", label: "write" },
    { from: "appr", to: "repo", label: "approved" },
    { from: "run", to: "repo", label: "read" },
    { from: "repo", to: "watch" },
    { from: "watch", to: "spa" },
    { from: "lsp", to: "spa" }
  ]
});
