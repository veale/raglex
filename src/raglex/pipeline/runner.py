"""Pipeline runner — sequences the shared stages over one source's stubs (§5).

    discover → dedup (hash) → fetch (raw bytes)
            → store raw → catalogue + typed relations edges

The DB *is* the orchestration state (§5): watermarks advance only after a clean
run, so a crash re-pulls rather than skips, and a ``RateLimitException`` pauses
this source's queue (§5a) rather than failing the run. Extraction, resolution,
and embedding are later stages that read the catalogue's queues; this runner is
the step-1 ingest path.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from ..core.adapter import Adapter
from ..core.errors import FetchError, RateLimitException
from ..core.models import DocType, Record, RelationshipType, Stub, UpstreamStatus
from ..storage.catalogue import Catalogue
from ..storage.rawstore import RawStore
from ..storage.textstore import TextStore

log = logging.getLogger("raglex.pipeline")


@dataclass(slots=True)
class RunStats:
    source: str
    discovered: int = 0
    deduped: int = 0
    fetched: int = 0
    stored: int = 0
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
    # stable_ids re-fetched because the source said the content CHANGED (e.g. Find Case
    # Law's contenthash) — not new documents, but they need re-extraction like new ones.
    refreshed_ids: list[str] = field(default_factory=list)
    # Internal hand-off to the facade's extraction pass.  Avoids two full-table
    # all_stable_ids() scans (and a million-element set diff) after a large bulk seed.
    stored_ids: list[str] = field(default_factory=list)

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
            f"deduped={self.deduped} errors={self.errors}"
            + (" RATE_LIMITED" if self.rate_limited else "")
        )


class Pipeline:
    def __init__(
        self,
        catalogue: Catalogue,
        rawstore: RawStore,
        *,
        textstore: TextStore | None = None,
    ) -> None:
        self.catalogue = catalogue
        self.rawstore = rawstore
        self.textstore = textstore

    def run(
        self,
        adapter: Adapter,
        *,
        backfill: bool = False,
        refetch_held: bool = False,
        since: str | None = None,
        max_pages: int | None = None,
        ignore_watermark: bool = False,
        record_health: bool = True,
        watermark_key: str | None = None,
        overlap_days: int | None = None,
        force_full: bool = False,
        on_progress=None,
        cancel_check=None,
    ) -> RunStats:
        """Run one source. ``backfill`` ignores the stored watermark and pages deep
        from ``since`` (§5); it now SKIPS already-held documents (unchanged) so a
        "get everything" sweep advances into the never-fetched tail instead of
        re-downloading the corpus on every run. ``refetch_held`` opts back into
        re-fetching held docs — for a *targeted* re-pull that needs the current
        upstream state (the effects-refresh worker re-reads outstanding amendments).
        ``ignore_watermark`` runs with NO date cursor at all and
        doesn't advance the watermark — for a targeted **search** (e.g. discover-citing),
        which isn't an incremental feed crawl, so the newest-first cutoff would otherwise
        drop every older result. ``record_health=False`` skips the consecutive-failures
        counter — used for targeted single-item fetches where a 404 means "this item
        doesn't exist" rather than "the source feed is broken".

        ``watermark_key`` scopes the incremental cursor. Two watches on the same source
        with different queries see different slices of the feed — sharing the source-wide
        cursor means whichever ran last pushes the other's cursor past everything it would
        have found, so a fresh query-watch never sees a single document."""
        import time as _time

        stats = RunStats(source=adapter.source)
        wm_key = watermark_key or adapter.source
        watermark = None if ignore_watermark else (since if backfill else self.catalogue.get_watermark(wm_key))
        # ``highest`` seeds from the STORED cursor, never the overlap-adjusted one, so a
        # quiet run (nothing new, only the re-scanned overlap window) can never regress the
        # watermark backwards. The overlap only widens what discover() is *asked* for.
        highest = watermark
        # Incremental overlap (§keep-current): re-ask the feed for a small window BEFORE the
        # stored cursor so a late-arriving or same-boundary item isn't stranded, and future-
        # dated items can't push the cursor past reality. Backfills (no cursor) and targeted
        # searches (ignore_watermark) are exempt. Re-seen items dedup by PK before any fetch,
        # so the cost is ~nil. Generalises the CanLII ``today−2d`` re-scan window.
        discover_since = watermark
        if watermark and not backfill and not ignore_watermark:
            overlap = _overlap_days(overlap_days)
            if overlap > 0:
                discover_since = _apply_overlap(watermark, overlap)
        # Backfill frontier — the fix for a repeat "backfill everything" re-walking the whole
        # upstream catalogue and deduping every item (observed: uk-caselaw re-discovered
        # 76,400 held docs, stored 0). A backfill's job is to reach what we DON'T hold by
        # walking the feed to its end; once one has completed cleanly there are no gaps below
        # the point it reached, so the only thing a later backfill can add is the newer tail.
        # Resume from the recorded frontier instead of re-walking — unless the caller forces a
        # full re-walk (force_full) or wants held docs refetched.
        backfill_frontier_key = f"backfill:{wm_key}"
        if (backfill and not since and not force_full and not refetch_held
                and not ignore_watermark):
            frontier = self.catalogue.get_watermark(backfill_frontier_key)
            if frontier:
                discover_since = frontier
                highest = _max_watermark(highest, frontier)
        cancelled = False  # set when a cooperative cancel breaks the crawl early
        wm_frozen = False  # a transient fetch failure freezes the cursor at that stub
        last_emit = 0.0

        # An immediate heartbeat so the Jobs panel shows "discovering …" the moment the run
        # starts, not "starting…". The held-prefilter below buffers ~200 stubs before the
        # loop yields its first item, and against a rate-limited feed that fill can take a
        # while — without this the job looks frozen until the first batch completes.
        if on_progress:
            on_progress(stage=f"discovering {adapter.source}", done=0)
        stubs = adapter.discover(discover_since, max_pages=max_pages)
        # Batched held-lookup: a backfill's resume pass re-walks the source's whole
        # catalogue mostly re-seeing held documents, and one point SELECT per stub
        # made that walk crawl at ~20 stubs/s against Postgres (hours of no-op over
        # a 300k-item TOC). The prefilter answers "held? extracted?" for 200 stubs
        # in one IN-query; every decision the loop takes per stub is unchanged.
        annotated = (((s, None, None, None) for s in stubs) if refetch_held
                     else self._batched_held(stubs))

        try:
            for stub, held_id, held_has_text, held_extracted in annotated:
                if cancel_check and cancel_check():
                    stats.notes.append("cancelled")
                    cancelled = True
                    break
                stats.discovered += 1
                # Heartbeat so a long crawl (the EDPB backfill fetches hundreds of PDFs
                # at a slow, WAF-safe pace) keeps the job alive and shows live progress,
                # instead of looking frozen behind one silent "harvesting" line.
                # Time-throttled, not per-stub: each callback costs a ~3ms GIL-yield in
                # the job runner, which across a million-stub dedup walk is ~50 minutes
                # of pure sleep. A slow crawl (seconds per item) still reports every
                # item; a fast walk reports a few times a second. The resume-offset
                # checkpoint rides these events, so at worst a restart replays the few
                # hundred stubs since the last emit — all deduped.
                now = _time.monotonic()
                if on_progress and now - last_emit >= 0.25:
                    last_emit = now
                    progress = {
                        "stage": f"harvesting {adapter.source}",
                        "done": stats.discovered,
                        "stored": stats.stored,
                        "item": stub.stable_id,
                    }
                    # A feed crawl doesn't normally know how many items exist until it has
                    # walked the whole feed, so the harvest phase shows a running count and
                    # no progress bar. When the adapter's API DOES report a total (e.g.
                    # CourtListener's paginated ``count``), it rides in on the stub as
                    # ``feed_total`` — surface it so the Jobs panel can draw a real bar.
                    feed_total = stub.hints.get("feed_total")
                    if feed_total:
                        progress["total"] = int(feed_total)
                    if stub.hints.get("resume_offset") is not None:
                        progress["_checkpoint"] = {
                            "phase": "discover",
                            "source": adapter.source,
                            "resume_offset": int(stub.hints["resume_offset"]),
                        }
                    on_progress(**progress)

                # Skip a stub we ALREADY hold before paying to download+parse it (dedup
                # otherwise only fires on the payload hash, *after* the fetch). A query/
                # full-text harvest — e.g. discover-citing — returns mostly docs already in
                # the corpus, so this turns 50 needless fetches into 50 cheap PK lookups.
                #
                # This applies on BACKFILL too. A "get everything" pass that re-downloaded
                # the whole corpus on every run (the NZ Supreme Court complaint) never made
                # progress into the never-fetched tail — the point of a backfill is to reach
                # what we DON'T hold, so already-held items should fall straight through. A
                # genuine upstream revision is still picked up: the contenthash-changed
                # branch below re-fetches those.
                #
                # The held check is by id, then by landing URL — the latter for adapters
                # whose stub id is provisional until the document is fetched (NZ), where an
                # id lookup can never match a doc already keyed by its real neutral citation.
                # Both lookups were answered by the batched prefilter above (held_id /
                # held_extracted ride in with the stub); refetch_held skips it entirely.
                refreshed = False
                if held_id is not None and held_has_text is not False:
                    # The pending-CJEU feed deliberately re-enumerates resolving
                    # decisions.  If the held copy is still French, fetch again because
                    # the English rendition may have appeared without changing the
                    # decision date.  If it is already full English, use the cheap sighting
                    # to retire any linked CN/TN notice even when this decision predates
                    # the feature and therefore has no stored supersedes edge yet.
                    english_refresh = bool(stub.hints.get("refetch_if_not_english"))
                    held_doc = self.catalogue.get_document(held_id) if english_refresh else None
                    held_is_english = bool(
                        held_doc is not None and held_doc["has_text"]
                        and str(held_doc["source_language"] or "").lower() == "en"
                    )
                    if held_is_english:
                        # Reassert the decision's exact CELEX alias.  During an initial
                        # seed the notice is deliberately seen first and temporarily owns
                        # the guessed judgment CELEX; an already-held decision is deduped,
                        # so without this write the temporary mapping would survive.
                        decision_celex = stub.hints.get("celex")
                        if decision_celex:
                            self.catalogue.put_alias(
                                str(decision_celex).casefold(), held_id,
                                source="celex-ecli", overwrite=True,
                            )
                        for notice_id in stub.hints.get("resolved_notices", []):
                            self.catalogue.retire_pending_eu_notice(notice_id, held_id)
                    if (english_refresh and not held_is_english
                            and _english_recheck_due(held_doc)):
                        refreshed = True
                    else:
                        # …unless the feed says the content CHANGED: a differing
                        # contenthash (FCL's change signal) means the held copy is a
                        # superseded revision — re-fetch it. No hash on either side →
                        # assume unchanged (the old rule).
                        feed_hash = stub.hints.get("contenthash")
                        held_hash = (self.catalogue.document_meta(held_id) or {}).get(
                            "contenthash") if feed_hash else None
                        if not (feed_hash and held_hash and feed_hash != held_hash):
                            stats.deduped += 1
                            # A durable document is not necessarily a completed pipeline
                            # item. Carry held-but-unscanned ids into extraction.
                            if held_extracted is None:
                                held_doc = self.catalogue.get_document(held_id)
                                held_extracted = bool(
                                    held_doc["last_extracted_at"]) if held_doc else True
                            if not held_extracted:
                                stats.stored_ids.append(held_id)
                            # A deduped stub was still seen and held: advance the cursor.
                            if not wm_frozen:
                                highest = _max_watermark(
                                    highest,
                                    stub.hints.get("watermark")
                                    or (stub.hint_date and stub.hint_date.isoformat()),
                                )
                            continue
                        refreshed = True

                # Fetch is usually the longest opaque operation (network retries,
                # download and parsing). Announce the exact item immediately before
                # blocking; the throttled discovery heartbeat may name a preceding
                # deduped stub on a short targeted run.
                if on_progress:
                    fetch_progress = {
                        "stage": f"fetching {adapter.source}",
                        "done": stats.discovered,
                        "stored": stats.stored,
                        "item": stub.stable_id,
                    }
                    if stub.hints.get("feed_total"):
                        fetch_progress["total"] = int(stub.hints["feed_total"])
                    on_progress(**fetch_progress)
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
                        # The item probably exists; we just couldn't get it NOW. Freeze
                        # the cursor here so the next incremental run re-reaches this
                        # stub and retries — advancing past it writes it off until its
                        # upstream timestamp happens to move again.
                        wm_frozen = True
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
                except Exception as exc:  # noqa: BLE001
                    # ONE malformed document must never sink a whole source run. A parser
                    # blowing up on a corrupt PDF ("Failed to open stream"), a surprise
                    # encoding, an adapter bug — previously any of these propagated out of
                    # the crawl and failed the job, losing every item after it. Treat it as
                    # a transient item error: record it, freeze the cursor so the item is
                    # retried, and carry on with the rest.
                    stats.errors += 1
                    stats.errors_transient += 1
                    wm_frozen = True
                    stats.notes.append(f"{stub.stable_id}: {type(exc).__name__}: {exc}")
                    log.exception("unexpected error fetching %s", stub.stable_id)
                    continue

                if record is None:
                    # The adapter reached the source and found nothing there — an absence,
                    # not a failure (no bytes, no metadata). Distinct from a FetchError.
                    stats.not_found += 1
                    continue
                stats.fetched += 1
                if on_progress:
                    store_progress = {
                        "stage": f"storing {adapter.source}",
                        "done": stats.discovered,
                        "stored": stats.stored,
                        "item": record.stable_id,
                    }
                    if stub.hints.get("feed_total"):
                        store_progress["total"] = int(stub.hints["feed_total"])
                    on_progress(**store_progress)

                # Provisional-id dedup (§5). Some adapters can only mint a stub's real
                # identity by fetching it: the id is the neutral citation printed inside
                # the PDF (NZ Supreme Court) or read off the judgment's detail page
                # (Ireland), so the discovery stub carries a placeholder id the prefilter
                # can't match against a copy already held under its real citation — e.g. a
                # case seeded by a bulk import. Without this, re-reaching such a case would
                # archive the held version and supersede it with this source's copy on
                # every backfill. When fetch() reveals a real id different from the stub's
                # and we already hold it, treat it as deduped (unless a deliberate
                # refetch/refresh). A genuine upstream revision still stores: it keeps the
                # stub's id, or is flagged `refreshed` by the contenthash-change path.
                if record.stable_id != stub.stable_id and not refetch_held and not refreshed:
                    real_state = self.catalogue.held_extraction_state([record.stable_id])
                    # A full-text held copy wins.  A metadata-only copy is deliberately
                    # replaceable: this fetch may be the source that finally supplies text.
                    if real_state.get(record.stable_id, (False, False))[0]:
                        stats.deduped += 1
                        if not wm_frozen:
                            highest = _max_watermark(
                                highest,
                                stub.hints.get("watermark")
                                or (stub.hint_date and stub.hint_date.isoformat()),
                            )
                        continue

                try:
                    stored = self._ingest(record, stats)
                except Exception as exc:  # noqa: BLE001
                    # Same rule as the fetch guard above, for the store half: a record
                    # that reached us intact can still be unstorable (text carrying a
                    # lone surrogate or a NUL that UTF-8/psycopg refuse to encode, an
                    # over-long field…). Failing the item is right; failing the run —
                    # losing every item after it — is not.
                    stats.errors += 1
                    stats.errors_transient += 1
                    wm_frozen = True
                    stats.notes.append(f"{record.stable_id}: {type(exc).__name__}: {exc}")
                    log.exception("unexpected error storing %s", record.stable_id)
                    continue

                if not stored and refreshed and held_id:
                    # We re-fetched this document ON PURPOSE — to see whether its English
                    # rendition had appeared — and the bytes came back unchanged, so
                    # _ingest deduped it and nothing was written. Record the attempt
                    # anyway. Without this the backoff below can never re-arm:
                    # ``fetched_at`` is only written on a store, so a document that never
                    # changes stays permanently overdue and is re-downloaded on EVERY
                    # run. Measured on the live corpus: 35,967 CJEU decisions in exactly
                    # that state, a run fetching 547 of them to store 0.
                    self.catalogue.note_refetch(
                        held_id, source_language=record.source_language)
                if stored:
                    stats.stored += 1
                    stats.stored_ids.append(record.stable_id)
                    if refreshed:
                        stats.refreshed_ids.append(record.stable_id)

                # A feed can carry a finer cursor than the date (hints["watermark"], e.g.
                # FCL's full <updated> timestamp) — prefer it; date-only cursors lose
                # same-day arrivals.
                if not wm_frozen:
                    highest = _max_watermark(
                        highest,
                        stub.hints.get("watermark")
                        or (stub.hint_date and stub.hint_date.isoformat()),
                    )

        except RateLimitException:
            stats.rate_limited = True
        finally:
            failed = stats.errors > 0 and stats.stored == 0
            if record_health:
                self.catalogue.record_run(
                    adapter.source, yielded=stats.stored > 0, failed=failed
                )

        # Advance the watermark only on a clean (non-rate-limited) crawl (§5) — never for
        # a targeted search, which isn't an incremental pass over the recency feed. Clamp a
        # date cursor to today first: a single future-dated item (a data-entry error, an
        # embargo/commencement date) would otherwise jump the cursor past real time and
        # silently strand every genuinely-new item dated "today" until the clock caught up.
        if highest and not stats.rate_limited and not ignore_watermark:
            highest = _clamp_future(highest)
            self.catalogue.set_watermark(wm_key, highest)
            stats.watermark = highest

        # Record the backfill frontier only when a FULL backfill completed cleanly — reached
        # the end of the feed (not cancelled, not rate-limited, not capped by max_pages) with
        # no item left frozen for retry. Then the next backfill resumes from here (above)
        # instead of re-walking the whole catalogue. A frontier-resumed run that finds nothing
        # new carries the frontier forward unchanged (highest was seeded from it).
        if (backfill and highest and not cancelled and not stats.rate_limited
                and not wm_frozen and max_pages is None):
            self.catalogue.set_watermark(backfill_frontier_key, _clamp_future(highest))

        log.info(stats.summary())
        return stats

    # how many stubs the held-prefilter answers per IN-query. Buffering delays the
    # first fetch by at most this many discovery steps — irrelevant for the bulk
    # walks it exists for, and a short feed just flushes at end-of-stream.
    _HELD_BATCH = 200

    def _batched_held(self, stubs):
        """Annotate each stub with ``(held_id, has_extraction_stamp)`` via batched
        catalogue lookups — see run(). ``held_extracted`` is None when unknown (a
        landing-URL match, whose id differs from the stub's), resolved lazily by the
        one consumer that needs it."""
        buf: list[Stub] = []
        for stub in stubs:
            buf.append(stub)
            if len(buf) >= self._HELD_BATCH:
                yield from self._annotate_held(buf)
                buf = []
        if buf:
            yield from self._annotate_held(buf)

    def _annotate_held(self, buf: "list[Stub]"):
        state = self.catalogue.held_extraction_state(
            [s.stable_id for s in buf if s.stable_id])
        by_url = self.catalogue.document_ids_by_landing_urls(
            [s.landing_url for s in buf
             if s.landing_url and s.stable_id not in state])
        # third rung: a stub id that is an upstream surrogate of a held document
        # (de-rii's doknr aliases onto the ECLI the decision is held under) — the
        # only alternative was reading + parsing the file to learn its real id
        misses = [s.stable_id for s in buf
                  if s.stable_id and s.stable_id not in state
                  and (s.landing_url or "") not in by_url]
        by_alias = self.catalogue.alias_targets(misses) if misses else {}
        alias_state = self.catalogue.held_extraction_state(
            list(by_alias.values())) if by_alias else {}
        for s in buf:
            if s.stable_id and s.stable_id in state:
                has_text, extracted = state[s.stable_id]
                yield s, s.stable_id, has_text, extracted
            elif s.landing_url and s.landing_url in by_url:
                held_id = by_url[s.landing_url]
                has_text, extracted = self.catalogue.held_extraction_state(
                    [held_id]).get(held_id, (True, False))
                yield s, held_id, has_text, extracted
            elif s.stable_id and by_alias.get(s.stable_id) in alias_state:
                dst = by_alias[s.stable_id]
                has_text, extracted = alias_state[dst]
                yield s, dst, has_text, extracted
            else:
                yield s, None, None, None

    def _ingest(self, record: Record, stats: RunStats) -> bool:
        """Dedup → store raw → catalogue. Returns True if stored."""
        # Broad regulator registers mix decisions and legal guidance with speeches,
        # vacancies and newsletters. Sources may opt into a strict relevance gate:
        # keep the fetched bytes/text and content hash as a durable processed row,
        # but suppress it from search unless a grammar sees a case or legislation.
        if record.extra.get("require_recognized_legal_citation"):
            from ..citations.extractor import all_grammar_citations

            legal_kinds = {
                "case", "opinion", "act", "regulation", "directive", "decision",
                "treaty", "eu_instrument",
            }
            # The full grammar set, not just the registered (anglophone) ones: a Dutch
            # DPA decision cites "artikel 5 AVG" and a French one "article 6 du RGPD",
            # neither of which the English grammars see — so the gate used to exclude
            # every continental regulator document from search for citing nothing.
            legal = [
                c for c in all_grammar_citations(record.text or "")
                if c.entity_kind in legal_kinds
            ]
            # Authoritative structured provision metadata is stronger than a grammar
            # match (e.g. the Irish DPC's explicit GDPR-Articles facet).
            structured = [
                r for r in record.relations
                if r.dst_id or r.raw_citation_string
            ]
            record.extra["recognized_legal_citations"] = len(legal) + len(structured)
            if legal or structured:
                record.extra.pop("search_excluded", None)
                record.extra.pop("search_exclusion_reason", None)
            else:
                record.extra["search_excluded"] = True
                record.extra["search_exclusion_reason"] = (
                    "no_recognized_case_or_legislation"
                )
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
        # …but first: is this the SAME authority the corpus already holds from another
        # register, under a better identifier? A record with no identifier of its own,
        # whose declared alias already names a held document, is another rendition — not
        # a new one. Minting it anyway forks the corpus: the NeuRIS backfill was on its
        # way to a second copy of all 83,465 German federal decisions, keyed so that
        # nothing could ever link the two.
        held = self._reconcile_identity(record)
        if held is not None:
            stats.deduped += 1
            self.catalogue.record_rendition(held, record.source, record.stable_id)
            return False

        self._mint_aliases(record)

        # Content-hash dedup (§5): identical bytes → skip the expensive downstream
        # work even when the feed bumped 'last modified'.
        if record.payload_hash and self.catalogue.payload_hash_seen(record.payload_hash):
            stats.deduped += 1
            return False

        raw_path = None
        if record.raw_bytes is not None:
            digest = self.rawstore.put(record.raw_bytes, ext=record.raw_ext)
            raw_path = str(self.rawstore.path_for(digest, record.raw_ext))

        # Persist the extracted-text projection (§1.2) so the tagging engine and
        # chunker can read it back by char span.
        text_path = None
        if self.textstore is not None and record.text and record.payload_hash:
            text_path = str(self.textstore.put(record.payload_hash, record.text,
                                               source=record.source))
            # Persist the adapter's structural segments alongside the text (§6b).
            self.textstore.put_segments(record.payload_hash, record.segments)

        self.catalogue.upsert_document(record, raw_path=raw_path, text_path=text_path)
        # A CN/TN notice is useful while the case is pending, but becomes duplicate
        # search noise once the dossier's full English judgment/order is held.  Keep the
        # notice append-only and auditable; only remove it from retrieval surfaces.
        if (record.source == "eu-cellar" and record.source_language == "en"
                and record.doc_type in (DocType.JUDGMENT, DocType.DECISION)):
            for rel in record.relations:
                if rel.relationship_type == RelationshipType.SUPERSEDES and rel.dst_id:
                    self.catalogue.retire_pending_eu_notice(rel.dst_id, record.stable_id)
        # A newly held consolidation makes a more accurate target available for
        # citations that already point at the base law. Rebuild those derived links
        # immediately; otherwise they would appear only after every citing document
        # happened to be re-extracted.
        version_bases = {
            rel.dst_id
            for rel in record.relations
            if rel.dst_id
            and rel.relationship_type in {
                RelationshipType.CONSOLIDATES,
                RelationshipType.POINT_IN_TIME_OF,
            }
        }
        for base_id in version_bases:
            self.catalogue.refresh_applicable_version_links(base_id)
        return True

    def _reconcile_identity(self, record: Record) -> str | None:
        """The held document this record is another rendition OF, or None if it is new.

        Deliberately narrow, because merging two documents wrongly is worse than holding
        two: only a record with NO identifier of its own (no ECLI), whose adapter-declared
        alias resolves to a document from a DIFFERENT source that already has text."""
        if record.ecli:
            return None
        for alias in (record.extra.get("aliases") if record.extra else None) or ():
            if not alias:
                continue
            held = self.catalogue.find_document_id(str(alias))
            if not held or held == record.stable_id:
                continue
            doc = self.catalogue.get_document(held)
            if doc is not None and doc["source"] != record.source and doc["has_text"]:
                return held
        return None

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
        # Generic adapter-declared aliases (§5b): forms the corpus cites this document
        # by that aren't ECLI/CELEX-shaped — e.g. an EDPB register decision's EDPBI
        # identifier. The adapter states them in extra["aliases"]; they resolve to the
        # document's stable_id.
        # First writer wins: an adapter-declared alias must not RE-POINT a key that
        # already names another held document. Two registers publish the same German
        # judgment, and the second one through re-pointed 2,960 docket keys away from the
        # ECLI-keyed copies — so a citation resolved to a rendition with no ECLI and none
        # of the edges the original had.
        for alias in (record.extra.get("aliases") if record.extra else None) or ():
            if alias:
                self.catalogue.put_alias(str(alias).casefold(), record.stable_id,
                                         source="adapter-alias",
                                         # A pending CN/TN notice is authoritative for its
                                         # own C/T family. It must dislodge an old cross-
                                         # family fallback (C-631/24 and T-631/24 can both
                                         # exist); the later full decision reclaims the
                                         # exact CELEX above.
                                         overwrite=bool(record.extra.get("pending")))
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


_DEFAULT_OVERLAP_DAYS = 2  # the CanLII re-scan window, generalised
_ISO_DATE_HEAD = re.compile(r"(\d{4})-(\d{2})-(\d{2})")

#: How long to leave a non-English CJEU decision alone before asking CELLAR again
#: whether its English rendition has appeared.
#:
#: Without a gate this re-fetch had NO backoff: the pending C/T feed re-enumerates its
#: ~700 resolving decisions every run, a large share are held in French only, and each
#: run therefore re-downloaded every one of them, got French again, and stored nothing.
#: Observed on the live corpus: consecutive runs reporting ``stored: 0`` after fetching
#: 375, 507, 297 and 638 documents, each run taking about an hour, none ever finishing —
#: so the watermark never advanced and the next run repeated the identical work. Most of
#: those decisions will never be translated, so "check again next run" is a loop, not a
#: retry. ``fetched_at`` already records the last attempt, so the backoff needs no new
#: state: a re-fetch updates it, and a document not fetched for this long is due again.
_ENGLISH_RECHECK_DAYS = int(os.environ.get("RAGLEX_ENGLISH_RECHECK_DAYS") or 14)


def _english_recheck_due(held_doc) -> bool:
    """Whether a non-English held decision is due another look for its translation."""
    if held_doc is None:
        return True
    fetched = str((held_doc["fetched_at"] if "fetched_at" in held_doc.keys()
                   else None) or "")
    m = _ISO_DATE_HEAD.match(fetched)
    if not m:
        return True                     # never recorded → treat as due
    try:
        last = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return True
    return (date.today() - last) >= timedelta(days=_ENGLISH_RECHECK_DAYS)


def _overlap_days(override: int | None) -> int:
    """Per-run override → global ``RAGLEX_INCREMENTAL_OVERLAP_DAYS`` → default. A value of
    0 disables the overlap (exact-cursor behaviour, as before)."""
    if override is not None:
        return max(0, override)
    raw = (os.environ.get("RAGLEX_INCREMENTAL_OVERLAP_DAYS") or "").strip()
    if raw:
        try:
            return max(0, int(raw))
        except ValueError:
            pass
    return _DEFAULT_OVERLAP_DAYS


def _shift_date_head(wm: str, days: int) -> str | None:
    """Shift the leading ``YYYY-MM-DD`` of a watermark by ``days`` (negative = earlier),
    preserving any time/timezone suffix exactly. Returns None if the watermark doesn't
    start with an ISO date — serials (edpb-oss ``00003927``) and id-shaped cursors
    (nz-legislation ``act_public_…``) are left for the caller to pass through unchanged."""
    m = _ISO_DATE_HEAD.match(wm)
    if not m:
        return None
    try:
        shifted = date(int(m[1]), int(m[2]), int(m[3])) + timedelta(days=days)
    except ValueError:
        return None
    return shifted.isoformat() + wm[10:]


def _apply_overlap(watermark: str, overlap_days: int) -> str:
    """The value passed to ``discover()``: the stored cursor rolled back ``overlap_days``.
    Date-aware; a non-date watermark is returned unchanged (those sources full-walk and
    filter anyway, so an overlap is a no-op there)."""
    return _shift_date_head(watermark, -overlap_days) or watermark


def _clamp_future(watermark: str) -> str:
    """Never store a date cursor beyond today — see run(). Non-date watermarks pass through."""
    m = _ISO_DATE_HEAD.match(watermark)
    if not m:
        return watermark
    try:
        d = date(int(m[1]), int(m[2]), int(m[3]))
    except ValueError:
        return watermark
    today = date.today()
    return today.isoformat() + watermark[10:] if d > today else watermark


def _max_watermark(current: str | None, candidate: str | None) -> str | None:
    if candidate is None:
        return current
    if current is None:
        return candidate
    return max(current, candidate)
