/* Tutorial - one repo, several isolated worktrees, sessions that never collide */
WSDiagram.mount("worktrees-diagram", {
  title: "One shared repo, one worktree per session",
  grid: { nodeW: 176, nodeH: 60, gapX: 64, gapY: 40 },
  groups: {
    sessions: { label: "Sessions" },
    worktrees: { label: "Per-session worktrees" },
    repo: { label: "Shared repository" }
  },
  nodes: [
    { id: "s1", group: "sessions", col: 0, row: 0, label: "Session A", sub: "chat", desc: "A chat session that enters a worktree keeps working in the main conversation while its files live somewhere else on disk." },
    { id: "s2", group: "sessions", col: 0, row: 1, label: "Session B", sub: "chat", desc: "A second session, entirely independent. Nothing it does can be seen by or collide with Session A's files." },
    { id: "s3", group: "sessions", col: 0, row: 2, label: "Sub-agent C", sub: "spawn_agent isolation: 'worktree'", desc: "A worktree-isolated sub-agent gets the same treatment automatically, keyed by its own agent id." },
    { id: "w1", group: "worktrees", col: 1, row: 0, label: "Worktree A", sub: ".whisper/worktrees/a", desc: "Its own checkout, its own index, its own HEAD, on branch worktree-a. Editing files here never touches the main checkout." },
    { id: "w2", group: "worktrees", col: 1, row: 1, label: "Worktree B", sub: ".whisper/worktrees/b", desc: "Same shape, a different slug and branch (worktree-b). Git refuses to let two worktrees share one branch, so the two can never step on each other." },
    { id: "w3", group: "worktrees", col: 1, row: 2, label: "Worktree agent-C", sub: ".whisper/worktrees/agent-c", desc: "Namespaced agent-<id> slug and branch. On a clean finish its changes are harvested back uncommitted; a stopped-early run keeps the worktree for inspection." },
    { id: "store", group: "repo", kind: "store", col: 2, row: 0.5, label: "Git object store", sub: ".git (shared)", desc: "One object database and ref namespace for the whole repo. Every worktree's commits land in the same store, nothing is duplicated on disk." },
    { id: "main", group: "repo", col: 2, row: 2, label: "Main checkout", sub: "your original branch", desc: "The working directory you started in. It sits untouched while other sessions work in their own worktrees, until a merge (or a harvest) brings changes back." }
  ],
  edges: [
    { from: "s1", to: "w1", label: "enter" },
    { from: "s2", to: "w2", label: "enter" },
    { from: "s3", to: "w3", label: "enter" },
    { from: "w1", to: "store", label: "commit" },
    { from: "w2", to: "store", label: "commit" },
    { from: "w3", to: "store", label: "commit" },
    { from: "w1", to: "main", label: "merge back" },
    { from: "w2", to: "main", label: "merge back" },
    { from: "w3", to: "main", label: "merge back (harvest)" }
  ]
});
