# In-app model browser (LM Studio / Unsloth style)

Design + research for letting users **search, download, and use** open-source
GGUF chat models from inside Whisper Studio, instead of hand-editing config.
Research-only document; no code has changed. Written on branch
`feat/model-browser`.

The brief was explicitly "no constraints, tell me the best way," so this leads
with the target architecture I recommend, then covers the mechanics, then a
phased plan that includes a fast MVP shortcut for comparison.

---

## 1. Goal

A "Discover" experience like LM Studio / Ollama / Jan:

1. Recommended / trending open-source models shown at the top.
2. Search across Hugging Face (Unsloth, bartowski, lmstudio-community, ggml-org,
   first-party orgs like Qwen/Google/Mistral, and optionally all of HF).
3. Per-model **quant picker** (Q4_K_M, Q5_K_M, Q8_0, Unsloth UD-\* …) with a
   size and a **will-it-fit** badge for this Mac.
4. One click to download, with progress; when it finishes it appears in the
   Models list and the composer picker automatically; clicking it in the
   composer loads it via `llama-server` exactly as today.

---

## 2. Recommended architecture: a first-class "model library"

Today an on-device model is a **config entry**: a `chat_models` object with
`is_local: true` carrying `repo_id` / `filename` / `dir` / `ctx`, merged from
built-ins + `config.user.json`. That was the right call for hand-curated models,
and a browser *could* just write more such entries. But as the primary way to
manage a growing library of downloaded models it's the wrong long-term shape:
config is for a human's deliberate settings, not an app-managed catalog of 20
downloaded files, and it conflates "my preferences" with "what I have on disk."

**The best way is to make the models directory the source of truth, backed by a
small manifest** — the model that LM Studio (scan a folder) and Ollama (a
manifest store) both use.

```
WHISPER_HOME/
  models/
    gguf/
      unsloth__Qwen3-8B-GGUF/
        Qwen3-8B-Q4_K_M.gguf
      bartowski__Llama-3.3-70B-Instruct-GGUF/
        Llama-3.3-70B-Instruct-Q4_K_M-00001-of-00002.gguf
        Llama-3.3-70B-Instruct-Q4_K_M-00002-of-00002.gguf
    library.json          <- manifest the browser writes on install
```

- **`library.json` (or a SQLite table) is the manifest.** One record per
  installed model: `key`, `repo_id`, `filename` (first shard), `shards`, `dir`,
  `label`, `quant`, `arch`, `n_ctx_default`, `size_bytes`, `chat_template_ok`,
  `supports_tools`, `installed_at`, `source` ("browser" | "manual"),
  `enabled`. Rich app-specific metadata that the GGUF header doesn't carry lives
  here; everything the header *does* carry (architecture, context length, param
  count, chat template presence) is read once at install and cached here so the
  app never re-derives it.
- **Disk reconciliation on startup.** Scan `models/gguf/`; a GGUF present but not
  in the manifest gets adopted (metadata read from its header) so a
  hand-dropped file just works; a manifest entry whose files are gone is pruned.
  "What's downloaded is what you have" — no config drift, delete-the-folder is a
  valid uninstall.
- **The chat registry becomes a 3-way merge**: built-ins (code) + `config`
  `chat_models` (hand-curated / non-HF / power users) + **library manifest**
  (browser-installed). `server/local/registry.py::local_models()` already merges
  built-ins + config; we add the library as a third source. Everything
  downstream — the Models tab, the composer picker (`/api/models`), and
  load-on-use via `llama-server` — is unchanged, because it already consumes the
  merged registry.

Why this over "just write config entries":
- Scales to a real library without bloating `config.user.json`.
- Clean separation: config = intent, library = inventory.
- Manual GGUF drops and browser installs converge on one code path.
- Uninstall is well-defined (manifest row + files), independent of user config.
- Keeps `config.user.json` for what it's genuinely good at: a power user pinning
  a specific non-HF model or overriding `ctx`/label.

