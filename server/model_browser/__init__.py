"""In-app model browser: search Hugging Face for GGUF and MLX chat models, pick
a build, and install it as a config-driven local model.

The HTTP surface is :data:`server.model_browser.routes.router`, mounted in
server/main.py. Everything else in the package is importable as a library
(compat rules, HF wrappers, install/service) for the routes and the tests."""

from server.model_browser.routes import router

__all__ = ["router"]
