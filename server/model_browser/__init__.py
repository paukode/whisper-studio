"""In-app model browser: search Hugging Face for GGUF chat models, pick a quant,
install it as a config-driven local model (P0 / design §11).

The HTTP surface is :data:`server.model_browser.routes.router`, mounted in
server/main.py. Everything else in the package is importable as a library
(compat rules, HF wrappers, install/service) for the routes and the tests."""

from server.model_browser.routes import router

__all__ = ["router"]
