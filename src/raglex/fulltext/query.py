"""The free-text query language: what a reader types → a Postgres ``tsquery`` plus
the literal strings that have to be checked against the real text afterwards.

``websearch_to_tsquery`` is not enough on three counts, each measured against the
live database rather than assumed:

* **Grouping is silently discarded.** ``(negligence or nuisance) damages`` compiles
  to ``'neglig' | 'nuisanc' & 'damag'`` — and because ``&`` binds tighter than ``|``
  that means *negligence OR (nuisance AND damages)*, which is not what was typed.
  No error, no warning.
* **Wildcards are silently dropped.** ``neglig*`` compiles to plain ``'neglig'``.
* **No proximity.** ``tsquery`` itself has ``<N>``, but websearch won't emit it.
  For a legal tool that matters: anyone arriving from Westlaw or Lexis expects
  ``/s``, ``/p``, ``/n``, and ``<N>`` is the same idea.

And one correctness point that shapes the whole design. Postgres stems, so the text
"duties of care" matches the query ``"duty of care"`` — both become
``'duti' <2> 'care'``. A *quoted* string therefore isn't literal, and this parser's
second job is to report which phrases were quoted so the caller can verify them
against the document text. Crucially, a NEGATED phrase must NOT be pushed into the
tsquery in exact mode: ``-"duty of care"`` would exclude a document containing only
"duties of care" — which does not contain the literal string asked to be excluded —
and verification can only filter candidates, never restore one that was never
retrieved. So negated literals come back separately too.

Grammar (loose, forgiving — this is a search box, not a compiler)::

    query    := or_expr
    or_expr  := and_expr (("OR" | "|") and_expr)*
    and_expr := unary (("AND" | "&")? unary)*
    unary    := ("NOT" | "-") unary | primary
    primary  := "(" query ")" | phrase | term
    phrase   := '"' words '"' ( "~" digits )?      -- ~N = words may be N apart
    term     := word ( "*" )? | word ("NEAR/" digits | "/" digits) term
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# One token of the query language. Order matters: the multi-character operators have
# to be tried before the bare words they start with.
_TOKEN = re.compile(
    r"""
      (?P<phrase>"[^"]*"|“[^”]*”)          # a quoted phrase
    | (?P<lparen>\()
    | (?P<rparen>\))
    | (?P<near>(?:NEAR\s*/\s*|~|/)\d{1,2})  # NEAR/3, ~3, /3
    | (?P<op>\bAND\b|\bOR\b|\bNOT\b|&&|\|\||&|\|)
    | (?P<neg>-(?=\S))                      # leading dash, not a hyphen inside a word
    # A word may CONTAIN a balanced parenthesis — "R(Miller)" and "section 3(2)" are
    # how these are written — but a lone ")" still closes a group, so
    # "(negligence or nuisance) damages" groups as typed.
    | (?P<word>[^\s()&|"“”-](?:[^\s()&|"“”]|\([^\s()]*\))*)
    """,
    re.VERBOSE | re.IGNORECASE,
)
# Characters Postgres would read as tsquery syntax if we let them through a lexeme.
_LEXEME_BAD = re.compile(r"['\\]")


class QueryError(ValueError):
    """The query can't be answered as written — carries a reader-facing message."""


# -- the parsed shape ---------------------------------------------------------
@dataclass(slots=True)
class Term:
    word: str
    prefix: bool = False


@dataclass(slots=True)
class Phrase:
    words: list[str]
    distance: int = 1          # 1 = adjacent; N = at most N apart (the ~N form)

    @property
    def text(self) -> str:
        return " ".join(self.words)


@dataclass(slots=True)
class Near:
    left: "Node"
    right: "Node"
    distance: int


@dataclass(slots=True)
class Not:
    child: "Node"


@dataclass(slots=True)
class And:
    children: list["Node"] = field(default_factory=list)


@dataclass(slots=True)
class Or:
    children: list["Node"] = field(default_factory=list)


Node = Term | Phrase | Near | Not | And | Or


@dataclass(slots=True)
class ParsedQuery:
    node: Node | None
    #: phrases the reader quoted, in positive position — these are what "literal"
    #: means, and the caller verifies them against the document's own text
    literals: list[Phrase] = field(default_factory=list)
    #: phrases quoted behind a NOT — excluded at verification time, never in the
    #: tsquery, because a stemmed negation over-excludes irrecoverably
    excluded: list[Phrase] = field(default_factory=list)
    #: things the reader should be told about their query
    notes: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return self.node is None


