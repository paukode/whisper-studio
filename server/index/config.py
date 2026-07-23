"""Tunables for the workspace semantic index + GraphRAG layer.

Everything configurable lives here so the model ids, chunking, and the GLiNER
entity vocabulary can be adjusted without touching the pipeline. Env overrides
keep parity with the rest of the backend (WHISPER_* knobs elsewhere).
"""

import os

# Local model directories — weights cached under ./models like whisper/parakeet,
# downloaded at setup. Sentinels are the real weight/config files (verified
# against the repo layout) so the "already present" check can't drift.
_SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MODELS_DIR = os.path.join(_SCRIPT_DIR, "models")
EMBED_MODEL_DIR = os.path.join(MODELS_DIR, "qwen3-embedding-0.6b")
EMBED_SENTINEL = os.path.join(EMBED_MODEL_DIR, "model.safetensors")
GLINER_MODEL_DIR = os.path.join(MODELS_DIR, "gliner-large-v2.5")
GLINER_SENTINEL = os.path.join(GLINER_MODEL_DIR, "gliner_config.json")
# GLiNER2 (fastino) — an optional alternative on-device NER model, selectable
# per workspace (see ner_model in wssettings). Schema-driven, English-strong.
GLINER2_MODEL_DIR = os.path.join(MODELS_DIR, "gliner2-large-v1")
GLINER2_SENTINEL = os.path.join(GLINER2_MODEL_DIR, "config.json")
RERANK_MODEL_DIR = os.path.join(MODELS_DIR, "qwen3-reranker-0.6b")
RERANK_SENTINEL = os.path.join(RERANK_MODEL_DIR, "model.safetensors")

# ── Embedding (Qwen3-Embedding-0.6B via transformers + torch) ────────────────
EMBED_MODEL = os.environ.get("WHISPER_INDEX_EMBED_MODEL", "Qwen/Qwen3-Embedding-0.6B")
EMBED_DIM = 1024  # Qwen3-Embedding-0.6B hidden size; vectors are L2-normalized.
# Qwen3-Embedding expects an instruction prefix on *queries* only (documents are
# embedded bare). This is the recipe from the model card.
QUERY_INSTRUCTION = (
    "Instruct: Given a search query, retrieve relevant passages from the user's "
    "documents (business docs, contracts, notes, transcripts; English and Polish)\n"
    "Query: "
)
EMBED_BATCH = int(os.environ.get("WHISPER_INDEX_EMBED_BATCH", 16))
EMBED_MAX_TOKENS = 512

# ── Reranker (Qwen3-Reranker-0.6B via transformers + torch) ──────────────────
# Optional cross-encoder reranker (behind the `rag_reranker` flag): reorders the
# fused candidate pool by judging each (query, passage) pair directly — higher
# precision than dense+keyword ranking alone. Same family as the embedder, so
# multilingual (handles non-English content). LLM-based: scores the "yes" token.
RERANK_MODEL = os.environ.get("WHISPER_INDEX_RERANK_MODEL", "Qwen/Qwen3-Reranker-0.6B")
RERANK_CANDIDATES = int(os.environ.get("WHISPER_INDEX_RERANK_CANDIDATES", 30))
RERANK_BATCH = int(os.environ.get("WHISPER_INDEX_RERANK_BATCH", 8))
RERANK_MAX_TOKENS = int(os.environ.get("WHISPER_INDEX_RERANK_MAX_TOKENS", 2048))

# ── Cohere on Bedrock (cloud embed + rerank backends) ────────────────────────
# Cohere Embed v4 + Rerank 3.5 are invoked via bedrock-runtime InvokeModel and
# are region-pinned to us-east-1 (Rerank 3.5 is only there). Embeddings are
# L2-normalized to match the store's cosine search; input_type differs for
# documents vs queries (Cohere's asymmetric retrieval recipe).
COHERE_EMBED_MODEL_ID = os.environ.get("WHISPER_INDEX_COHERE_EMBED_MODEL", "cohere.embed-v4:0")
COHERE_RERANK_MODEL_ID = os.environ.get("WHISPER_INDEX_COHERE_RERANK_MODEL", "cohere.rerank-v3-5:0")
COHERE_REGION = os.environ.get("WHISPER_INDEX_COHERE_REGION", "us-east-1")
COHERE_EMBED_DIM = int(
    os.environ.get("WHISPER_INDEX_COHERE_EMBED_DIM", 1536)
)  # Embed v4 default output dim

# Embedding dim per backend. Each per-backend index DB holds vectors of exactly
# one width, and the store's sqlite-vec table is declared at the backend's dim.
EMBED_DIMS = {"qwen3": EMBED_DIM, "cohere": COHERE_EMBED_DIM}


