"""Graph + semantic-map view wrappers.

Thin cached facades over the heavy view bodies in server/index/graph_views.py.
The wrappers memoize through the shared derived-view cache (invalidated on any
index write); ``_gv`` is imported at the bottom so the graph_views<->store
cycle resolves (graph_views reads store attributes only at call time).
"""

from __future__ import annotations

from .base import _cached_derived


def file_graph(ws_path: str, max_edges: int = 300) -> dict:
    """Cached wrapper (invalidated on index write). See graph_views.file_graph_impl."""
    return _cached_derived(
        ws_path, ("file_graph", int(max_edges)), lambda: _gv.file_graph_impl(ws_path, max_edges)
    )


def entity_graph(ws_path: str, name: str, label: str = "", max_files: int = 200) -> dict:
    """Cached wrapper (invalidated on index write). See graph_views.entity_graph_impl."""
    return _cached_derived(
        ws_path,
        ("entity_graph", name, label, int(max_files)),
        lambda: _gv.entity_graph_impl(ws_path, name, label, max_files),
    )


def all_workspaces_graph(max_edges: int = 400) -> dict:
    """Cross-workspace file-relationship graph. See graph_views.all_workspaces_graph."""
    return _gv.all_workspaces_graph(max_edges)


def all_workspaces_umap_graph(max_edges: int = 400) -> dict:
    """Cross-workspace semantic map (UMAP over all indexed files). See
    graph_views.all_workspaces_umap_graph."""
    return _gv.all_workspaces_umap_graph(max_edges)


def umap_graph(ws_path: str, max_edges: int = 300) -> dict:
    """Cached wrapper (invalidated on index write). See graph_views.umap_graph_impl."""
    return _cached_derived(
        ws_path, ("umap_graph", int(max_edges)), lambda: _gv.umap_graph_impl(ws_path, max_edges)
    )


def explore_overview(ws_path: str) -> dict:
    """Cached wrapper (invalidated on index write). See explore_views.overview_impl."""
    return _cached_derived(ws_path, ("explore_overview",), lambda: _xv.overview_impl(ws_path))


def explore_entities(ws_path: str, q: str = "", include_low: bool = False) -> dict:
    """Cached wrapper (invalidated on index write). See explore_views.entity_list_impl."""
    return _cached_derived(
        ws_path,
        ("explore_entities", q, bool(include_low)),
        lambda: _xv.entity_list_impl(ws_path, q, include_low),
    )


def explore_entity(ws_path: str, name: str, label: str = "") -> dict:
    """Cached wrapper (invalidated on index write). See explore_views.entity_page_impl."""
    return _cached_derived(
        ws_path,
        ("explore_entity", name.lower(), label),
        lambda: _xv.entity_page_impl(ws_path, name, label),
    )


def explore_file(ws_path: str, file_path: str) -> dict:
    """Cached wrapper (invalidated on index write). See explore_views.file_page_impl."""
    return _cached_derived(
        ws_path, ("explore_file", file_path), lambda: _xv.file_page_impl(ws_path, file_path)
    )


# Heavy derived graph + explorer view bodies live in graph_views/explore_views
# (split out to keep the store package's modules under the file-size budget).
# Imported here, after the wrappers are defined, so the views<->store import
# cycle resolves cleanly; the wrappers touch _gv/_xv only at call time.
from server.index import explore_views as _xv  # noqa: E402
from server.index import graph_views as _gv  # noqa: E402
