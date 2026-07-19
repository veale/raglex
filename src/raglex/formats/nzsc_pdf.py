"""Layout-aware parser for New Zealand Supreme Court judgment PDFs.

The Courts of NZ publish every Supreme Court judgment as a born-digital PDF with a
consistent house style, which lets us recover the structure a flat text dump loses:

  * the **neutral citation** — "[2026] NZSC 88" — printed on the first page, which is
    the case's identity (``nzsc/2026/88``) and the target every "[2026] NZSC 88"
    citation elsewhere in the corpus resolves to. Pulled from the PDF text itself, not
    inferred from the filename (older files aren't named by citation).
  * **numbered paragraphs** — "[1]", "[2]", … — the citable unit, emitted as segments
    so a reader can deep-link a paragraph and pinpoint citations ("at [42]") anchor.
  * **footnotes** — set in a smaller font at the foot of each page. PyMuPDF interleaves
    them into the reading flow where they break up paragraph anchoring; we lift them
    out into their own labelled zone at the end, *preserved* (not dropped) so the
    authorities cited in them still resolve, while the body paragraphs stay clean.

The rich (font-size aware) split needs PyMuPDF (``fitz``, the optional ``raglex[import]``
extra). Without it we fall back to a flat text extraction and still recover the citation
and paragraph anchors — only the footnote separation is skipped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from ..core.models import Segment

# "[2026] NZSC 88", tolerant of the odd double space / non-breaking space PDFs emit.
_NEUTRAL_RE = re.compile(r"\[\s*(?P<year>(?:19|20)\d{2})\s*\]\s*NZSC\s*(?P<num>\d+)", re.IGNORECASE)
# A numbered judgment paragraph at the start of a line: "[1]", "[42]".
_PARA_NUM_RE = re.compile(r"^\[(\d+)\]")
# A footnote line opens with its number ("1 R v Smith…"); a line that doesn't is a wrap.
_FN_START_RE = re.compile(r"^\d+\s+\S")
# Where a merged run of footnotes can be split: a new footnote number after a sentence-
# ending period, followed by a capital or opening quote/paren. Deliberately anchored to a
# PERIOD only (not "]"/")") so a citation's internal "] 1 NZLR" — the volume after a year
# bracket — is never mistaken for a footnote boundary; and "see p. 2 above" won't split
# (a lowercase word follows). Precision over recall: merging two footnotes is far less
# harmful than slicing a citation in half.
_FN_MID_SPLIT_RE = re.compile(r"(?<=\.)\s+(?=\d+\s+[A-Z\"'(])")
# Page-footer / running-header boilerplate that shares the footnotes' small font at the
# foot of the page but is NOT a footnote (repeats every page): the ALL-CAPS running case
# name, the "Solicitors:/Counsel:/Coram:" lines, a bare page number.
_FOOTER_KW_RE = re.compile(r"^(?:solicitors|counsel|coram|judgment|before|between|and)\b", re.IGNORECASE)

FOOTNOTES_HEADING = "Footnotes"

# -- first-page intituling (front matter) metadata -------------------------
# The court file / registry number: "SC CRI 2/2004", "SC 36/2018", "SC UR 6/2026". Before
# neutral citation (NZSC judgments delivered in 2004), this + party names + date IS the
# citation ("R v Palmer SC CRI 13/2004, 12 October 2004"), so it's the key later matching
# turns on. NB: today's NZSC only exists from 2004; a pre-1980 bare "SC" is the *old*
# Supreme Court (now the High Court), never this court — but every file number here carries
# a /2004+ year, so there is no collision.
_FILE_NUMBER_RE = re.compile(r"\bSC\s+(?:[A-Z]{2,4}\s+)?\d+/(?:19|20)\d{2}\b")
# labelled intituling fields, each running to the next label
_HDR_LABEL_RE = re.compile(
    r"\b(Coram|Court|Judges?|Counsel|Hearing|Judgment|Before|Appearances)\b\s*:", re.IGNORECASE)
_COURT_NAME_RE = re.compile(r"IN THE SUPREME COURT OF NEW ZEALAND|I TE K[ŌO]TI MANA NUI", re.IGNORECASE)
# a bench member: "Elias CJ", "William Young J", "Ellen France JJ". Name words are
# Titlecase (a capital then a lowercase run), which the judicial suffixes (CJ/J/JJ) are
# not — so the name part can't greedily swallow the next judge's suffix.
_JUDGE_RE = re.compile(r"(?:[A-ZĀ][a-zāēīōū'’-]+\s+){1,3}(?:CJ|JJ|J)\b")
# party role words / ordinals — stripped from anywhere in the party line
_ROLE_RE = re.compile(
    r"\b(?:First|Second|Third|Fourth|Fifth|Sixth|Seventh|Eighth)?\s*"
    r"(?:Applicants?|Appellants?|Respondents?|Interveners?|Intervenors?|Plaintiffs?|"
    r"Defendants?|Cross-?[Aa]ppellants?|Crown|Prosecutor)\b", re.IGNORECASE)
# a running-header / hearing date that leaks into the front matter ("[14 December 2018]")
_LOOSE_DATE_RE = re.compile(r"\[?\s*\d{1,2}\s+[A-Za-z]+\s+(?:19|20)\d{2}\s*\]?")
_DATE_RE = re.compile(r"\b(\d{1,2})\s+([A-Za-z]+)\s+((?:19|20)\d{2})\b")
_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"], start=1)}


@dataclass(slots=True)
class ParsedJudgment:
    text: str = ""
    segments: list[Segment] = field(default_factory=list)
    neutral_citation: str | None = None   # canonical id, e.g. "nzsc/2026/88" (2005+ only)
    footnotes: list[str] = field(default_factory=list)
    # intituling metadata — the identity/matching signal, richest for pre-2005 cases that
    # have no neutral citation at all.
    file_number: str | None = None        # "SC CRI 2/2004" — the unreported-citation key
    parties: str | None = None            # "Alan Ivo Greer v The Queen"
    coram: list[str] = field(default_factory=list)   # ["Elias CJ", "Blanchard J"]
    counsel: str | None = None
    judgment_date: "date | None" = None   # from the "Judgment:" line — the actual delivery date


def neutral_citation_id(text: str) -> str | None:
    """First "[YYYY] NZSC N" in ``text`` → the canonical id ``nzsc/YYYY/N`` (the same
    slug the neutral-citation grammar mints, so citations resolve to the stored doc).

    Intended for the first-page **intituling** only — scanning a whole judgment risks
    picking up a *cited* case's citation as this document's identity."""
    m = _NEUTRAL_RE.search(text or "")
    if not m:
        return None
    return f"nzsc/{m.group('year')}/{int(m.group('num'))}"


def file_number_id(file_number: str | None) -> str | None:
    """Court file number → a stable internal id for a case with no neutral citation:
    "SC CRI 2/2004" → ``nzsc/sc-cri-2-2004``. Stable across the feed's duplicate case-page
    URLs (they share the file number), and namespaced so it can't collide with a neutral
    citation id (``nzsc/2005/1``)."""
    if not file_number:
        return None
    slug = re.sub(r"[^a-z0-9]+", "-", file_number.lower()).strip("-")
    return f"nzsc/{slug}" if slug else None


def parse_header(front: str) -> dict:
    """Pull the intituling metadata off a judgment's first page: file number, parties,
    coram (bench), counsel, and the delivery date. Robust to the label-on-its-own-line
    layout PyMuPDF emits ("Coram:\\nElias CJ\\nBlanchard J\\nCounsel:\\n…")."""
    front = front or ""
    meta: dict = {}

    fm = _FILE_NUMBER_RE.search(front)
    if fm:
        meta["file_number"] = re.sub(r"\s+", " ", fm.group(0)).strip()

    # labelled fields: slice each label's value up to the next label
    labels = list(_HDR_LABEL_RE.finditer(front))
    fields: dict[str, str] = {}
    for i, lm in enumerate(labels):
        name = lm.group(1).lower()
        end = labels[i + 1].start() if i + 1 < len(labels) else len(front)
        fields[name] = re.sub(r"\s+", " ", front[lm.end():end]).strip()

    coram_raw = fields.get("coram") or fields.get("court") or fields.get("judges") or fields.get("before")
    if coram_raw:
        judges = _parse_coram(coram_raw)
        if judges:
            meta["coram"] = judges
    if fields.get("counsel"):
        meta["counsel"] = fields["counsel"]
    if fields.get("judgment"):
        dm = _DATE_RE.search(fields["judgment"])
        if dm:
            meta["judgment_date"] = _to_date(dm)

    parties = _extract_parties(front, labels[0].start() if labels else len(front),
                               meta.get("file_number"))
    if parties:
        meta["parties"] = parties
    return meta


def _parse_coram(raw: str) -> list[str]:
    """Bench members from the "Coram:/Court:" line, both NZSC house forms:
      * comma form with a shared trailing suffix — "Elias CJ, William Young, Glazebrook,
        O’Regan and Ellen France JJ" → each bare puisne name inherits the "J";
      * space form, each judge already suffixed — "Elias CJ Blanchard J"."""
    raw = re.sub(r"\s+", " ", raw or "").strip()
    if not raw:
        return []
    if "," in raw or re.search(r"\sand\s", raw):
        parts = re.split(r"\s*,\s*|\s+and\s+|\s+&\s+", raw)
        out: list[str] = []
        for p in parts:
            p = p.strip(" .")
            if not p or not p[0].isupper() or not any(c.islower() for c in p):
                continue
            sm = re.search(r"\b(CJ|JJ|J)\s*$", p)
            if sm:
                name = re.sub(r"\s*\b(?:CJ|JJ|J)\s*$", "", p).strip()
                out.append(f"{name} {'CJ' if sm.group(1) == 'CJ' else 'J'}")
            else:
                out.append(f"{p} J")            # a bare puisne name → the shared "J"
        return [j for j in out if len(j.split()) <= 4]
    return [re.sub(r"\s+", " ", j).strip() for j in _JUDGE_RE.findall(raw)]


def _extract_parties(front: str, cut: int, file_number: str | None) -> str | None:
    """The party line from the front matter — the lines above the first labelled field,
    minus the court-name header, the running header, and the file-number/citation line."""
    head = front[:cut]
    keep: list[str] = []
    for line in head.split("\n"):
        s = line.strip()
        if not s or _COURT_NAME_RE.search(s):
            continue
        if _FILE_NUMBER_RE.search(s) or _NEUTRAL_RE.search(s):
            continue        # the "SC …/YYYY [YYYY] NZSC N" line, or the running header
        keep.append(s)
    party = re.sub(r"\s+", " ", " ".join(keep)).strip()
    party = _LOOSE_DATE_RE.sub(" ", party)               # drop a leaked hearing/running date
    party = _ROLE_RE.sub(" ", party)                     # drop Applicant/Respondent/ordinals
    party = re.sub(r"\bBETWEEN\b", " ", party, flags=re.IGNORECASE)  # intituling connector
    party = re.sub(r"\s+(?:v|V)\s+", " v ", party)       # normalise the "v" connector
    party = re.sub(r"\s{2,}", " ", party).strip(" -–—")
    return party or None


def _to_date(m: "re.Match[str]") -> "date | None":
    mon = _MONTHS.get(m.group(2).lower())
    if not mon:
        return None
    try:
        return date(int(m.group(3)), mon, int(m.group(1)))
    except ValueError:
        return None


def _paragraph_segments(text: str) -> list[Segment]:
    """Segments for each "[N]" numbered paragraph in ``text`` (char offsets into it).
    A paragraph runs from its "[N]" line to just before the next "[N]" line."""
    starts: list[tuple[int, int]] = []  # (char offset of line start, paragraph number)
    cursor = 0
    for line in text.split("\n"):
        stripped = line.strip()
        m = _PARA_NUM_RE.match(stripped)
        # "[2026] NZSC 88" also starts with "[NNNN]" — it's the neutral citation, not a
        # paragraph. Exclude a bracketed year that heads the citation line.
        if m and not _NEUTRAL_RE.match(stripped):
            starts.append((cursor, int(m.group(1))))
        cursor += len(line) + 1  # +1 for the \n
    segments: list[Segment] = []
    for i, (start, num) in enumerate(starts):
        end = starts[i + 1][0] - 1 if i + 1 < len(starts) else len(text)
        segments.append(Segment(label=f"[{num}]", char_start=start, char_end=max(start, end),
                                kind="paragraph", level=0))
    return segments


def _assemble(body: str, footnotes: list[str], first_page: str) -> ParsedJudgment:
    """Join body + a preserved footnote zone, emit paragraph + footnote segments with
    offsets into the combined text, and pull the intituling metadata off the first page.

    The neutral citation is taken from the **first page only** — a whole-body scan could
    latch onto a *cited* case's "[YYYY] NZSC N" and mis-key this document."""
    body = body.strip()
    segments = _paragraph_segments(body)
    text = body
    if footnotes:
        # a labelled zone, appended so footnote citations stay in `text` (and resolve)
        # while the body paragraphs above are uninterrupted.
        text = body + "\n\n" + FOOTNOTES_HEADING + "\n\n"
        for i, fn in enumerate(footnotes, start=1):
            fn = fn.strip()
            if not fn:
                continue
            start = len(text)
            text += fn + "\n\n"
            segments.append(Segment(label=f"fn {i}", char_start=start, char_end=start + len(fn),
                                    kind="footnote", level=0))
        text = text.rstrip()
    head = first_page or body[:2000]
    hdr = parse_header(head)
    return ParsedJudgment(
        text=text, segments=segments,
        neutral_citation=neutral_citation_id(head),   # 2005+ only; None for 2004 cases
        footnotes=footnotes,
        file_number=hdr.get("file_number"),
        parties=hdr.get("parties"),
        coram=hdr.get("coram", []),
        counsel=hdr.get("counsel"),
        judgment_date=hdr.get("judgment_date"),
    )


