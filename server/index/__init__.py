"""Workspace semantic index + GraphRAG.

Retrieval over a connected workspace. The embed, rerank and NER backends follow
the model mode (server/infrastructure/model_mode.py): on-device Qwen3 and GLiNER,
or Cohere and Haiku on Bedrock.

  - chunker.py      line-anchored text chunking (pure)
  - embedder.py     Qwen3-Embedding (on-device); embedder_cohere.py for Cohere
  - reranker.py     Qwen3-Reranker (on-device) or Cohere Rerank
  - extractor.py    entity extraction (GLiNER, GLiNER2, Haiku or an on-device model)
  - relations.py    typed entity relations; relstore.py stores them
  - salience.py     entity salience scores
  - store/          SQLite per workspace and backend; vectors searched with sqlite-vec
  - pipeline.py     build/refresh (incremental, hash-based) + grounding retrieval
  - graph_views.py, explore_views.py  the explorer's derived views

Importing this package is cheap: the heavy ML deps (torch, gliner) are imported
lazily inside embedder/extractor, never at module load.
"""

# Re-exported for main.py wiring; importing these registers their executors
# (workspace_semantic_search, workspace_graph_query) via @register_executor.
from . import graph_tool, tool  # noqa: F401
from .routes import router  # noqa: F401
from .scheduler import init_index_scheduler  # noqa: F401