def dim_for_backend(backend: str | None) -> int:
    """Embedding vector width for an embed backend (defaults to the Qwen3 dim)."""
    return EMBED_DIMS.get(backend or "qwen3", EMBED_DIM)


# ── GLiNER (zero-shot NER → knowledge-graph nodes) ───────────────────────────
# gliner_large-v2.5 (gliner-community, Apache-2.0) is a drop-in upgrade from
# gliner_mediumv2.1 — same predict_entities API, higher span quality. ~2× the
# params, so per-chunk CPU latency rises; override to a *-medium-v2.5 build via
# the env var if latency-bound. On first index it downloads into ./models;
# the old gliner-mediumv2.1 directory can be deleted manually.
GLINER_MODEL = os.environ.get("WHISPER_INDEX_GLINER_MODEL", "gliner-community/gliner_large-v2.5")
# GLiNER2 model id (fastino), used when a workspace selects ner_model="gliner2".
# large-v1 is the English-strongest tier; -base-v1 (205M) is faster, -multi-v1 is
# multilingual. Override via env.
GLINER2_MODEL = os.environ.get("WHISPER_INDEX_GLINER2_MODEL", "fastino/gliner2-large-v1")
# On-device NER model choices offered per workspace (wssettings.ner_model). The
# active NER *backend* (gliner vs cloud haiku) is still chosen by model_mode; this
# only picks WHICH local GLiNER family runs when the backend is on-device.
NER_MODELS = ("gliner", "gliner2")
DEFAULT_NER_MODEL = os.environ.get("WHISPER_INDEX_NER_MODEL", "gliner")
# GLiNER2 native relation extraction over-generates on dense text (it scores many
# predicates per entity pair) yet is sparse on real prose; keep only pairs whose
# head+tail confidence clears this floor, then one best predicate per directed pair.
# 0.5 empirically keeps every correct relation on real business docs (all observed
# raw relations landed at conf >= 0.52) while cutting the low-confidence guesses a
# denser sentence provokes. Lower recovers more (noisier) edges; raise for precision.
GLINER2_RELATION_MIN_CONF = float(os.environ.get("WHISPER_INDEX_GLINER2_REL_MIN_CONF", 0.5))
# Entity types we ask GLiNER to find. Two profiles, chosen automatically per file
# from its extension (see profile_for_ext) rather than by a user setting:
# "business" (reports, contracts, notes, transcripts) drops the code-oriented
# labels that make GLiNER faithfully tag JSON keys, identifiers, and abstract
# nouns ("concept" -> "trust"/"efficiency"); "code" keeps the original 18-label
# set for source files. The graph's node labels come straight from the active
# profile's list.
_BUSINESS_LABELS = [
    "person",
    "organization",
    "product",
    "project",
    "team",
    "job title",
    "metric",
    "technology",
    "location",
    "event",
    "document",
]
_CODE_LABELS = [
    "person",
    "organization",
    "product",
    "technology",
    "programming language",
    "library",
    "framework",
    "module",
    "function",
    "class",
    "API",
    "service",
    "database",
    "concept",
    "file",
    "protocol",
    "event",
    "location",
]
ENTITY_PROFILES = {"business": _BUSINESS_LABELS, "code": _CODE_LABELS}
DEFAULT_ENTITY_PROFILE = os.environ.get("WHISPER_INDEX_ENTITY_PROFILE", "business")

# Source-code file extensions -> the "code" entity profile. Every other file
# (docs, notes, transcripts, markdown) uses "business". Picked automatically per
# file so a mixed folder indexes each file with the right labels, with no user
# choice. This is the source-code subset of TEXT_EXTENSIONS (markdown/rst/txt/tex
# are docs; the DATA_EXTENSIONS config files skip NER entirely anyway).
CODE_EXTENSIONS = {
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".mjs",
    ".cjs",
    ".java",
    ".kt",
    ".go",
    ".rs",
    ".rb",
    ".php",
    ".c",
    ".h",
    ".cpp",
    ".hpp",
    ".cc",
    ".cs",
    ".swift",
    ".scala",
    ".sh",
    ".bash",
    ".zsh",
    ".sql",
    ".css",
    ".scss",
    ".vue",
    ".lua",
    ".r",
    ".jl",
    ".dart",
    ".ex",
    ".exs",
}


def profile_for_ext(ext: str) -> str:
    """Auto-select the entity profile from a file's extension: source code gets
    the code label set, everything else the business set. No user choice."""
    return "code" if (ext or "").lower() in CODE_EXTENSIONS else "business"


def labels_for_profile(profile: str | None) -> list[str]:
    """Entity label set for an entity profile (defaults to business)."""
    return ENTITY_PROFILES.get(profile or DEFAULT_ENTITY_PROFILE, _BUSINESS_LABELS)


