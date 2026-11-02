/* Tutorial: one repo, three kinds of worktree, each on its own branch */
WSDiagram.mount("worktrees-diagram", {
  title: "One shared repo, three kinds of worktree",
  grid: { nodeW: 176, nodeH: 60, gapX: 64, gapY: 40 },
  groups: {
    sources: { label: "Who makes it" },
    worktrees: { label: "Worktrees" },
    repo: { label: "Shared repository" }
  },
  nodes: [
    { id: "s1", group: "sources", col: 0, row: 0, label: "The assistant", sub: "ws_create_worktree", desc: "During a conversation, the assistant makes a task branch to keep a change with many files out of your working tree until the merge." },
    { id: "s2", group: "sources", col: 0, row: 1, label: "Agent runtime", sub: "spawn_agent isolation: 'worktree'", desc: "The runtime makes a worktree for each isolated subagent automatically. If it cannot, the agent runs read-only." },
    { id: "s3", group: "sources", col: 0, row: 2, label: "You", sub: "terminal Worktrees menu", desc: "In the terminal panel, + Add worktree makes a worktree for a branch that you name." },
    { id: "w1", group: "worktrees", col: 1, row: 0, label: "Task branch", sub: ".worktrees/<name>", desc: "Its own checkout on branch whisper/<name>. Edits here do not change the main checkout." },
    { id: "w2", group: "worktrees", col: 1, row: 1, label: "Agent worktree", sub: ".whisper/worktrees/agent-<id>", desc: "Branch worktree-agent-<id>. After a clean finish, harvest applies its changes uncommitted. A run that stopped early keeps the worktree." },
    { id: "w3", group: "worktrees", col: 1, row: 2, label: "Terminal worktree", sub: "../<repo>-<branch>", desc: "A worktree next to the main checkout, on a new or existing branch. You merge its branch with git." },
    { id: "store", group: "repo", kind: "store", col: 2, row: 0.5, label: "Git object store", sub: ".git (shared)", desc: "One object database for the full repository. The commits of each worktree go into the same store. Git does not let two worktrees check out the same branch." },
    { id: "main", group: "repo", col: 2, row: 2, label: "Main checkout", sub: "the connected workspace", desc: "The folder that all chat sessions use. It does not change until a merge or a harvest brings changes back." }
  ],
  edges: [
    { from: "s1", to: "w1", label: "create" },
    { from: "s2", to: "w2", label: "create" },
    { from: "s3", to: "w3", label: "add" },
    { from: "w1", to: "store", label: "commit" },
    { from: "w2", to: "store", label: "commit" },
    { from: "w3", to: "store", label: "commit" },
    { from: "w1", to: "main", label: "ws_merge_worktree" },
    { from: "w2", to: "main", label: "harvest" },
    { from: "w3", to: "main", label: "git merge" }
  ]
});
