"""The §8 ops + research web API (FastAPI). Requires the `web` extra."""

from .app import create_app, serve_app

__all__ = ["create_app", "serve_app"]