The quick alternative (browser writes `config.user.json` entries, reusing the
exact path a hand-added model uses today) is a legitimate **MVP shortcut** — it
ships in days and reuses 90% of the plumbing — but it's a stepping stone, not
the destination. See the phased plan (§9).

---

## 3. How the comparable tools work (informs the choices)

- **LM Studio** (closest analog: llama.cpp, Apple Silicon): in-app HF search,
  paste-a-repo-URL, per-model **quant dropdown** that "highlights the
  recommended choice for your hardware and indicates which options are
  supported." Curation is a **hybrid**: a trusted curated feed (the
  `lmstudio-community` HF org) plus live `?library=gguf&sort=trending`. Shows a
  green/yellow/red **fit badge** from a built-in memory estimator
  (`lms load --estimate-only`), targeting weights ≤ ~80% of available memory.
- **Ollama**: own registry *and* runs any HF GGUF directly
  (`ollama run hf.co/{user}/{repo}:{quant}`), default quant `Q4_K_M`, chat
  template auto-selected from GGUF metadata.
- **Unsloth**: publishes `UD-` "dynamic" quants (per-layer optimized), often
  day-0 for new architectures with corrected chat templates. A strong default
  "recommended author."

Takeaways we adopt: hybrid curation, a quant picker with a hardware fit badge,
Q4_K_M / UD-Q4_K_XL as the default recommended quant, and a trusted-author
allowlist with an opt-in "search all of HF."

---

## 4. Data source: the Hugging Face Hub API

The app already calls `HfApi().model_info(..., files_metadata=True)` in
`server/models_manager/sizes.py`, so `huggingface_hub` is present and this is an
extension of existing usage, not a new dependency.

**Search** (`HfApi().list_models`, or `GET /api/models`):
```python
api.list_models(
    library="gguf",                 # GGUF repos only
    pipeline_tag="text-generation", # chat/completion (exclude embedders)
    author=<trusted or None>,       # unsloth / bartowski / lmstudio-community / ggml-org / Qwen …
    search=<query>,
    sort="trendingScore",           # "trending" tab; use "downloads" for all-time popular
    direction=-1, limit=30,
    expand=["downloads","likes","trendingScore","gguf","lastModified"],
)
```
- `sort="trendingScore"` powers the "hot now" list; `sort="downloads"` (last-30-day
  count) powers "popular." `num_parameters="min:..,max:.."` can cap size in the UI.
- **No token required** for public search/download. Anonymous limits are
  per-IP over 5-min windows (≈500 API / 3,000 download requests) — fine for a
  desktop app; offer an optional HF token field in Settings to raise them and
  rely on `huggingface_hub ≥ 1.2.0` automatic 429 backoff.

**Quant / file enumeration** (per chosen repo):
```python
info = api.model_info(repo_id, files_metadata=True)   # siblings[i].rfilename + .size
```
Parse the quant token from each `*.gguf` filename; group shards
(`-00001-of-00002.gguf` → one logical model, sum sizes, download all, pass the
**first** shard to llama-server); hide standalone `*mmproj*.gguf` (vision
projectors, not chat models on their own).

---

## 5. Compatibility, fit, and quant guidance

**Architecture gate (do this before download).** Whether a GGUF actually runs is
decided by the bundled `llama-server`'s compiled architecture support, not by
the file being valid. Read `general.architecture` from the GGUF header cheaply —
either `expand=["gguf"]` on the search/info call (HF pre-parses header metadata
into `ModelInfo.gguf`) or a client-side header range-read — and compare against
the architectures our bundled llama.cpp supports. Mark unsupported models
"needs a newer engine" instead of letting a multi-GB download fail at load. Also
require a chat template (`tokenizer.chat_template` present) so we don't offer
base/non-chat GGUFs.