# -- lexing / parsing ---------------------------------------------------------
# A NUMBER/NUMBER (or decimal) run is ONE lexeme to Postgres — measured, not assumed:
#
#     to_tsvector('english', 'No 765/2008')             -> '765/2008':2
#     to_tsvector('english', 'Regulation (EU) 2016/679') -> '2016/679':3 'eu':2 'regul':1
#     to_tsvector('english', '1.5 million')             -> '1.5':1 'million':2
#
# Splitting it here into '765' and '2008' compiled the phrase to `'765' <-> '2008'`,
# which cannot match a tsvector holding the single lexeme '765/2008'. So every quoted
# phrase carrying an EU citation number — the commonest identifier in this corpus —
# silently returned nothing:
#
#     "the CE marking has been affixed in violation of Article 30 of
#      Regulation (EC) No 765/2008"                                  0 hits
#     …the same phrase cut before the number                        43 hits
#
# The two tokenisers have to agree, and Postgres's is the one that built the index.
_WORD_CHARS = re.compile(
    r"""
      \d+(?:[./]\d+)+                    # 765/2008, 2016/679, 1.5 — one lexeme
    | [^\W_]+(?:['’][^\W_]+)*
    """,
    re.UNICODE | re.VERBOSE,
)


def _words_of(text: str) -> list[str]:
    """The indexable words of a phrase. Punctuation is dropped here because Postgres
    drops it too ("section 3(2)" → 'section','3','2'); the *literal* check later is
    what puts the punctuation back.

    Except where Postgres KEEPS it (see ``_WORD_CHARS``): a slash or decimal point
    between digits binds them into one lexeme, and a phrase that disagrees about that
    matches nothing at all. Hyphenated numbers ("2020-12-31" → '2020' '-12' '-31')
    still disagree; they have not been seen to matter and the fix there is not a
    character class but the sign the parser keeps."""
    return _WORD_CHARS.findall(text)


class _Parser:
    def __init__(self, tokens: list[tuple[str, str]]) -> None:
        self.toks = tokens
        self.i = 0

    def peek(self) -> tuple[str, str] | None:
        return self.toks[self.i] if self.i < len(self.toks) else None

    def take(self) -> tuple[str, str]:
        tok = self.toks[self.i]
        self.i += 1
        return tok

    def parse(self) -> Node | None:
        node = self.or_expr()
        if self.peek() is not None:
            # a stray ")" — forgive it rather than refuse the search
            self.i = len(self.toks)
        return node

    def or_expr(self) -> Node | None:
        left = self.and_expr()
        parts = [left] if left is not None else []
        while (t := self.peek()) and t[0] == "op" and t[1].lower() in ("or", "|", "||"):
            self.take()
            right = self.and_expr()
            if right is not None:
                parts.append(right)
        if not parts:
            return None
        return parts[0] if len(parts) == 1 else Or(parts)

    def and_expr(self) -> Node | None:
        parts: list[Node] = []
        while (t := self.peek()) is not None:
            if t[0] == "rparen":
                break
            if t[0] == "op" and t[1].lower() in ("or", "|", "||"):
                break
            if t[0] == "op" and t[1].lower() in ("and", "&", "&&"):
                self.take()          # explicit AND is just a separator
                continue
            node = self.unary()
            if node is None:
                break
            # a NEAR operator binds the node just parsed to the one after it
            while (n := self.peek()) and n[0] == "near":
                dist = int(re.search(r"\d+", self.take()[1]).group())
                right = self.unary()
                if right is None:
                    break
                node = Near(node, right, dist)
            parts.append(node)
        if not parts:
            return None
        return parts[0] if len(parts) == 1 else And(parts)

    def unary(self) -> Node | None:
        t = self.peek()
        if t is None:
            return None
        if t[0] == "neg" or (t[0] == "op" and t[1].lower() == "not"):
            self.take()
            child = self.unary()
            return Not(child) if child is not None else None
        return self.primary()

    def primary(self) -> Node | None:
        t = self.peek()
        if t is None:
            return None
        kind, text = t
        if kind == "lparen":
            self.take()
            inner = self.or_expr()
            if (nt := self.peek()) and nt[0] == "rparen":
                self.take()
            return inner
        if kind == "rparen":
            return None
        if kind == "phrase":
            self.take()
            body = text[1:-1]
            dist = 1
            # "…"~3 — the words may be up to 3 apart
            if (nt := self.peek()) and nt[0] == "near":
                dist = int(re.search(r"\d+", self.take()[1]).group())
            words = _words_of(body)
            return Phrase(words, dist) if words else None
        if kind == "word":
            self.take()
            prefix = text.endswith("*")
            core = text[:-1] if prefix else text
            words = _words_of(core)
            if not words:
                return None
            # a "word" holding punctuation ("R(Miller)") is several lexemes to
            # Postgres; treat it as an adjacent phrase so it still means one thing
            if len(words) > 1:
                return Phrase(words, 1)
            return Term(words[0], prefix)
        self.take()
        return None