# ``ENTITY_LABELS`` stays as the union of both profiles so any label a past index
# stored still round-trips through the label-canonicalization map, and code that
# imports it keeps working. New extraction uses the per-workspace profile.
ENTITY_LABELS = _CODE_LABELS + [lbl for lbl in _BUSINESS_LABELS if lbl not in _CODE_LABELS]

# GLiNER is *called* at a permissive floor so recall stays high (especially on
# Polish text, where span confidences run lower); junk is removed downstream by
# salience rather than by a blunt global threshold. Per-label acceptance floors
# then trim the lowest-confidence spans of the labels most prone to false
# positives. GLINER_THRESHOLD is kept for back-compat / operator override.
GLINER_THRESHOLD = float(os.environ.get("WHISPER_INDEX_GLINER_THRESHOLD", 0.5))
GLINER_CALL_THRESHOLD = float(os.environ.get("WHISPER_INDEX_GLINER_CALL_THRESHOLD", 0.35))
LABEL_THRESHOLDS = {
    "person": 0.55,
    "organization": 0.55,
    "product": 0.60,
    "project": 0.60,
    "team": 0.60,
    "job title": 0.60,
    "metric": 0.65,
    "technology": 0.60,
    "location": 0.65,
    "event": 0.65,
    "document": 0.60,
    "default": 0.55,
}

# ── Entity resolution (second, semantic dedup pass) ──────────────────────────
# After the lexical dedup (NFKC/case/punct) collapses obvious variants, a
# semantic pass merges entity nodes whose names are near-duplicate *within a
# label* by embedding cosine — catching what the conservative lexical key can't
# ("Postgres"/"PostgreSQL", "GHA"/"GitHub Actions"). Reuses the Qwen3 embedder
# already resident during a build; best-effort (skipped if it can't load). The
# threshold is deliberately high so distinct entities are never collapsed.
ENTITY_SEMANTIC_MERGE = os.environ.get("WHISPER_INDEX_SEMANTIC_MERGE", "1") not in (
    "0",
    "false",
    "False",
)
ENTITY_SEMANTIC_THRESHOLD = float(os.environ.get("WHISPER_INDEX_SEMANTIC_THRESHOLD", 0.92))

# ── Entity salience (statistical noise defense, always-on) ───────────────────
# Every entity node gets a 0–1 salience score (name-shape × GLiNER confidence ×
# inverse document frequency), computed at build time. Junk (JSON keys, generic
# words, code identifiers, boilerplate hubs) scores low and is downweighted
# everywhere — retrieval expansion, graph views, descriptions — instead of being
# hard-deleted (a refresh would only re-extract it). Two consumption floors:
SALIENCE_JUNK_FLOOR = float(os.environ.get("WHISPER_INDEX_SALIENCE_JUNK_FLOOR", 0.15))
SALIENCE_GRAPH_FLOOR = float(os.environ.get("WHISPER_INDEX_SALIENCE_GRAPH_FLOOR", 0.30))
# An entity in more than this fraction of all chunks is boilerplate regardless of
# name shape (e.g. "agent" in 31.8% of chunks, "Company" in 42.4%) and is capped
# at the junk floor — language-independently, which a static English stoplist can't.
BOILERPLATE_DF_FRAC = float(os.environ.get("WHISPER_INDEX_BOILERPLATE_DF_FRAC", 0.08))

# ── Grounding score floors (per embed backend) ───────────────────────────────
# Cosine-distance distributions differ across embedders, so the floor that drops
# noise vector matches from the grounding block is per-backend. A relative guard
# (drop hits below GROUND_REL_FLOOR × the top score) adapts to per-query spread.
GROUND_SCORE_FLOORS = {"qwen3": 0.15, "cohere": 0.10}
GROUND_REL_FLOOR = float(os.environ.get("WHISPER_INDEX_GROUND_REL_FLOOR", 0.55))

# ── FTS5 keyword index ───────────────────────────────────────────────────────
# Bumped when the fts_chunks tokenizer changes so the derived table is dropped
# and rebuilt (v2 folds diacritics: ą/ę/ó/ś/ż match their ASCII forms in PL text).
FTS_SCHEMA_VER = 2
# Polish stopwords, merged with the English set in the store's keyword leg so a
# chunk sharing only "i"/"w"/"jest" with a Polish query isn't a keyword hit.
PL_STOPWORDS = frozenset(
    (
        "i",
        "w",
        "we",
        "na",
        "do",
        "z",
        "ze",
        "o",
        "od",
        "za",
        "po",
        "pod",
        "przez",
        "przy",
        "dla",
        "oraz",
        "ale",
        "lub",
        "czy",
        "co",
        "to",
        "tym",
        "tego",
        "jego",
        "jej",
        "ich",
        "jak",
        "się",
        "jest",
        "są",
        "być",
        "był",
        "była",
        "było",
        "były",
        "nie",
        "tak",
        "już",
        "tylko",
        "może",
        "bardzo",
        "który",
        "która",
        "które",
        "których",
    )
)

