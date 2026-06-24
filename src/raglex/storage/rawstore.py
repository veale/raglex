"""Content-addressed raw-byte store (§1.2).

Raw bytes are immutable; everything else (text, tags, embeddings, edges) is a
re-derivable projection. Bytes are stored under their SHA-256 so identical
payloads dedup for free and the path is stable forever. A filesystem store is the
single-operator default; an object-store backend (S3/MinIO) is a drop-in later.
"""

from __future__ import annotations

from pathlib import Path

from ..core.models import sha256_bytes


class RawStore:
    """Sharded content-addressed filesystem store: ``<root>/ab/cd/<hash>.<ext>``.

    The two-level prefix keeps any single directory from filling up at corpus
    scale (millions of documents).
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, payload_hash: str, ext: str | None = None) -> Path:
        suffix = f".{ext.lstrip('.')}" if ext else ""
        return self.root / payload_hash[:2] / payload_hash[2:4] / f"{payload_hash}{suffix}"

    def put(self, data: bytes, ext: str | None = None) -> str:
        """Store bytes; return the content hash. Idempotent — re-storing identical
        bytes is a no-op, which is exactly the content-hash dedup of §5."""
        digest = sha256_bytes(data)
        dest = self.path_for(digest, ext)
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(dest.suffix + ".tmp")
            tmp.write_bytes(data)
            tmp.replace(dest)  # atomic publish
        return digest

    def get(self, payload_hash: str, ext: str | None = None) -> bytes:
        return self.path_for(payload_hash, ext).read_bytes()

    def exists(self, payload_hash: str, ext: str | None = None) -> bool:
        return self.path_for(payload_hash, ext).exists()
