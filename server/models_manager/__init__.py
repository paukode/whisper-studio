"""In-app model download manager: catalog, download with progress, cancel,
delete, and a cheap status poll — mounted at /api/models (see routes.py)."""

from server.models_manager.routes import router

__all__ = ["router"]
