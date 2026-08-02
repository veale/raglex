"""One judgment, one node — the UK division/chamber identity work.

A UK neutral citation names the division in brackets ("[2013] EWHC 3560 (Comm)"), and
judges routinely leave it out. So the corpus mints a CHAMBER-LESS alias for every held
UK judgment — ``ewhc/comm/2013/3560`` also answers to ``ewhc/2013/3560`` — which is what
lets a bare citation reach the judgment at all. 98,521 of those aliases exist and most
earn their keep.

The flaw is what happens when two judgments share a number. **UK numbering is NOT one
sequence across divisions**, measured on the live corpus:

    ewhc/admin/2025/177  Guzdek v Circuit Court in Kalisz
    ewhc/ch/2025/177     Cheshire East Borough Council v M & Ors      -- different cases
    ewca/civ/1964/1      Pinion, Re
    ewca/crim/1964/1     R v Chandler                                 -- 1,551 such pairs

The alias key is unique, so the second import silently overwrites the first: the
chamber-less form then names whichever judgment was loaded last, and every bare citation
in the corpus follows it. That is how a commercial arbitration reference ("Trust Risk
Group SpA v AmTrust") came to point at a criminal appeal.

Three operations, in the order they should be run:

1. :func:`audit_chamber_aliases` — how many chamber-less aliases are ambiguous, and
   which edges currently follow them.
2. :func:`repair_chamber_aliases` — drop the ambiguous aliases and DEMOTE the edges that
   resolved through them back to pending. A hanging reference is recoverable; a
   confident link to the wrong case is not.
3. :func:`tiebreak_ambiguous_divisions` — settle what the citing text can settle. The
   name beside the citation is matched against a CLOSED candidate set (the two or three
   judgments holding that number), which is a discriminator and not a corpus-wide name
   search — the distinction matters, because open name matching on "R (…) v SSHD" style
   parties collides constantly.

and one that is about duplicates rather than ambiguity:

4. :func:`unify_synonym_slugs` — ``ewhc/pat`` and ``ewhc/patents`` are one court under
   two slugs, so those pairs are not two judgments but one held twice (312 of them).
   Keep the content, unify the node.
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger("raglex.ops.uk_identity")

#: Slugs that name the SAME court, so a pair of documents sharing court/year/number
#: under them is one judgment held twice — never an ambiguity to be resolved. ``qb``
#: and ``kb`` are the Queen's/King's Bench Division either side of the September 2022
#: renaming: a judgment sits in exactly one of them, and a citation naming the other is
#: simply using the name of the day.
SYNONYM_SLUGS: dict[str, str] = {
    "pat": "patents", "patents": "patents",
    "com": "comm", "comm": "comm",
    "acc": "aac", "aac": "aac",            # (ACC) is a misspelling that reaches citations
    "scco": "costs", "costs": "costs",
    "adm": "admlty", "admlty": "admlty",
    "qb": "kb", "kb": "kb",
}

_UK_SLUG = re.compile(r"^(?P<court>[a-z]+)/(?P<div>[a-z]+)/(?P<year>\d{4})/(?P<num>\d+)$")
_BARE_SLUG = re.compile(r"^(?P<court>[a-z]+)/(?P<year>\d{4})/(?P<num>\d+)$")


def _key(stable_id: str) -> tuple[str, str, str] | None:
    """(court, year, number-without-leading-zeros) — the coordinates a chamber-less
    citation actually names."""
    m = _UK_SLUG.match(stable_id or "")
    if not m:
        return None
    return (m.group("court"), m.group("year"), m.group("num").lstrip("0") or "0")


def _same_court(a: str, b: str) -> bool:
    """Whether two division slugs name the same court (see SYNONYM_SLUGS)."""
    return SYNONYM_SLUGS.get(a, a) == SYNONYM_SLUGS.get(b, b)


def _held_by_key(cat) -> dict[tuple[str, str, str], list[tuple[str, str]]]:
    """{(court, year, num): [(stable_id, title), …]} over held UK judgments."""
    out: dict[tuple[str, str, str], list[tuple[str, str]]] = {}
    for row in cat.conn.execute(
            "SELECT stable_id, COALESCE(title, '') AS title FROM documents "
            "WHERE source IN ('uk-caselaw', 'ie-caselaw')"):
        k = _key(row["stable_id"])          # also the shape filter
        if k:
            out.setdefault(k, []).append((row["stable_id"], row["title"]))
    return out


def _ambiguous(held: dict) -> dict:
    """The keys where two GENUINELY DIFFERENT courts hold the same number. Pairs that
    are only the same court under two slugs are duplicates, not ambiguities, and belong
    to :func:`unify_synonym_slugs`."""
    out = {}
    for k, docs in held.items():
        divs = {_UK_SLUG.match(sid).group("div") for sid, _t in docs}
        if len(divs) < 2:
            continue
        canon = {SYNONYM_SLUGS.get(d, d) for d in divs}
        if len(canon) > 1:
            out[k] = docs
    return out


def audit_chamber_aliases(cat, *, sample: int = 10) -> dict:
    """How many chamber-less aliases name more than one judgment, and what follows them."""
    held = _held_by_key(cat)
    amb = _ambiguous(held)
    keys = ["/".join(k) for k in amb]
    following, samples = 0, []
    for i in range(0, len(keys), 500):
        chunk = keys[i:i + 500]
        qs = ",".join("?" * len(chunk))
        following += cat.conn.execute(
            f"SELECT COUNT(*) AS n FROM relations WHERE candidate_id IN ({qs}) "
            "AND resolution_status = 'resolved'", chunk).fetchone()["n"]
    for k, docs in list(amb.items())[:sample]:
        samples.append({"cited_as": "/".join(k),
                        "held": [{"id": sid, "title": t[:60]} for sid, t in docs]})
    return {"held_numbers": len(held), "ambiguous_numbers": len(amb),
            "edges_following_an_ambiguous_alias": following, "samples": samples}


def repair_chamber_aliases(cat, *, dry_run: bool = True) -> dict:
    """Drop every chamber-less alias that names more than one judgment, and demote the
    edges that resolved through it.

    Not "pick a better one" — there is nothing in an alias key to pick WITH. The alias
    is deleted because it is false, and the edges go back to pending so that
    :func:`tiebreak_ambiguous_divisions`, or a human, can settle them on evidence."""
    held = _held_by_key(cat)
    amb = _ambiguous(held)
    keys = ["/".join(k) for k in amb]
    aliases = demoted = 0
    if not keys:
        return {"ambiguous_numbers": 0, "aliases_deleted": 0, "edges_demoted": 0,
                "dry_run": dry_run}
    for i in range(0, len(keys), 500):
        chunk = keys[i:i + 500]
        qs = ",".join("?" * len(chunk))
        aliases += cat.conn.execute(
            f"SELECT COUNT(*) AS n FROM citation_aliases WHERE alias IN ({qs})",
            chunk).fetchone()["n"]
        demoted += cat.conn.execute(
            f"SELECT COUNT(*) AS n FROM relations WHERE candidate_id IN ({qs}) "
            "AND resolution_status = 'resolved'", chunk).fetchone()["n"]
        if dry_run:
            continue
        with cat._atomic():
            cat.conn.execute(
                f"DELETE FROM citation_aliases WHERE alias IN ({qs})", chunk)
            cat.conn.execute(
                f"UPDATE relations SET resolution_status = 'pending', dst_id = NULL "
                f"WHERE candidate_id IN ({qs}) AND resolution_status = 'resolved'", chunk)
    return {"ambiguous_numbers": len(amb),
            "aliases_deleted": 0 if dry_run else aliases,
            "edges_demoted": 0 if dry_run else demoted,
            "aliases_matched": aliases, "edges_matched": demoted, "dry_run": dry_run}


# --- the tiebreak ------------------------------------------------------------------
# Evidence, strongest first. Each rung either eliminates candidates or scores them; a
# candidate wins only OUTRIGHT (strictly better than the runner-up). Where nothing
# discriminates the edge is left pending, which is the same choice the shorthand store
# makes for an ambiguous name: never guess between two authorities.
_NAME_BEFORE = re.compile(
    r"([A-Z][A-Za-z'’()\-.& ]{1,60}?\s+v\.?\s+[A-Z][A-Za-z'’()\-.& ]{1,60}?)\s*[,\[(]\s*$")
_CRIMINAL = re.compile(r"\bR\.?\s+v\.?\s|\bRegina\s+v|\bReg\.?\s+v|\bthe\s+Crown\b", re.I)
_PUBLIC_LAW = re.compile(r"\(on the application of\)|\bex\s*p(?:arte)?\b|\bR\s*\(", re.I)
#: words too common in party names to discriminate anything
_STOP = {"the", "and", "ors", "anor", "another", "others", "ltd", "limited", "plc",
         "inc", "llp", "co", "company", "secretary", "state", "home", "department",
         "commissioners", "revenue", "customs", "council", "borough", "chief",
         "constable", "attorney", "general", "director", "public", "prosecutions",
         "regina", "application", "behalf", "trust", "board", "authority", "service",
         "services", "commission", "minister", "queen", "king", "crown"}


def _words(text: str) -> set[str]:
    return {w.lower() for w in re.split(r"\W+", text or "")
            if len(w) > 3 and w.lower() not in _STOP}


def _score(name: str, before: str, cand_id: str, title: str) -> int:
    """How well one candidate explains the citing text.

    Three rungs, weighted by how much they prove. An explicit "A v B" beside the citation
    is the strongest; failing that, a distinctive word from the run-up appearing in one
    candidate's title and not the other's still discriminates ("Pinion, Re" and "In the
    Vegetarian Society case" are both named without a "v"); the court hints are a nudge,
    never enough on their own."""
    div = _UK_SLUG.match(cand_id).group("div")
    title_words = _words(title)
    score = 3 * len(_words(name) & title_words)
    if not name:
        score += len(_words(before[-90:]) & title_words)
    if _CRIMINAL.search(before[-110:]):
        score += 2 if div == "crim" else -2
    if _PUBLIC_LAW.search(before[-110:]):
        score += 2 if div in ("admin", "civ") else -1
    return score


def tiebreak_ambiguous_divisions(cat, textstore, *, dry_run: bool = True,
                                 limit: int = 5000) -> dict:
    """Settle chamber-less references against the citing text, and repoint them.

    Only the edges whose candidate names several held judgments are touched. A candidate
    must beat the runner-up outright; a tie, or no usable evidence, leaves the edge
    exactly as it was."""
    held = _held_by_key(cat)
    amb = _ambiguous(held)
    if not amb:
        return {"considered": 0, "settled": 0, "repointed": 0, "dry_run": dry_run}
    keys = ["/".join(k) for k in amb]
    rows: list = []
    for i in range(0, len(keys), 500):
        chunk = keys[i:i + 500]
        qs = ",".join("?" * len(chunk))
        rows.extend(cat.conn.execute(
            f"SELECT relation_id, src_id, candidate_id, dst_id, context_start "
            f"FROM relations WHERE candidate_id IN ({qs}) "
            f"AND context_start IS NOT NULL LIMIT ?", [*chunk, limit]).fetchall())
    texts: dict[str, str | None] = {}
    settled = repointed = no_evidence = tied = 0
    samples: list[dict] = []
    for r in rows:
        sid = r["src_id"]
        if sid not in texts:
            doc = cat.get_document(sid)
            try:
                texts[sid] = (textstore.get(doc["payload_hash"])
                              if doc and doc["payload_hash"] else None)
            except OSError:
                texts[sid] = None
        text = texts[sid]
        if not text:
            no_evidence += 1
            continue
        start = int(r["context_start"])
        before = " ".join(text[max(0, start - 170):start].split())
        m = _NAME_BEFORE.search(before)
        name = m.group(1) if m else ""
        cands = amb[tuple(r["candidate_id"].split("/"))]
        scored = sorted(((_score(name, before, sid2, title), sid2, title)
                         for sid2, title in cands), reverse=True)
        if scored[0][0] <= 0:
            no_evidence += 1
            continue
        if len(scored) > 1 and scored[0][0] == scored[1][0]:
            tied += 1                       # two candidates explain it equally: leave it
            continue
        settled += 1
        winner = scored[0][1]
        if winner == r["dst_id"]:
            continue
        repointed += 1
        if len(samples) < 12:
            samples.append({"cited_as": r["candidate_id"], "in": sid,
                            "was": r["dst_id"], "now": winner,
                            "because": (name or before[-70:])[:70]})
        if not dry_run:
            cat.conn.execute(
                "UPDATE relations SET dst_id = ?, resolution_status = 'resolved' "
                "WHERE relation_id = ?", (winner, r["relation_id"]))
    if not dry_run:
        cat.conn.commit()
    return {"considered": len(rows), "settled": settled, "repointed": repointed,
            "no_evidence": no_evidence, "tied": tied, "samples": samples,
            "dry_run": dry_run}


# --- duplicates, not ambiguities ---------------------------------------------------
#: The Queen's Bench Division became the King's Bench on 8 September 2022. A judgment
#: belongs to whichever court was sitting when it was given, so the year decides which
#: slug is the real one — not a fixed preference, and not the alphabet.
_KB_FROM_YEAR = 2023
_QB_TO_YEAR = 2021


def _preferred_div(divs: set[str], canon_div: str, year: str) -> str:
    """Which of two synonymous slugs the corpus should keep."""
    if divs <= {"qb", "kb"}:
        y = int(year) if year.isdigit() else 0
        if y <= _QB_TO_YEAR:
            return "qb"
        if y >= _KB_FROM_YEAR:
            return "kb"
        return ""          # 2022 straddles the rename: let text/edges decide
    return canon_div


def unify_synonym_slugs(cat, *, dry_run: bool = True) -> dict:
    """Fold ``ewhc/pat/…`` into ``ewhc/patents/…`` and friends: one court, two slugs, so
    the pair is one judgment held twice.

    The content is kept — the surviving row keeps its text, and the folded id is recorded
    as a RENDITION on it, so every way the judgment has been named still reaches it.
    What goes is the second NODE, because two rows for one judgment split its citations,
    its authority score and its search results.

    The survivor is the row with text, then the one with more edges, then the
    conventional slug. QB/KB is included only where exactly one of the pair is held —
    where both are, they are treated as different judgments and left alone."""
    held = _held_by_key(cat)
    folded = edges_moved = aliases = 0
    samples: list[dict] = []
    for k, docs in held.items():
        if len(docs) < 2:
            continue
        groups: dict[str, list[tuple[str, str]]] = {}
        for sid, title in docs:
            div = _UK_SLUG.match(sid).group("div")
            groups.setdefault(SYNONYM_SLUGS.get(div, div), []).append((sid, title))
        for canon_div, members in groups.items():
            if len(members) < 2:
                continue
            # QB/KB: only a real duplicate when the titles agree; two different
            # judgments can legitimately hold the same number either side of the rename.
            divs = {_UK_SLUG.match(sid).group("div") for sid, _t in members}
            if divs <= {"qb", "kb"}:
                titles = {re.sub(r"\W+", " ", t).strip().lower()[:26] for _s, t in members}
                if len(titles) > 1:
                    continue
            prefer = _preferred_div(divs, canon_div, k[1])
            ranked = sorted(
                members,
                key=lambda m: (
                    0 if (cat.get_document(m[0]) or {})["has_text"] else 1,
                    # the slug the source will mint again on the next import: keeping the
                    # other one only means folding the same pair a second time
                    0 if _UK_SLUG.match(m[0]).group("div") == prefer else 1,
                    -cat.conn.execute(
                        "SELECT COUNT(*) AS n FROM relations WHERE dst_id = ?",
                        (m[0],)).fetchone()["n"],
                ))
            keep, drop = ranked[0][0], [m[0] for m in ranked[1:]]
            for dead in drop:
                folded += 1
                if len(samples) < 12:
                    samples.append({"kept": keep, "folded": dead})
                if dry_run:
                    continue
                with cat._atomic():
                    edges_moved += cat.conn.execute(
                        "UPDATE relations SET dst_id = ? WHERE dst_id = ?",
                        (keep, dead)).rowcount
                    cat.conn.execute(
                        "UPDATE relations SET candidate_id = ? WHERE candidate_id = ?",
                        (keep, dead))
                    cat.conn.execute(
                        "UPDATE citation_aliases SET dst_id = ? WHERE dst_id = ?",
                        (keep, dead))
                    cat.put_alias(dead.casefold(), keep, source="uk-slug-fold",
                                  commit=False)
                    aliases += 1
                    cat.record_rendition(keep, "uk-caselaw", dead, commit=False)
                    cat.conn.execute("DELETE FROM citations WHERE src_id = ?", (dead,))
                    cat.conn.execute("DELETE FROM relations WHERE src_id = ?", (dead,))
                    cat.conn.execute("DELETE FROM doc_fts WHERE doc_id = ?", (dead,))
                    cat.conn.execute("DELETE FROM documents WHERE stable_id = ?", (dead,))
    return {"duplicate_nodes": folded, "edges_moved": edges_moved,
            "aliases_written": aliases, "samples": samples, "dry_run": dry_run}
