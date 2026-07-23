"""Workspace semantic index + GraphRAG.

Local, offline retrieval over a connected workspace:

  - chunker.py   — line-anchored text chunking (pure)
  - embedder.py  — Qwen3-Embedding-0.6B (transformers/torch, lazy load/unload)
  - extractor.py — GLiNER zero-shot NER → knowledge-graph nodes
  - store.py     — SQLite metadata + numpy vector matrix (per workspace)
  - pipeline.py  — build/refresh (incremental, hash-based) + query (vector + graph hop)

Importing this package is cheap: the heavy ML deps (torch, gliner) are imported
lazily inside embedder/extractor, never at module load.
"""

# Re-exported for main.py wiring; importing these registers their executors
# (workspace_semantic_search, workspace_graph_query) via @register_executor.
from . import graph_tool, tool  # noqa: F401
from .routes import router  # noqa: F401
from .scheduler import init_index_scheduler  # noqa: F401
