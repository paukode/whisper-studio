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

Read the docs online at **[paukode.github.io/whisper-studio](https://paukode.github.io/whisper-studio/)**,
published to GitHub Pages automatically on every push to `main`.

The docs are a self-contained static site in **[`docs/`](docs/)** (no build
step, zero external network calls), so you can also browse them locally:

```sh
cd docs && python3 -m http.server 8123
# open http://127.0.0.1:8123
```

Deploy setup is in [`docs/README.md`](docs/README.md).

| If you want to… | Read |
|---|---|
| Install from scratch (zero prior tooling) | [Installation](https://paukode.github.io/whisper-studio/installation.html) · [Requirements](https://paukode.github.io/whisper-studio/requirements.html) |
| Configure region, model, keys, feature flags | [Configuration](https://paukode.github.io/whisper-studio/configuration.html) · [Env vars](https://paukode.github.io/whisper-studio/ref-env.html) · [Settings keys](https://paukode.github.io/whisper-studio/ref-settings.html) |
| Learn a task (chat, voice, docs, research, coding, cron) | [Tutorials](https://paukode.github.io/whisper-studio/tut-first-chat.html) |
| Look up a slash command or agent tool | [Slash commands](https://paukode.github.io/whisper-studio/ref-slash-commands.html) · [Agent tools](https://paukode.github.io/whisper-studio/ref-tools.html) |
| Record mic, a Chrome tab, or system audio for meetings | [Voice & meetings](https://paukode.github.io/whisper-studio/tut-voice.html) |
| Understand the internals | [Architecture](https://paukode.github.io/whisper-studio/arch-overview.html) |
| Understand the security model | [Security](https://paukode.github.io/whisper-studio/ref-security.html) |
| Hack on the code | [Development & contributing](https://paukode.github.io/whisper-studio/contributing.html) |

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
The **[Installation guide](https://paukode.github.io/whisper-studio/installation.html)** walks through
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
**[Configuration](https://paukode.github.io/whisper-studio/configuration.html)**,
**[Environment variables](https://paukode.github.io/whisper-studio/ref-env.html)**, and
**[Settings & config keys](https://paukode.github.io/whisper-studio/ref-settings.html)**.

## Usage

Type `/` in the chat input for slash commands, `@file:path` to pull a
file into the prompt, and the microphone icon to dictate (say "okay send",
"send now", "fire away", or "send the message" to submit hands-free; a
bare "send" never fires). When a workspace is connected, the
assistant can read and edit files, run sandboxed commands, use Git,
search the web, remember things across sessions, schedule jobs, and spawn
sub-agents — all gated by the current permission mode.

Each of these has a tutorial or reference page:

- **[Tutorials](https://paukode.github.io/whisper-studio/tut-first-chat.html)** — first chat, voice &
  meetings, documents, web research, the workspace IDE, permissions,
  memory & WHISPER.md, skills, sub-agents, cron, indexing & search, MCP &
  plugins, and model modes.
- **[Slash commands](https://paukode.github.io/whisper-studio/ref-slash-commands.html)** and
  **[Agent tools](https://paukode.github.io/whisper-studio/ref-tools.html)** — the complete reference for
  every `/` command and the full tool pool the assistant can call (~80+
  core tools plus skill-backed tools).

## Security model

Whisper Studio binds `127.0.0.1` by default, sanitizes every Markdown
render through DOMPurify before it reaches the DOM, routes risky commands
through a server-side nonce approval flow, and wraps shell + Python
execution in `sandbox-exec(5)`. Plugins are opt-in, and the only outbound
call the backend makes on its own is to Amazon Bedrock for chat — no
telemetry.

Full boundary table: **[Security model](https://paukode.github.io/whisper-studio/ref-security.html)**,
with the deeper rationale in
[Overall system § Security boundaries](https://paukode.github.io/whisper-studio/overall-system.html#security-boundaries).

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
documented in **[Development & contributing](https://paukode.github.io/whisper-studio/contributing.html)**.

## License

MIT License. See [LICENSE](LICENSE) for the exact terms.

## Disclaimer

Whisper Studio is an independent, community project. It is not affiliated with,
endorsed by, sponsored by, or certified by Amazon, Anthropic, OpenAI, Google, or
any other company whose products or services it can connect to.

All product names, brands, and model names referenced in this project are the
property of their respective owners:

- Amazon, AWS, and Amazon Bedrock are trademarks of Amazon.com, Inc. or its affiliates.
- Claude is a trademark of Anthropic.
- GPT and ChatGPT are trademarks of OpenAI.
- Gemma is a trademark of Google LLC.

These names are used here only for identification and interoperability — to
describe the third-party services and models that Whisper Studio can connect to.
Their use does not imply any affiliation with, or endorsement by, the trademark
owners.

Whisper Studio is built independently, using publicly available knowledge,
documentation, and APIs. It does not use or rely on any confidential,
proprietary, or non-public information belonging to any of these companies.

Whisper Studio does not include, host, redistribute, or grant any rights to these
models, services, or their weights. You access them through your own accounts and
credentials, and your use of each is governed by that provider's own terms of
service and pricing. Cloud models are paid services billed to your own account per
use; running in on-device (local) mode makes no cloud calls. Model names and
availability are described as of this writing and may change without notice.

The software itself is provided "as is", without warranty of any kind, under the
terms of the MIT License in [LICENSE](LICENSE).
