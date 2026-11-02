/* Configuration - the five config layers merged into one effective config */
WSDiagram.mount("config-layers-diagram", {
  title: "How the config layers merge",
  grid: { nodeW: 176, nodeH: 62, gapX: 96, gapY: 30 },
  groups: {
    external: { label: "Shell" }, server: { label: "Project" },
    persist: { label: "User" }, shipped: { label: "Shipped" }, browser: { label: "Effective" }
  },
  nodes: [
    { id: "env", group: "external", kind: "external", col: 0, row: 0, label: "Environment", sub: "TAVILY_API_KEY", desc: "The TAVILY_API_KEY environment variable, the only config key with an environment override. Read on every request; a rotated key applies without a restart. Highest priority." },
    { id: "project", group: "server", col: 0, row: 1, label: "Project", sub: ".whisper/settings.json", desc: "Per-project overrides in the workspace: model, region, effort, cost caps, feature flags. Permission mode and auto-mode rules are never read from here. Hand-authored, often checked in; only a safe subset of keys is honored. Read fresh whenever the workspace changes." },
    { id: "user", group: "persist", kind: "store", col: 0, row: 2, label: "User", sub: "config.user.json", desc: "Your per-machine deltas for all sessions: keys, region, added models, per-field overrides, a hide-list. Under ~/.whisper for the Mac app, at the repo root for a source install. Gitignored; holds the Tavily key. Startup never changes it." },
    { id: "system", group: "shipped", kind: "store", col: 0, row: 3, label: "System", sub: "config.example.json", desc: "The app-owned model catalog and shipped defaults. Read-only and read live, so new template models appear with no sync step. chat_models merges key by key with your user layer." },
    { id: "defaults", group: "shipped", kind: "store", col: 0, row: 4, label: "Code defaults", sub: "DEFAULTS", desc: "Built-in fallbacks in server/infrastructure/config.py. Used only where no higher layer sets a value. Lowest priority." },
    { id: "eff", group: "browser", col: 1, row: 2, label: "Effective config", sub: "what runs", desc: "The merged result: DEFAULTS, then the shipped system layer, then user, then project, then env. Latched per session at the first turn so mid-session edits never disrupt a live conversation." }
  ],
  edges: [
    { from: "env", to: "eff", label: "highest" },
    { from: "project", to: "eff" },
    { from: "user", to: "eff" },
    { from: "system", to: "eff" },
    { from: "defaults", to: "eff", label: "lowest" }
  ]
});
