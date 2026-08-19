"""Graph + explorer view wrappers.

Thin cached facades over the heavy view bodies in server/index/graph_views.py
and server/index/explore_views.py. The wrappers memoize through the shared
derived-view cache (invalidated on any index write); the view modules are
imported at the bottom so the views<->store cycle resolves (they read store
attributes only at call time).
"""

from __future__ import annotations

from .base import _cached_derived


def file_graph(ws_path: str, max_edges: int = 300) -> dict:
    """Cached wrapper (invalidated on index write). See graph_views.file_graph_impl."""
    return _cached_derived(
        ws_path, ("file_graph", int(max_edges)), lambda: _gv.file_graph_impl(ws_path, max_edges)
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
