"""Extracted-text store — the re-derivable text projection of raw bytes (§1.2).

Raw bytes are immutable and content-addressed (``RawStore``); extracted text is a
*projection* of (raw document + a versioned extraction pipeline). Keying text by
the source ``payload_hash`` keeps it aligned with the bytes it came from and lets a
re-extraction (better OCR/parser, §5c) overwrite in place without touching raw.

This is also where the §6b char-span chunker reads from (``char_start/end`` map back
into this text), so it is stored as one clean UTF-8 document per payload.

**Split storage.** The store can span two roots: a fast local ``root`` and a
read-through ``fallback``. That exists because the corpus outgrew the machine that
serves it — the whole text projection is tens of gigabytes and lives on another host,
where a random read measured 22.57 ms against 0.046 ms locally, a 495x difference
that decides whether free-text verification and snippets are possible at all. So the
jurisdictions being searched are copied local and the rest stay remote.

The danger in that arrangement is silent migration: a repair that walks the whole
corpus calling ``put`` would copy every document it touched onto the small fast disk
and fill it. So a write goes **where the document already lives**, and only a
genuinely new payload lands in the primary root. Nothing moves unless asked.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict
from pathlib import Path

from ..core.models import Segment
from ..core.text import fix_cp1252_c1, scrub_surrogates

log = logging.getLogger(__name__)


class TextStore:
    def __init__(self, root: str | Path, fallback: str | Path | None = None,
                 local_sources: set[str] | None = None) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        # A second, usually remote root, read when the primary doesn't have the
        # payload. Never created: if it isn't there, there is no fallback, and a
        # missing mount must not look like an empty corpus.
        env_fallback = os.environ.get("RAGLEX_TEXT_FALLBACK_DIR")
        chosen = fallback if fallback is not None else (env_fallback or None)
        self.fallback = Path(chosen) if chosen else None
        if self.fallback is not None and not self.fallback.is_dir():
            log.warning("[textstore] fallback %s is not a directory — ignoring",
                        self.fallback)
            self.fallback = None
        # Which sources belong on the fast disk. Without this a new harvest of any
        # source lands locally, and the 2.9M-document French collection would fill
        # a disk sized for the UK and EU slices. Empty/unset = everything local,
        # which is the single-root behaviour.
        env_scope = os.environ.get("RAGLEX_TEXT_LOCAL_SOURCES") or ""
        self.local_sources = (local_sources if local_sources is not None
                              else {s for s in env_scope.replace(",", " ").split() if s})

    # -- paths -----------------------------------------------------------------
    @staticmethod
    def _rel(payload_hash: str) -> Path:
        return Path(payload_hash[:2]) / payload_hash[2:4] / f"{payload_hash}.txt"

    def path_for(self, payload_hash: str) -> Path:
        """Where this payload's text IS — the primary if it holds it, else the
        fallback if that does, else the primary (where a new one would be written)."""
        primary = self.root / self._rel(payload_hash)
        if self.fallback is None or primary.exists():
            return primary
        alt = self.fallback / self._rel(payload_hash)
        return alt if alt.exists() else primary

    def _seg_path(self, payload_hash: str) -> Path:
        return self.path_for(payload_hash).with_suffix(".seg.json")

    def locate(self, payload_hash: str) -> str | None:
        """"local" | "fallback" | None — which root actually holds this payload."""
        if (self.root / self._rel(payload_hash)).exists():
            return "local"
        if self.fallback is not None and (self.fallback / self._rel(payload_hash)).exists():
            return "fallback"
        return None

    # -- read / write ----------------------------------------------------------
    def _new_payload_root(self, source: str | None) -> Path:
        """Where a payload seen for the first time should be written.

        In scope, or no scope configured → the fast local root. Out of scope → the
        remote one, if it will take the write; a read-only mount falls back to local
        rather than losing the document, because a harvest that cannot store its text
        is worse than one that stores it in the wrong place."""
        if self.fallback is None or not self.local_sources:
            return self.root
        if source is None or source in self.local_sources:
            return self.root
        if os.access(self.fallback, os.W_OK):
            return self.fallback
        log.warning("[textstore] fallback %s is not writable — storing %s locally",
                    self.fallback, source)
        return self.root

    def put(self, payload_hash: str, text: str, *, source: str | None = None) -> Path:
        """Write the text, IN PLACE wherever it already lives.

        A repair walking the corpus must not quietly migrate documents onto the
        primary disk — that is how a 15 GB working set becomes a full disk halfway
        through a job. ``path_for`` resolves to the existing copy, so only a payload
        that exists in neither root is new; ``source`` then decides which root a new
        one belongs in."""
        if self.locate(payload_hash) is None:
            dest = self._new_payload_root(source) / self._rel(payload_hash)
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(".txt.tmp")
            tmp.write_text(fix_cp1252_c1(scrub_surrogates(text, join_pairs=False)),
                           encoding="utf-8")
            tmp.replace(dest)
            return dest
        dest = self.path_for(payload_hash)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".txt.tmp")
        # Backstop for any extractor that lets an unencodable surrogate through: this
        # write is where it would otherwise raise and take the whole harvest with it.
        # The cp1252 repair rides along — the mis-decoded punctuation it fixes reaches
        # here from several parsers, and this is the one place all of them pass through.
        # Both replacements are 1:1, so the record's segment/citation offsets, computed
        # before this call, still point exactly where they did.
        tmp.write_text(fix_cp1252_c1(scrub_surrogates(text, join_pairs=False)),
                       encoding="utf-8")
        tmp.replace(dest)  # atomic publish; re-extraction overwrites cleanly
        return dest

    def put_local(self, payload_hash: str, text: str) -> Path:
        """Write to the PRIMARY root regardless of where a copy already lives — the
        deliberate migration ``put`` refuses to do on its own. Used when bringing a
        jurisdiction into local storage."""
        dest = self.root / self._rel(payload_hash)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".txt.tmp")
        tmp.write_text(fix_cp1252_c1(scrub_surrogates(text, join_pairs=False)),
                       encoding="utf-8")
        tmp.replace(dest)
        return dest

    def get(self, payload_hash: str) -> str:
        return self.path_for(payload_hash).read_text(encoding="utf-8")

    def exists(self, payload_hash: str) -> bool:
        return self.locate(payload_hash) is not None

    # -- segments --------------------------------------------------------------
    def put_segments(self, payload_hash: str, segments: list[Segment]) -> None:
        """Persist the structural segments (§6b) as a sidecar next to the text —
        a re-derivable projection the chunker reads back. Written beside whichever
        copy of the text it describes, for the same reason ``put`` is."""
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
