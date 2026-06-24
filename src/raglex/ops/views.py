"""Operations / observability views (§8).

The design is emphatic: **build the ops half first** — it's what keeps the corpus
healthy enough to be worth exploring, and what home-grown harvesters most often
lack. These are read-only projections over the catalogue, shared by the CLI
dashboard and the web API:

- **Source dashboard** — per adapter: last run, watermark, success/failure,
  reachability, doc count (catches "my feed silently broke 3 weeks ago").
- **Pipeline queue view** — where documents are stuck between stages.
- **Resolution worklist** — most-cited citations not yet harvested (§5b).
- **Corpus stats** — breakdown by doc_type / jurisdiction / tag for faceting.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from ..storage.catalogue import Catalogue


@dataclass(slots=True)
class SourceHealth:
    key: str
    last_run: str | None
    watermark: str | None
    consecutive_failures: int
    last_yield_at: str | None
    documents: int
    llm_extracted_ratio: float
    requires_js: bool
    requires_proxy: bool

    def to_dict(self) -> dict:
        return asdict(self)


def source_dashboard(catalogue: Catalogue) -> list[SourceHealth]:
    out: list[SourceHealth] = []
    for row in catalogue.all_sources():
        key = row["key"]
        out.append(
            SourceHealth(
                key=key,
                last_run=row["last_run"],
                watermark=row["watermark"],
                consecutive_failures=row["consecutive_failures"],
                last_yield_at=row["last_yield_at"],
                documents=catalogue.source_doc_count(key),
                llm_extracted_ratio=catalogue.llm_extracted_ratio(key),
                requires_js=bool(row["requires_js"]),
                requires_proxy=bool(row["requires_proxy"]),
            )
        )
    return out


@dataclass(slots=True)
class CorpusStats:
    total: int
    by_doc_type: dict[str, int]
    by_source: dict[str, int]
    by_upstream_status: dict[str, int]
    by_tag: dict[str, int]
    resolution: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def corpus_stats(catalogue: Catalogue) -> CorpusStats:
    counts = catalogue.corpus_counts()
    return CorpusStats(
        total=counts["total"],
        by_doc_type=counts["by_doc_type"],
        by_source=counts["by_source"],
        by_upstream_status=counts["by_upstream_status"],
        by_tag=catalogue.tag_counts(),
        resolution=catalogue.resolution_stats(),
    )


def pipeline_queues(catalogue: Catalogue) -> dict:
    return catalogue.queue_depths()


def resolution_worklist(catalogue: Catalogue, limit: int = 50) -> list[dict]:
    return [
        {"cite_count": r["cite_count"], "raw_citation_string": r["raw_citation_string"]}
        for r in catalogue.resolution_worklist(limit=limit)
    ]