def _split_body_and_footnotes(data: bytes) -> tuple[str, list[str], str]:
    """Walk the PDF with PyMuPDF, separating body text from the smaller-font footnote
    zone at the foot of each page. Returns (body_text, [footnote, …], first_page_text).

    Heuristic (robust to the NZSC house style, degrades gracefully): on each page the
    dominant text size is the body; a *trailing* run of blocks in the lower half of the
    page set noticeably smaller than the body is the footnote apparatus. Everything else
    is body, in reading order."""
    import fitz

    body_parts: list[str] = []
    footnote_parts: list[str] = []
    first_page = ""
    with fitz.open(stream=data, filetype="pdf") as doc:
        for pno, page in enumerate(doc):
            if pno == 0:
                first_page = page.get_text()     # raw first page — the intituling, labels intact
            blocks = _page_blocks(page)          # [(y0, size, text)], reading order
            if not blocks:
                continue
            body_size = _dominant_size(blocks)
            page_h = page.rect.height or 1.0
            # find the longest trailing run of smaller-font blocks sitting in the lower
            # half of the page — the footnote apparatus.
            cut = len(blocks)
            for i in range(len(blocks) - 1, -1, -1):
                y0, size, _txt = blocks[i]
                if size <= body_size - 1.0 and y0 > page_h * 0.5:
                    cut = i
                else:
                    break
            for y0, _size, txt in blocks[:cut]:
                body_parts.append(txt)
            for _y0, _size, txt in blocks[cut:]:
                if txt.strip():
                    footnote_parts.append(txt.strip())

    body = "\n\n".join(p for p in body_parts if p.strip())
    footnotes = _split_footnotes(footnote_parts)
    return body, footnotes, first_page