# ── Typed relationships (optional, OFF by default) ───────────────────────────
# When enabled, each changed file's text + its GLiNER entities are sent to an
# LLM to extract typed entity↔entity relations (works_at, cites, depends_on, …).
# Adds one LLM call per changed file at index time (Bedrock Haiku on the cloud
# build). GLiNER co-occurrence edges remain the default graph either way.
# The on/off switch is per-workspace, in the "typed_relations" section of
# wssettings.get_settings(ws) ({"enabled", "engine"}). It is persisted in the
# index meta table and updated via PUT /api/workspace/index/settings. Only the
# per-file caps stay here.
TYPED_RELATIONS_MAX_ENTITIES = 40  # cap entities listed to the LLM per file
TYPED_RELATIONS_MAX_CHARS = 6000  # cap file text sent to the LLM per file

# ── Chunking ─────────────────────────────────────────────────────────────────
# Budget per chunk in *estimated* tokens (~4 chars/token), kept under the
# embedder's 512-token window so a chunk is never truncated at embed time.
# Chunks are structure-aware: they prefer to split at headings, code
# definitions, and paragraph breaks rather than mid-block.
CHUNK_MAX_TOKENS = 400
CHUNK_OVERLAP_TOKENS = 64

# Structured-data files: still embedded and keyword-searchable, but NOT entity-
# mined — their keys ("name", "settings", "personality", …) are schema, not
# knowledge-graph entities, and NER over them is the single biggest source of
# junk nodes. Skipping extraction here is more reliable than filtering after.
DATA_EXTENSIONS = {".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".xml"}

# ── File selection ───────────────────────────────────────────────────────────
# Per-class size caps. Text/docs are read whole (so bounded by render/OCR/RAM
# cost); sheets get a large cap because large ones are *streamed* to a bounded
# schema+sample (server/index/pipeline._sample_large_sheet — no full load);
# media is read into memory then transcribed, so its cap mirrors attachments.
MAX_FILE_BYTES = 1_000_000  # plain-text/code: skip over 1 MB (blobs/artifacts)
RICH_MAX_FILE_BYTES = 50_000_000  # PDFs/Office docs/images: 50 MB (read whole)
SHEET_MAX_FILE_BYTES = 200_000_000  # csv/xlsx: 200 MB — large sheets are streamed, not loaded
MEDIA_MAX_FILE_BYTES = 200_000_000  # audio/video: 200 MB (read into memory, then transcribed)

# Plain-text/code: read bytes directly (fast, no conversion).
TEXT_EXTENSIONS = {
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".mjs",
    ".cjs",
    ".java",
    ".kt",
    ".go",
    ".rs",
    ".rb",
    ".php",
    ".c",
    ".h",
    ".cpp",
    ".hpp",
    ".cc",
    ".cs",
    ".swift",
    ".scala",
    ".sh",
    ".bash",
    ".zsh",
    ".sql",
    ".css",
    ".scss",
    ".vue",
    ".md",
    ".mdx",
    ".rst",
    ".txt",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".xml",
    ".tex",
    ".lua",
    ".r",
    ".jl",
    ".dart",
    ".ex",
    ".exs",
}
# Spreadsheets: small ones via convert_document (schema+sample); large ones via
# a streamed bounded sampler in the indexer. Kept separate from TEXT so a big
# CSV isn't read whole as plain text.
SHEET_EXTENSIONS = {".csv", ".xlsx", ".xls"}
# Rich documents: routed through server.extract.convert_document, which runs
# MarkItDown and — for scanned PDFs — falls back to OCR (Apple Vision → Haiku).
RICH_DOC_EXTENSIONS = {
    ".pdf",
    ".docx",
    ".doc",
    ".pptx",
    ".ppt",
    ".html",
    ".htm",
    ".epub",
    ".rtf",
}
# Images: OCR'd to text via server.extract.ocr_image_bytes (Apple Vision → Haiku).
IMAGE_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".bmp",
    ".tiff",
    ".tif",
    ".heic",
}
# Audio/video: transcribed locally (mlx-whisper) via server.extract.media; for
# video, on-screen text from sampled frames is added. Mirrors server/extract/media.py.
MEDIA_EXTENSIONS = {
    ".mp3",
    ".wav",
    ".m4a",
    ".flac",
    ".ogg",
    ".aac",
    ".aiff",
    ".aif",
    ".opus",
    ".wma",
    ".mp4",
    ".mov",
    ".webm",
    ".mkv",
    ".avi",
    ".m4v",
}
