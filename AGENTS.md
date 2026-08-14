# Working on RagLex

Instructions for any coding agent or contributor working in this repository. Read this
before writing an adapter; read [`docs/adapter-authoring.md`](docs/adapter-authoring.md)
for the full contract and [`docs/job-authoring.md`](docs/job-authoring.md) for background
work.

RagLex harvests law. Its failures are almost never crashes — they are **silent
incompleteness**: a source that quietly stops early, a jurisdiction that quietly reads as
"Other", a retry that quietly files itself as done. Nothing in the corpus says a document
is missing, because a missing document leaves no trace. Everything below exists because
one of them happened.

---

## 1. If you report a resume cursor, you MUST accept one back

**This is the single most damaging mistake an adapter can make, and it is invisible.**

An adapter that sets `resume_offset` on its stubs:

```python
hints["resume_offset"] = offset          # "I can be resumed from here"
```

is making a promise. When a long backfill is interrupted — a redeploy, a restart, a
crash — `raglex.jobs` reads the checkpoint and **passes `start_offset` to your
constructor** on the retry. So the constructor must accept it:

```python
from ..core.adapter import BaseAdapter, option_int, resume_floor

def __init__(self, *, ..., start_offset: int | str | None = None):
    self.start_offset = resume_floor(option_int(start_offset, 0), PAGE_SIZE)
```

and `discover()` must honour it.

**What happens if you don't:** the resumed run raises `TypeError: __init__() got an
unexpected keyword argument 'start_offset'`, the job is recorded with `status='done'` and
the error buried in `result_json`, and a backfill that stopped 14,000 documents into
357,000 **appears finished in every list, panel and count**. On 2026-08-14 a routine
redeploy interrupted four backfills — Austria's VwGH, LVwG and BVwG, and Estonia's
lahend.ee — and all four did exactly this. A later audit found eleven more adapter
registrations with the same latent defect, across the ICO, CMA, Ofgem, Ofwat, GOV.UK, the
Commons and Lords Libraries, SPICe and the Irish committees. None had ever been noticed,
because the bug cannot fire until something interrupts a long run.

Rules:

- **Resume early, never late.** Always go through `core.adapter.resume_floor`, which
  backs off one page. Re-covering a page costs one listing request — the pipeline drops a
  stub whose document it already holds. Resuming one item late loses that document
  permanently. Page arithmetic always drifts, so push the error to the harmless side.
- **The cursor must count the whole feed.** If discovery is sliced (by year, by court, by
  month), a per-slice counter restarts the run in the middle of whichever slice it
  happened to reach. Finlex is walked year by year and had exactly this bug.
- **Do not skip pages you cannot bound.** Skipping requests is a fine optimisation only
  where the page count is known in advance. Where a window's size is unknown until you
  ask for it, skip *emission*, not requests — the saving that matters is the fetches, not
  the listing.

`tests/test_adapter_resume_contract.py` enforces this across the whole registry. If it
fails on your adapter, do not skip it.

## 2. Declare jurisdiction once, in `SourceInfo`

`SourceInfo` is the only place a source's jurisdiction, label and kind may be stated.
Never add a screen-local country table, and never extend one you find.

`Facade._jurisdiction_of` reads the registry. Its prefix table is a fallback for legacy
keys that predate the registry (`bailii`, `westlaw`, `hol`, `ico`) and for two imported
corpora with no adapter — **it is not somewhere new sources get added**. When it *was*
the only lookup, every source whose key did not begin with one of fifteen hardcoded
prefixes fell through to "Other": 358,176 documents, including the whole of Austria,
Finland, Estonia, Slovakia and Sweden, all of which had declared their jurisdiction
correctly when they were written. Guarded by `tests/test_jurisdiction_buckets.py`.

Add the country code to `JURISDICTION_LABELS` when you add a source for a new country. A
code with no label resolves to nothing and falls back to the prefix table, which is how a
whole country goes quiet.

## 3. A source that returns HTTP 200 has not necessarily succeeded

Assume the API lies. Every European case-law API in this corpus has at least one failure
that arrives as a 200:

- Austria's RIS answers a rejected parameter with an `Error` body inside a 200.
- Slovakia accepts its own output date format as a filter input and silently ignores it,
  returning all 4.68 million rows as though they were the week you asked for.
- Sweden's detail route answers a withdrawn publication with 200 and an empty body.
- Finland's listing hands back retrieval URLs that 404.

Read the body, not the status. Where a failure and an empty result look alike, raise —
never let "the source is broken" and "there is nothing there" produce the same outcome.

## 4. A complete-looking enumeration can still be a partial view

Sweden's paged list is deterministic (two full walks returned identical id sets) and its
own facet counts agree with it, and it still withholds 391 judgments, because it returns
one publication per *group*. Nothing the service reports reveals that they exist.

When adopting a register whose publisher offers no manifest, reconcile it **once** against
an independent inventory, and classify every difference before acting on it.

## 5. Never import a bulk snapshot without comparing it first

Storing a differing payload under a held `stable_id` archives the old version and advances
to a new one. A third-party dump of a source we already harvest is usually *worse* text,
so importing it wholesale puts worse documents in front of better ones and reports it as a
successful backfill. Scope the import to what the live source cannot supply.

## 6. Another agent may be working in this checkout

Stage the exact paths you changed and commit them together — a module and its registration
in one commit. Never `git commit -a`: it sweeps up the other agent's uncommitted work. If
you both edited `registry.py`, replay your own edits onto `git show HEAD:...` and stage
that, which is the reason to make registry edits with a script rather than by hand.

---

## Conventions

- Tests: `pytest`. The full suite must pass except known-pre-existing failures — say
  plainly which those are rather than describing the suite as green.
- Comments explain *why*, especially where the code looks odd because a source is odd.
  A comment that only restates the code is noise; one that records the failure the line
  prevents is why the line survives the next refactor.
- Long work goes through the job system, not `docker exec`.