**Fit badge (LM Studio style), Apple Silicon unified memory:**
```
total ≈ gguf_size + kv_cache(n_ctx) + ~8% overhead
kv_cache ≈ 2 × n_layers × n_ctx × n_kv_heads × head_dim × bytes_per_elem
budget ≈ 0.75 × total_RAM        # Metal wired limit, roughly
green  if total ≤ 0.80 × budget
yellow if total ≤ budget
red    if total > budget
```
All the header fields (`n_layers`, `n_kv_heads`, `head_dim`, default `n_ctx`)
come from `expand=gguf`, so the badge needs no download. Recompute when the user
changes context size. `hf-mem` (`uvx hf-mem --model-id … --gguf-file … --experimental`)
computes authoritative weights + KV numbers from metadata via range requests and
is a good cross-check or backend.

**Quant guidance to surface:** default-select **Q4_K_M** (or Unsloth
`UD-Q4_K_XL`), tagged "recommended," auto-picking the largest quant that stays
green. Ladder: Q3_K\* (small, some quality loss) → **Q4_K_M** → Q5_K_M → Q6_K /
Q8_0 (near-lossless, large) → IQ\* i-quants (smaller at equal quality, slightly
slower).

---

## 6. Backend design

New module `server/model_browser/` (search + install orchestration), reusing the
existing download machinery:

- `GET /api/models/browse/curated` → featured list (remote JSON + offline cache).
- `GET /api/models/browse/search?q=&author=&sort=&all=` → live `list_models`,
  scoped to the trusted-author allowlist unless `all=1`. Returns repo id, label,
  downloads/trending, param size, and an arch-supported flag.
- `GET /api/models/browse/repo/{repo_id}` → the quant list: each GGUF file (shards
  grouped), size, parsed quant, arch, chat-template-ok, and a fit badge computed
  against this machine's RAM. mmproj/embedding files filtered out.
- `POST /api/models/browse/install` `{repo_id, filename|quant, n_ctx?}` →
  1. resolve shards, 2. write a **library manifest** record (recommended) — or a
  `config.user.json` entry (MVP), 3. enqueue the download through the existing
  `server/models_manager` queue (which already gives progress, cancel, retry,
  the "queued behind N" behavior, and the actionable-error handling we shipped),
  downloading via `huggingface_hub.hf_hub_download` (cache + resume + dedupe).
- `DELETE /api/models/browse/{key}` → stop if resident (llama_server.stop),
  remove files + manifest row.

Reused as-is: the download **queue/progress/cancel/retry**, `sizes.py` HF sizing,
the composer picker's live refresh, and load-on-use via `llama-server`. The only
genuinely new backend surface is search + GGUF-header reading + the library store.

**RAM detection** for the fit badge: read total physical memory
(`sysctl hw.memsize` / `os.sysconf`) once at startup.

---

## 7. Frontend design

A **"Discover" tab** in Settings (next to Models), or a "Browse models" button
on the Models tab:

- A search box; two ranked sections by default: **Recommended** (curated) and
  **Trending/Popular** (live). A toggle "Search all of Hugging Face" beyond the
  trusted authors.
- Each result expands to a **quant picker**: rows of quant | size | fit badge
  (green/yellow/red), Q4_K_M pre-selected and tagged "recommended," an
  "unsupported — needs newer engine" state for archs the bundled llama.cpp can't
  run, and a disk-space warning when free space is tight.
- **Download** enqueues via the existing queue; progress and the "Queued behind
  N" chip we already built appear in the Models tab. On completion the model
  shows in Models and the composer with zero extra wiring (the live picker
  refresh we shipped handles it).
- Reuse the zustand-v5-safe patterns and the existing model-card styling.

---

## 8. Curation (fresh without app updates)

Hybrid, matching LM Studio:
1. A small **remote JSON** "featured" list (hosted on an HF dataset repo / gist /
   CDN) — repo id, blurb, recommended quant, param size, min-RAM hint — so the
   list refreshes without shipping an app update.
