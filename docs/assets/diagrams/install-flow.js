/* Get started - installation flow (prerequisites to first hello) */
WSDiagram.mount("install-flow-diagram", {
  title: "Zero to a running app",
  grid: { nodeW: 166, nodeH: 60, gapX: 52, gapY: 40 },
  groups: {
    server: { label: "On your Mac" }, external: { label: "Cloud services" }, browser: { label: "In the app" }
  },
  nodes: [
    { id: "prereqs", group: "server", col: 0, row: 0, label: "Install prerequisites", sub: "brew: git python awscli", desc: "Install Homebrew, then `brew install git python@3.12 awscli`. Reopen Terminal so PATH updates." },
    { id: "aws", group: "external", kind: "external", col: 1, row: 0, label: "Connect AWS", sub: "aws configure", desc: "Run `aws configure` with an access key, secret, and region. The region must match the Bedrock Region you set in Settings." },
    { id: "tavily", group: "external", kind: "external", col: 1, row: 1, label: "Tavily key", sub: "optional", desc: "Optional web-search key. Export TAVILY_API_KEY in ~/.zshrc, or paste it into Settings. Everything else works without it." },
    { id: "clone", group: "server", col: 2, row: 0.5, label: "Clone + setup.sh", sub: "builds + serves", desc: "Clone the repo and run `bash setup.sh`. It creates a venv, installs deps, builds the frontend, and serves it on a free port from 8000." },
    { id: "configure", group: "browser", col: 3, row: 0.5, label: "Configure in app", sub: "gear icon", desc: "Open the printed URL, click the gear icon, and set Bedrock Region and Default Model." },
    { id: "hello", group: "browser", col: 4, row: 0.5, label: "Say hello", sub: "verify", desc: "Type hello in chat. If Claude responds, you are done." }
  ],
  edges: [
    { from: "prereqs", to: "aws" },
    { from: "aws", to: "clone" },
    { from: "tavily", to: "clone", label: "optional" },
    { from: "clone", to: "configure" },
    { from: "configure", to: "hello" }
  ]
});
