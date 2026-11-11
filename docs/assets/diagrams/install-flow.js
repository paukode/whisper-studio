/* Get started - installation flow (prerequisites to first hello) */
WSDiagram.mount("install-flow-diagram", {
  title: "Zero to a running app",
  grid: { nodeW: 166, nodeH: 60, gapX: 52, gapY: 40 },
  groups: {
    server: { label: "On your Mac" }, external: { label: "Cloud services" }, browser: { label: "In the app" }
  },
  nodes: [
    { id: "prereqs", group: "server", col: 0, row: 0, label: "Install prerequisites", sub: "Homebrew + Xcode CLT", desc: "Install Homebrew and the Xcode Command Line Tools (`xcode-select --install`). setup.sh installs Python 3.13, git, and the AWS CLI itself when they are missing." },
    { id: "aws", group: "external", kind: "external", col: 1, row: 0, label: "Connect AWS", sub: "aws configure", desc: "Needed only for Hybrid or Cloud mode. Run `aws configure` (or `aws sso login`), or set a Bedrock API key in AWS_BEARER_TOKEN_BEDROCK. Bedrock calls go to the Bedrock Region you set in Settings." },
    { id: "tavily", group: "external", kind: "external", col: 1, row: 1, label: "Tavily key", sub: "optional", desc: "Optional web-search key. Export TAVILY_API_KEY in ~/.zshrc, or paste it into Settings. Everything else works without it." },
    { id: "clone", group: "server", col: 2, row: 0.5, label: "Clone + setup.sh", sub: "builds + serves", desc: "Clone the repo and run `bash setup.sh`. It creates a venv, installs deps and the on-device runtime, fetches the always-on speech models, builds the capture helper and the frontend, and serves it on port 41430 (or the nearest free port)." },
    { id: "configure", group: "browser", col: 3, row: 0.5, label: "Configure in app", sub: "gear icon", desc: "Open the printed URL. A fresh install starts in Local mode: for Claude or GPT, click the gear icon, switch Model mode to Hybrid or Cloud, and set the Bedrock Region under Settings > General; to stay on-device, install a model from Settings > Models > Discover. The chat model is picked from the model chip in the composer, not in Settings." },
    { id: "hello", group: "browser", col: 4, row: 0.5, label: "Say hello", sub: "verify", desc: "Type hello in chat. If the model responds, you are done." }
  ],
  edges: [
    { from: "prereqs", to: "aws" },
    { from: "aws", to: "clone" },
    { from: "tavily", to: "clone", label: "optional" },
    { from: "clone", to: "configure" },
    { from: "configure", to: "hello" }
  ]
});
