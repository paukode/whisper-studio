# Whisper Studio

A local-first AI workspace for macOS. Real-time speech transcription, a
chat client to Claude (via Amazon Bedrock), and a full development
environment — file tree, Monaco editor, integrated terminal, Git,
LSP — in a single browser tab served by a single local process.

> **Transcription runs entirely on your machine.** The speech model
> loads into memory on first record and runs locally on CPU/GPU. Audio
> never leaves the laptop. Amazon Bedrock is only used for the chat
> (Claude) responses you explicitly send. No telemetry, no proxies, no
> per-minute transcription fees.

## Documentation

The full docs live in **[`docs/`](docs/)** — a self-contained
static site (no build step, zero external network calls). Browse it
locally:

```sh
cd docs && python3 -m http.server 8123
# open http://127.0.0.1:8123
```

It also publishes to GitHub Pages — see
[`docs/README.md`](docs/README.md).

| If you want to… | Read |
|---|---|
| Install from scratch (zero prior tooling) | [Installation](docs/installation.html) · [Requirements](docs/requirements.html) |
| Configure region, model, keys, feature flags | [Configuration](docs/configuration.html) · [Env vars](docs/ref-env.html) · [Settings keys](docs/ref-settings.html) |
| Learn a task (chat, voice, docs, research, coding, cron) | [Tutorials](docs/tut-first-chat.html) |
| Look up a slash command or agent tool | [Slash commands](docs/ref-slash-commands.html) · [Agent tools](docs/ref-tools.html) |
| Record mic, a Chrome tab, or system audio for meetings | [Voice & meetings](docs/tut-voice.html) |
| Understand the internals | [Architecture](docs/arch-overview.html) |
| Understand the security model | [Security](docs/ref-security.html) |
| Hack on the code | [Development & contributing](docs/contributing.html) |

## Highlights

- **Single-origin local app** — FastAPI serves the REST/SSE/WebSocket
  API and the React SPA on `http://127.0.0.1:8000`. No CORS, no separate
  frontend host, no remote backend.
- **Local transcription** with speaker diarization — two on-device
  engines (Parakeet streaming + Whisper batch), per-session speaker
  memory across reconnects.
- **Chat with Claude or GPT** (Haiku, Sonnet, Sonnet 5, Opus 4.6/4.7/4.8,
  Fable 5.0, GPT-5.4/5.5) — streaming tokens, tool use, attachments, slash commands,
  and voice-submit triggers. Or run fully on-device in local mode.
- **Full workspace IDE** — file tree, Monaco editor, xterm.js terminal,
  Git (status/diff/log/blame/branches/PR), LSP (Python + TypeScript),
  ripgrep search.
- **Extensible** — cloud / hybrid / local model modes, MCP servers,
  opt-in Python plugins, and custom Markdown skills.
- **Persistent sessions** in SQLite (WAL) and **cron-style background
  jobs** whose output streams back into the originating chat.

## Quick start

If you already have Python 3.10+, Homebrew, Git, and the AWS CLI configured:

```sh
git clone https://github.com/paukode/whisper-studio.git
cd whisper-studio
bash setup.sh
```

`setup.sh` provisions an isolated `venv/`, installs Node into it via
`nodeenv`, fetches frontend deps, builds the bundle, and serves the app
on a single port. Open the URL it prints, click the gear icon, and set
your **Bedrock Region** and **Default Model**. Type "hello" in chat — if
Claude responds, you're done. Transcription needs no setup; the speech
model auto-downloads on first record.

For the Vite dev server with hot-module reload, use `bash setup.sh --dev`.

New to Python / Homebrew / AWS, or want the setup flags and model modes?
The **[Installation guide](docs/installation.html)** walks through
everything from zero.

## Updating

Already running Whisper Studio? No need to re-clone. From the repo folder:

```sh
git fetch origin
git reset --hard origin/main
bash setup.sh          # rebuild the bundle and install any new deps
```

Your settings, chat history, and downloaded models live in gitignored folders
(`config.json`, `storage/`, `data/`, `models/`), so this updates only the code
and leaves them untouched.

> **Heads up:** `git reset --hard` discards any local changes to *project files*
> (not your config or data), so commit or `git stash` them first if you've
> edited the code. And never run `git clean` here: it would delete the
> gitignored `config.json`, `models/`, and `storage/` you want to keep.

## Configuration

Settings resolve in three layers, highest priority first: environment
(`TAVILY_API_KEY`, `HOST`, `PORT`), project
(`<workspace>/.whisper/settings.json`), and user (`config.json` at the
repo root, gitignored). The project and user layers are edited through
the in-app Settings panel (gear icon); the env layer is shell-only.

`setup.sh` seeds `config.json` from `config.example.json` on first run.
Chat, transcription, and the workspace tools all work out of the box;
web search needs a Tavily key. Full field-by-field tables are in
**[Configuration](docs/configuration.html)**,
**[Environment variables](docs/ref-env.html)**, and
**[Settings & config keys](docs/ref-settings.html)**.

## Usage

Type `/` in the chat input for slash commands, `@file:path` to pull a
file into the prompt, and the microphone icon to dictate (say "okay send",
"send now", "fire away", or "send the message" to submit hands-free; a
bare "send" never fires). When a workspace is connected, the
assistant can read and edit files, run sandboxed commands, use Git,
search the web, remember things across sessions, schedule jobs, and spawn
sub-agents — all gated by the current permission mode.

Each of these has a tutorial or reference page:

- **[Tutorials](docs/tut-first-chat.html)** — first chat, voice &
  meetings, documents, web research, the workspace IDE, permissions,
  memory & WHISPER.md, skills, sub-agents, cron, indexing & search, MCP &
  plugins, and model modes.
- **[Slash commands](docs/ref-slash-commands.html)** and
  **[Agent tools](docs/ref-tools.html)** — the complete reference for
  every `/` command and the full tool pool the assistant can call (~80+
  core tools plus skill-backed tools).

## Security model

Whisper Studio binds `127.0.0.1` by default, sanitizes every Markdown
render through DOMPurify before it reaches the DOM, routes risky commands
through a server-side nonce approval flow, and wraps shell + Python
execution in `sandbox-exec(5)`. Plugins are opt-in, and the only outbound
call the backend makes on its own is to Amazon Bedrock for chat — no
telemetry.

Full boundary table: **[Security model](docs/ref-security.html)**,
with the deeper rationale in
[Overall system § Security boundaries](docs/overall-system.html#security-boundaries).

## Development

```sh
source venv/bin/activate
pytest tests/ -q     # backend unit + smoke tests
npm run build        # tsc -b + vite build
npm run lint         # ESLint
npm test             # vitest frontend tests
```

Ruff lints and formats the Python (`ruff check .` / `ruff format .`) and
is a blocking CI gate. The project layout, conventions (backend-restart,
tool wiring, approval flow, file-size budget), and full tech stack are
documented in **[Development & contributing](docs/contributing.html)**.

## License

MIT License. See [LICENSE](LICENSE) for the exact terms.

## Disclaimer

This is a personal project, not affiliated with, endorsed by, or sponsored
by Amazon. Amazon, AWS, and Amazon Bedrock are trademarks of Amazon.com,
Inc. or its affiliates, referenced here only for identification. Whisper
Studio sends chat requests to Amazon Bedrock, a paid AWS service billed to
your own AWS account per token, so using cloud models incurs costs. Running
in on-device (local) mode makes no cloud calls.