def _lex(text: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for m in _TOKEN.finditer(text or ""):
        kind = m.lastgroup or ""
        out.append((kind, m.group()))
    return out


# -- compiling to tsquery -----------------------------------------------------
def _lexeme(word: str) -> str:
    return "'" + _LEXEME_BAD.sub("", word) + "'"


def _compile(node: Node, *, exact: bool) -> str | None:
    """The tsquery text for a node, or None when it contributes nothing.

    In ``exact`` mode a negated phrase compiles to nothing — see the module docstring:
    stemmed negation over-excludes and verification cannot restore what was never
    retrieved, so exclusion is deferred to the literal check."""
    if isinstance(node, Term):
        return _lexeme(node.word) + (":*" if node.prefix else "")
    if isinstance(node, Phrase):
        if not node.words:
            return None
        op = f" <{node.distance}> " if node.distance > 1 else " <-> "
        return "(" + op.join(_lexeme(w) for w in node.words) + ")"
    if isinstance(node, Near):
        left = _compile(node.left, exact=exact)
        right = _compile(node.right, exact=exact)
        if not left or not right:
            return left or right
        return f"({left} <{node.distance}> {right})"
    if isinstance(node, Not):
        if exact and _quoted_phrases(node.child):
            return None                      # deferred to verification
        inner = _compile(node.child, exact=exact)
        return f"!({inner})" if inner else None
    if isinstance(node, (And, Or)):
        joiner = " & " if isinstance(node, And) else " | "
        parts = [p for p in (_compile(c, exact=exact) for c in node.children) if p]
        if not parts:
            return None
        # An OR that lost a branch would silently widen from "a or b" to "a"; that is
        # still the honest reading of what survived, but an AND that lost a branch is
        # fine because the survivors only narrow.
        return "(" + joiner.join(parts) + ")" if len(parts) > 1 else parts[0]
    return None


def _quoted_phrases(node: Node | None) -> list[Phrase]:
    if isinstance(node, Phrase):
        return [node]
    if isinstance(node, Not):
        return []
    if isinstance(node, Near):
        return _quoted_phrases(node.left) + _quoted_phrases(node.right)
    if isinstance(node, (And, Or)):
        out: list[Phrase] = []
        for c in node.children:
            out.extend(_quoted_phrases(c))
        return out
    return []


def _negated_phrases(node: Node | None) -> list[Phrase]:
    if isinstance(node, Not):
        return _quoted_phrases(node.child)
    if isinstance(node, Near):
        return _negated_phrases(node.left) + _negated_phrases(node.right)
    if isinstance(node, (And, Or)):
        out: list[Phrase] = []
        for c in node.children:
            out.extend(_negated_phrases(c))
        return out
    return []


# English stop words Postgres removes. A phrase made only of these compiles to an
# empty tsquery and would match NOTHING, silently — the reader has to be told, not
# shown zero results for "in and of itself".
_STOPWORDS = {
    "a", "about", "above", "after", "again", "against", "all", "am", "an", "and",
    "any", "are", "as", "at", "be", "because", "been", "before", "being", "below",
    "between", "both", "but", "by", "can", "did", "do", "does", "doing", "down",
    "during", "each", "few", "for", "from", "further", "had", "has", "have",
    "having", "he", "her", "here", "hers", "herself", "him", "himself", "his",
    "how", "i", "if", "in", "into", "is", "it", "its", "itself", "just", "me",
    "more", "most", "my", "myself", "no", "nor", "not", "now", "of", "off", "on",
    "once", "only", "or", "other", "our", "ours", "ourselves", "out", "over",
    "own", "same", "she", "should", "so", "some", "such", "than", "that", "the",
    "their", "theirs", "them", "themselves", "then", "there", "these", "they",
    "this", "those", "through", "to", "too", "under", "until", "up", "very",
    "was", "we", "were", "what", "when", "where", "which", "while", "who", "whom",
    "why", "will", "with", "you", "your", "yours", "yourself", "yourselves",
}


def parse(text: str, *, exact: bool = True) -> ParsedQuery:
    """Parse a reader's query. ``exact`` decides whether a quoted string means the
    literal characters (verified against the text) or Postgres's stemmed phrase
    match, which also finds "duties of care" for ``"duty of care"``."""
    node = _Parser(_lex(text)).parse()
    if node is None:
        return ParsedQuery(None)
    positives = _quoted_phrases(node)
    negatives = _negated_phrases(node)
    notes: list[str] = []
    for ph in positives + negatives:
        if ph.words and all(w.lower() in _STOPWORDS for w in ph.words):
            notes.append(
                f'“{ph.text}” is made only of words the index does not store '
                f"(the, of, in …), so it cannot be looked up directly.")
    return ParsedQuery(node=node, literals=positives if exact else [],
                       excluded=negatives if exact else [], notes=notes)


def to_tsquery(parsed: ParsedQuery, *, exact: bool = True) -> str | None:
    """The tsquery text for a parsed query, or None if nothing is left to look up
    (an all-stopword phrase, or a query that was only an exclusion)."""
    if parsed.node is None:
        return None
    return _compile(parsed.node, exact=exact) or None
