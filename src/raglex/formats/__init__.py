"""Pluggable document-format parsers (markup family → text + segments + edges).

Importing the package registers the built-in parsers. Add a format = a module that
calls ``register(name, parser)``.
"""

from . import akoma_ntoso, bwb, eurlex_html, formex  # noqa: F401  (register on import)
from .base import ParsedDoc, available, parse, register

__all__ = ["ParsedDoc", "available", "parse", "register"]