2. **Live trending/popular** via `list_models(sort=trendingScore|downloads)`.
3. **Offline fallback**: bundle a static copy of the featured JSON; if the remote
   JSON and the HF API are both unreachable, show the bundled list plus whatever
   is already installed.

Default the browse scope to a **trusted-author allowlist** (unsloth, bartowski,
lmstudio-community, ggml-org, Qwen, google, mistralai …); "all of HF" is an
explicit opt-in to keep quality/safety high.

---

## 9. What changes vs what's reused (honest accounting)

Reused unchanged: download queue + progress + cancel + retry, `config.user.json`
user layer (for the MVP path and for power-user overrides), the composer
picker's live refresh, load-on-use via `llama-server`, `sizes.py` HF sizing,
`hf_hub_download`.

New: the **library store + disk reconciliation** (the recommended architecture),
the **search/curated/repo/install/uninstall** endpoints, **GGUF header reading**
for arch + fit, the **fit estimator + RAM detection**, and the **Discover UI**.

Changed (recommended, not required for MVP): `registry.local_models()` grows a
third source (the library manifest); model existence becomes disk-authoritative
rather than config-authoritative.

---

## 10. Risks / edge cases

- **Unsupported architecture** for the bundled llama.cpp → gate at browse time;
  longer term, ship engine (llama.cpp) updates so "needs newer engine" resolves.
- **Sharded GGUFs** → group as one install, sum sizes, download all shards, pass
  the first shard to the server.
- **Gated repos** (Llama/Gemma terms) → detect via the API `gated` flag; deep-link
  the user to accept on the model page, or hide behind the token setting.
- **Disk space** → check free space ≥ size × ~1.1 before install; sharded models
  multiply this.
- **Broken chat templates** even in good repos → fall back to
  `--chat-template-file`; surface a link to the repo's Discussions.
- **Trust** → GGUF weights don't execute code, but default to the trusted-author
  allowlist and validate the header before running through the sandboxed server.
- **Rate limits** → optional HF token in Settings; rely on `huggingface_hub`
  smart-retry; prefer resolver (download) calls, which have far higher quotas.

---

## 11. Phased plan

- **P0 — MVP (fast, config path):** search trusted authors + quant picker + size,
  install by writing a `config.user.json` entry and enqueuing the existing
  download; it then appears in Models + composer via the paths we already have.
  Proves the end-to-end loop in days. No fit badge, no arch gate yet.
- **P1 — Compatibility + fit:** GGUF-header reading (`expand=gguf`), arch gate,
  RAM detection, the green/yellow/red fit badge, sharded-model grouping,
  mmproj/embedding filtering.
- **P2 — Library store (the target architecture):** introduce `library.json` +
  disk reconciliation, make `registry.local_models()` a 3-way merge, migrate the
  MVP's config-written entries into the library, and move install/uninstall onto
  it. This is where it becomes a real, scalable library rather than config rows.
- **P3 — Curation + reach:** remote featured JSON + offline cache, trending/popular
  tabs, "search all of HF" opt-in, HF-token setting, gated-repo handling.
- **P4 — Engine lifecycle:** bundle-updatable llama.cpp so newly-released
  architectures become runnable without a full app release; "update engine"
  action tied to the arch gate.

---

## 12. Open decisions for the user

1. **Architecture:** go straight to the library store (P2 first, cleaner), or
   ship the config-path MVP (P0) and migrate later? Recommend: MVP to validate
   UX, then the library store, with a one-time migration.
2. **Default browse scope:** trusted-author allowlist only, or all of HF with the
   allowlist merely ranked first?
3. **Curated list hosting:** an HF dataset repo you own, a gist/CDN, or bundled-
   only for v1?
4. **Vision models (VLM + mmproj):** in scope for the browser, or text chat only
   to start?
5. **Engine updates (P4):** is a user-updatable bundled llama.cpp something you
   want on the roadmap, given it's what unlocks brand-new architectures?
