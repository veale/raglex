"""Pipeline runner — sequences the shared stages over one source's stubs (§5).

    discover → cheap topic gate → dedup (hash) → fetch (raw bytes)
            → store raw → confirm topic → catalogue + typed relations edges

The DB *is* the orchestration state (§5): watermarks advance only after a clean
run, so a crash re-pulls rather than skips, and a ``RateLimitException`` pauses
this source's queue (§5a) rather than failing the run. Extraction, resolution,
and embedding are later stages that read the catalogue's queues; this runner is
the step-1 ingest path.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from ..core.adapter import Adapter
from ..core.errors import FetchError, RateLimitException
from ..core.models import Record, UpstreamStatus
from ..storage.catalogue import Catalogue
from ..storage.rawstore import RawStore
from ..storage.textstore import TextStore
from ..topics import cheap_match, confirm

log = logging.getLogger("raglex.pipeline")


@dataclass(slots=True)
class RunStats:
    source: str
    discovered: int = 0
    gated_out: int = 0
    deduped: int = 0
    fetched: int = 0
    stored: int = 0
    off_topic: int = 0
    errors: int = 0
    # Why a fetch failed decides whether the caller may cool the item off for months.
    # A 404/410 (or an adapter that found nothing upstream) means "this item does not
    # exist" — safe to skip for a long time. A timeout / transport error / 429 means
    # "we couldn't tell" — cooling those off is how a whole worklist gets written out
    # of existence by one bad afternoon at the source.
    errors_fatal: int = 0
    errors_transient: int = 0
    not_found: int = 0
    rate_limited: bool = False
    watermark: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def outcome(self) -> str:
        """One word for what happened to a *targeted single-item* run — the vocabulary
        the harvest-drain uses to decide miss/retry/abort."""
        if self.rate_limited:
            return "rate_limited"
        if self.stored:
            return "stored"
        if self.errors_transient:
            return "transient"
        if self.errors_fatal or self.not_found:
            return "absent"
        if self.deduped:
            return "present"
        return "empty"

    def summary(self) -> str:
        return (
            f"[{self.source}] discovered={self.discovered} stored={self.stored} "
            f"gated_out={self.gated_out} deduped={self.deduped} off_topic={self.off_topic} "
            f"errors={self.errors}"
            + (" RATE_LIMITED" if self.rate_limited else "")
        )


class Pipeline:
    def __init__(
        self,
        catalogue: Catalogue,
        rawstore: RawStore,
        *,
        textstore: TextStore | None = None,
        topic_threshold: float = 3.0,
        skip_topic_gate: bool = False,
    ) -> None:
        self.catalogue = catalogue
        self.rawstore = rawstore
        self.textstore = textstore
        self.topic_threshold = topic_threshold
        # In-scope-by-construction sources (ICO/DPC/EDPB) are tagged, not gated (§4).
        self.skip_topic_gate = skip_topic_gate

    def run(
        self,
        adapter: Adapter,
        *,
        backfill: bool = False,
        since: str | None = None,
        max_pages: int | None = None,
        ignore_watermark: bool = False,
        record_health: bool = True,
    ) -> RunStats:
        """Run one source. ``backfill`` ignores the stored watermark and pages deep
        from ``since`` (§5). ``ignore_watermark`` runs with NO date cursor at all and
        doesn't advance the watermark — for a targeted **search** (e.g. discover-citing),
        which isn't an incremental feed crawl, so the newest-first cutoff would otherwise
        drop every older result. ``record_health=False`` skips the consecutive-failures
        counter — used for targeted single-item fetches where a 404 means "this item
        doesn't exist" rather than "the source feed is broken"."""
        stats = RunStats(source=adapter.source)
        watermark = None if ignore_watermark else (since if backfill else self.catalogue.get_watermark(adapter.source))
        highest = watermark

        try:
            for stub in adapter.discover(watermark, max_pages=max_pages):
                stats.discovered += 1

                # Skip a stub we ALREADY hold before paying to download+parse it (dedup
                # otherwise only fires on the payload hash, *after* the fetch). A query/
                # full-text harvest — e.g. discover-citing — returns mostly docs already in
                # the corpus, so this turns 50 needless fetches into 50 cheap PK lookups.
                # A backfill still re-fetches (to pick up upstream revisions).
                if not backfill and stub.stable_id and self.catalogue.get_document(stub.stable_id) is not None:
                    stats.deduped += 1
                    continue

                # Stage 1: cheap topic gate (§4) over the stub's cheap fields.
                if not self.skip_topic_gate and cheap_match(stub) is False:
                    stats.gated_out += 1
                    continue

                try:
                    record = adapter.fetch(stub)
                except RateLimitException:
                    # Pause THIS source's queue (§5a); leave the watermark un-advanced
                    # so the run resumes cleanly next time.
                    stats.rate_limited = True
                    stats.notes.append(f"rate limited on stub {stub.stable_id}")
                    log.warning("rate limited on %s; pausing source queue", adapter.source)
                    break
                except FetchError as exc:
                    stats.errors += 1
                    if exc.transient:
                        stats.errors_transient += 1
                    else:
                        stats.errors_fatal += 1
                        if stub.stable_id:
                            # A 404/410 for a known doc → flag upstream_status, never delete (§1.4a).
                            if self.catalogue.get_document(stub.stable_id) is not None:
                                self.catalogue.mark_upstream_status(
                                    stub.stable_id, UpstreamStatus.GONE_404
                                )
                    stats.notes.append(f"{stub.stable_id}: {exc}")
                    log.warning("fetch failed for %s: %s", stub.stable_id, exc)
                    continue

                if record is None:
                    # The adapter reached the source and found nothing there — an absence,
                    # not a failure (no bytes, no metadata). Distinct from a FetchError.
                    stats.not_found += 1
                    continue
                stats.fetched += 1

                if self._ingest(record, stats):
                    stats.stored += 1

                highest = _max_watermark(highest, stub.hint_date and stub.hint_date.isoformat())

        except RateLimitException:
            stats.rate_limited = True
        finally:
            failed = stats.errors > 0 and stats.stored == 0
            if record_health:
                self.catalogue.record_run(
                    adapter.source, yielded=stats.stored > 0, failed=failed
                )

        # Advance the watermark only on a clean (non-rate-limited) crawl (§5) — never for
        # a targeted search, which isn't an incremental pass over the recency feed.
        if highest and not stats.rate_limited and not ignore_watermark:
            self.catalogue.set_watermark(adapter.source, highest)
            stats.watermark = highest

        log.info(stats.summary())
        return stats

    def _ingest(self, record: Record, stats: RunStats) -> bool:
        """Dedup → store raw → confirm topic → catalogue. Returns True if stored."""
        record.ensure_payload_hash()

        # Outstanding amendments (§0): (re)schedule the effects re-check BEFORE the
        # dedup early-return, so even an unchanged re-fetch pushes the next check out
        # (otherwise a stale-but-unchanged Act would be re-pulled every tick). A zero
        # count clears the queue row — the editors have caught up.
        eff = record.extra.get("unapplied_effects") if record.extra else None
        if eff is not None:
            self.catalogue.record_outstanding_effects(
                record.stable_id, eff.get("outstanding", 0), eff.get("affecting", []),
            )

        # Mint this record's resolution aliases BEFORE the dedup early-return. They are
        # cheap idempotent writes that citing edges resolve against, and a re-fetch of an
        # already-held case (a CJEU case cited by a guessed …CJ… descriptor that we already
        # hold under its real …CO…/ECLI) dedups here — so minting only on the store path
        # would leave those edges pending forever even though the target is present.
        self._mint_aliases(record)

        # Content-hash dedup (§5): identical bytes → skip the expensive downstream
        # work even when the feed bumped 'last modified'.
        if record.payload_hash and self.catalogue.payload_hash_seen(record.payload_hash):
            stats.deduped += 1
            return False

        # Stage 2: confirm topic over full text (§4), unless this source is
        # in-scope by construction.
        if not self.skip_topic_gate and record.text:
            result = confirm(record.text, threshold=self.topic_threshold)
            if not result.keep:
                stats.off_topic += 1
                return False
            record.topic_tags = list(dict.fromkeys([*record.topic_tags, *result.tags]))
            record.topic_score = result.score

        raw_path = None
        if record.raw_bytes is not None:
            digest = self.rawstore.put(record.raw_bytes, ext=record.raw_ext)
            raw_path = str(self.rawstore.path_for(digest, record.raw_ext))

        # Persist the extracted-text projection (§1.2) so the tagging engine and
        # chunker can read it back by char span.
        text_path = None
        if self.textstore is not None and record.text and record.payload_hash:
            text_path = str(self.textstore.put(record.payload_hash, record.text))
            # Persist the adapter's structural segments alongside the text (§6b).
            self.textstore.put_segments(record.payload_hash, record.segments)

        self.catalogue.upsert_document(record, raw_path=raw_path, text_path=text_path)
        return True

    def _mint_aliases(self, record: Record) -> None:
        """Register the resolution aliases a document's citing edges key off (§5b).
        Idempotent, and safe to call for a node that isn't stored yet — the resolver
        confirms the target exists at resolve time."""
        # Map this doc's CELEX → its ECLI so case-number citations ("C-311/18",
        # whose grammar candidate is the CELEX) resolve to the ECLI-keyed node (§5b).
        celex = record.extra.get("celex") if record.extra else None
        if celex and record.ecli:
            self.catalogue.put_alias(celex.casefold(), record.ecli, source="celex-ecli")
        # Alternate CELEXes the corpus *cites* this document by (§5b). A CJEU case number
        # gives no hint whether the case ended in a judgment (…CJ…) or an order (…CO…), so
        # the grammar guesses; the targeted fetch resolves the real descriptor and records
        # the guess here. Without these aliases the fetched case would sit in the corpus
        # while every edge citing the guessed form stayed pending forever.
        for alias in (record.extra.get("celex_aliases") if record.extra else None) or ():
            if alias and record.ecli:
                self.catalogue.put_alias(str(alias).casefold(), record.ecli, source="celex-ecli")
        # ECHR application numbers → ECLI (§5b): Strasbourg cases are cited by application
        # number ("6878/75"), often several per case, but the document is keyed by ECLI —
        # so without this every appno citation of a held case stays pending forever. Bare
        # appno candidates are ECHR by construction (the CJEU grammars mint a CELEX, never
        # a bare number), so the mapping is unambiguous.
        appnos = record.extra.get("appno") if record.extra else None
        if appnos and record.ecli:
            for a in re.split(r"[;,]", str(appnos)):
                a = a.strip()
                if a:
                    self.catalogue.put_alias(a.casefold(), record.ecli, source="echr-appno")
        # Tribunal/court chamber recovery (§5b): a UK Find Case Law id carries the
        # chamber as a path segment (ukut/aac/2012/440), but a citation may omit it
        # ("[2012] UKUT 440" → ukut/2012/440). Mint the chamber-less alias so the
        # bare citation resolves to this node.
        bare = _chamberless_alias(record.stable_id)
        if bare:
            self.catalogue.put_alias(bare, record.stable_id, source="chamber-alias")


def _chamberless_alias(stable_id: str) -> str | None:
    """For a 4-segment UK FCL slug ``court/chamber/year/num`` (chamber alphabetic),
    return the chamber-less ``court/year/num``; else None."""
    parts = stable_id.split("/")
    if (len(parts) == 4 and parts[0].isalpha() and parts[1].isalpha()
            and len(parts[2]) == 4 and parts[2].isdigit() and parts[3].isdigit()):
        return f"{parts[0]}/{parts[2]}/{parts[3]}".casefold()
    return None


def _max_watermark(current: str | None, candidate: str | None) -> str | None:
    if candidate is None:
        return current
    if current is None:
        return candidate
    return max(current, candidate)
