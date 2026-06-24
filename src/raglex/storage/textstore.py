"""Extracted-text store — the re-derivable text projection of raw bytes (§1.2).

Raw bytes are immutable and content-addressed (``RawStore``); extracted text is a
*projection* of (raw document + a versioned extraction pipeline). Keying text by
the source ``payload_hash`` keeps it aligned with the bytes it came from and lets a
re-extraction (better OCR/parser, §5c) overwrite in place without touching raw.

This is also where the §6b char-span chunker will read from (``char_start/end``
map back into this text), so it is stored as one clean UTF-8 document per payload.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from ..core.models import Segment


class TextStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, payload_hash: str) -> Path:
        return self.root / payload_hash[:2] / payload_hash[2:4] / f"{payload_hash}.txt"

    def _seg_path(self, payload_hash: str) -> Path:
        return self.path_for(payload_hash).with_suffix(".seg.json")

    def put(self, payload_hash: str, text: str) -> Path:
        dest = self.path_for(payload_hash)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".txt.tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(dest)  # atomic publish; re-extraction overwrites cleanly
        return dest

    def get(self, payload_hash: str) -> str:
        return self.path_for(payload_hash).read_text(encoding="utf-8")

    def put_segments(self, payload_hash: str, segments: list[Segment]) -> None:
        """Persist the structural segments (§6b) as a sidecar next to the text —
        a re-derivable projection the chunker reads back."""
        if not segments:
            return
        dest = self._seg_path(payload_hash)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps([asdict(s) for s in segments]), encoding="utf-8")

    def get_segments(self, payload_hash: str) -> list[Segment]:
        path = self._seg_path(payload_hash)
        if not path.exists():
            return []
        try:
            return [Segment(**d) for d in json.loads(path.read_text(encoding="utf-8"))]
        except (OSError, json.JSONDecodeError, TypeError):
            return []
