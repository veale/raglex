"""RagLex — a multi-jurisdiction legal-corpus harvester + analysis system.

See `raglex design docs/` for the full design. This package implements build
sequencing step 1 (§9): the adapter pattern, content-addressed raw store, the
catalogue with typed-relations edges, the two-stage topic gate, the shared ingest
pipeline with watermarks, and the first source adapter (UK Find Case Law).
"""

__version__ = "0.1.0"