def _page_blocks(page) -> list[tuple[float, float, str]]:
    """(top-y, representative font size, joined text) per text block, in reading order.
    Inside a block hard newlines are line-wrapping → collapsed to spaces (so a numbered
    paragraph stays one line and its "[N]" anchor is at the block start)."""
    out: list[tuple[float, float, str]] = []
    d = page.get_text("dict")
    for b in d.get("blocks", []):
        if b.get("type", 0) != 0:  # skip image blocks
            continue
        sizes: list[tuple[float, int]] = []
        lines: list[str] = []
        for ln in b.get("lines", []):
            spans = ln.get("spans", [])
            lines.append("".join(s.get("text", "") for s in spans))
            for s in spans:
                t = (s.get("text") or "").strip()
                if t:
                    sizes.append((round(float(s.get("size", 0)), 1), len(t)))
        text = re.sub(r"\s*\n\s*", " ", "\n".join(lines)).strip()
        if not text:
            continue
        size = _weighted_mode(sizes)
        out.append((round(float(b["bbox"][1]), 1), size, text))
    out.sort(key=lambda t: t[0])  # top-down
    return out


def _weighted_mode(sizes: list[tuple[float, int]]) -> float:
    """Most common font size, weighted by how many characters are set at it."""
    if not sizes:
        return 0.0
    weight: dict[float, int] = {}
    for size, n in sizes:
        weight[size] = weight.get(size, 0) + n
    return max(weight.items(), key=lambda kv: kv[1])[0]


