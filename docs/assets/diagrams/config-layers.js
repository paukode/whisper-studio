/* Configuration - the three config layers merged into one effective config */
WSDiagram.mount("config-layers-diagram", {
  title: "How the config layers merge",
  grid: { nodeW: 176, nodeH: 62, gapX: 96, gapY: 30 },
  groups: {
    external: { label: "Shell" }, server: { label: "Project" },
    persist: { label: "User" }, browser: { label: "Effective" }
  },
  nodes: [
    { id: "env", group: "external", kind: "external", col: 0, row: 0, label: "Environment", sub: "TAVILY_API_KEY · HOST · PORT", desc: "Shell environment variables. Read on every request; a rotated key applies without a restart. Highest priority." },
    { id: "project", group: "server", col: 0, row: 1, label: "Project", sub: ".whisper/settings.json", desc: "Per-project overrides in the workspace: model, region, permission mode. Read fresh whenever the workspace changes." },
    { id: "user", group: "persist", kind: "store", col: 0, row: 2, label: "User", sub: "config.json", desc: "Your per-machine defaults for all sessions. Gitignored; holds the Tavily key. Cached with a short TTL." },
    { id: "defaults", group: "persist", kind: "store", col: 0, row: 3, label: "Code defaults", sub: "DEFAULTS", desc: "Built-in fallbacks in server/infrastructure/config.py. Used only where no higher layer sets a value. Lowest priority." },
    { id: "eff", group: "browser", col: 1, row: 1.5, label: "Effective config", sub: "what runs", desc: "The merged result: DEFAULTS, then user, then project, then env. Latched per session at the first turn so mid-session edits never disrupt a live conversation." }
  ],
  edges: [
    { from: "env", to: "eff", label: "highest" },
    { from: "project", to: "eff" },
    { from: "user", to: "eff" },
    { from: "defaults", to: "eff", label: "lowest" }
  ]
});
