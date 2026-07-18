"""RagLex — a multi-jurisdiction legal-corpus harvester + analysis system.

See `raglex design docs/` for the full design. This package implements the
adapter pattern, content-addressed raw store, the catalogue with typed-relations
edges, the shared ingest pipeline with watermarks, and the source adapters. It is
a generic harvester — no built-in subject scope; tagging is user-defined.
"""

__version__ = "0.1.0"