def _dominant_size(blocks: list[tuple[float, float, str]]) -> float:
    """Body font size = the size carrying the most text across the page's blocks."""
    weight: dict[float, int] = {}
    for _y0, size, txt in blocks:
        weight[size] = weight.get(size, 0) + len(txt)
    return max(weight.items(), key=lambda kv: kv[1])[0] if weight else 0.0


def _footer_noise(text: str) -> bool:
    """True if this small-font, foot-of-page line is running-header / footer boilerplate
    (the ALL-CAPS case name, a "Solicitors:/Counsel:" line, a bare page number) rather
    than a footnote — so it's neither a new footnote nor a continuation of one."""
    body = re.sub(r"^\d+\s+", "", text).strip()   # drop a leading (page/footnote) number
    if not body:
        return True                                # a bare page number
    if _FOOTER_KW_RE.match(body):
        return True
    letters = [c for c in body if c.isalpha()]
    if letters and sum(c.isupper() for c in letters) / len(letters) > 0.7:
        return True                                # ALL-CAPS running case-name header
    return False


def _split_footnotes(lines: list[str]) -> list[str]:
    """Group footnote text into individual footnotes. Only a line that opens with a
    footnote number ("1 R v Smith…") starts a footnote; a numberless line continues the
    one above (a wrapped footnote). Page-footer / running-header boilerplate that shares
    the small font is rejected — it never starts a footnote and never attaches to one, so
    "[case name] … Solicitors: …" running footers don't pollute the footnote zone.
    A merged run of several footnotes on one line is split on the sentence-boundary
    numbering."""
    notes: list[str] = []
    for raw in lines:
        line = re.sub(r"[ \t]+", " ", raw).strip()
        if not line:
            continue
        for piece in _FN_MID_SPLIT_RE.split(line):
            piece = piece.strip()
            if not piece:
                continue
            if _FN_START_RE.match(piece) and not _footer_noise(piece):
                notes.append(piece)
            elif notes and not _footer_noise(piece):
                notes[-1] = f"{notes[-1]} {piece}"
            # else: leading footer noise before any footnote — drop it
    return notes


def parse_nzsc_pdf(data: bytes) -> ParsedJudgment:
    """Parse an NZSC judgment PDF → body text + paragraph/footnote segments + intituling
    metadata (neutral citation where it exists, file number, parties, coram, counsel,
    date). Uses PyMuPDF for the footnote-aware split; falls back to a flat text extraction
    (citation + paragraph anchors + header only) when PyMuPDF isn't available or the layout
    walk fails."""
    try:
        body, footnotes, first_page = _split_body_and_footnotes(data)
        if body.strip():
            return _assemble(body, footnotes, first_page)
    except Exception:  # noqa: BLE001 — never let a layout quirk lose the document
        pass

    # Fallback: the shared extractor (PyMuPDF blocks or pypdf), no footnote separation.
    from ..extraction import extract_bytes

    flat = extract_bytes(data, ext="pdf", mime="application/pdf").text or ""
    return _assemble(flat, [], flat[:2000])
