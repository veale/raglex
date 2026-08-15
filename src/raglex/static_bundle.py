"""A whole set of static editions, exported together as one small website.

:mod:`static_export` builds ONE law's self-contained page. A working set — the GDPR, the
DSA, the DMA, the OSA — is what a reading list actually needs, published at one level with
an ``index.html`` linking them by their own short filenames (``gdpr.html``), each carrying
the date it was exported.

Two outputs, from the same build:

* a **folder** on disk beside the catalogue and the raw store, overwritten in place (no
  versioning, no confirmation) — what a scheduled run keeps current, and what a static
  host or sync tool can point at;
* a **zip** for the browser, when a person pressed the button.

The set, the filenames, the shared preamble and each item's own line live in one settings
row, so the operator edits them in the UI rather than in a script.
"""

from __future__ import annotations

import io
import json
import os
import re
import unicodedata
import zipfile
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from .config import Config
from .facade import Facade
from .static_export import (
    _STYLE,
    _flag_assets,
    build_static_export_cache,
    cached_export_page_title,
    editorial_paragraphs,
    public_base_url,
    render_cached_export,
    static_export_status,
)


def _xml(value: str) -> str:
    """Escape for an XML text node — a sitemap is XML, and an ampersand in a filename
    would otherwise make the whole document unparseable and the sitemap ignored."""
    return (str(value or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))

# One JSON settings row holds the whole plan (see SettingsStore._SYSTEM_KEYS).
CONFIG_KEY = "RAGLEX_STATIC_BUNDLE"
LAST_RUN_KEY = "RAGLEX_STATIC_BUNDLE_LAST"

DEFAULT_INDEX_TITLE = "Statutes"
DEFAULT_INDEX_TEXT = (
    "Self-contained editions of these instruments, each with the documents that cite it. "
    "Last updated <dateexported>."
)
_SLUG_SAFE = re.compile(r"[^a-z0-9._-]+")
_UNPLACED = "Other instruments"
#: Where an instrument the operator has not put in any theme is listed. Named rather
#: than hidden: a set that silently dropped a law from its index because nobody had
#: tagged it would be worse than an honest last section, and tagging it makes this
#: heading disappear on its own.
_UNTHEMED = "Other instruments"

#: The EU appends this to the formal title of every instrument that extends to the EEA.
#: It is a legal note about territorial scope, not part of the name anyone cites the act
#: by, and on an index page of forty laws it is forty repetitions of the same eight
#: words. Dropped from the index only — inside an edition the instrument keeps the
#: official title exactly as published.
_EEA_RELEVANCE = re.compile(
    r"\s*\(\s*Text\s+with\s+EEA\s+relevance\s*\)\s*\.?\s*$", re.IGNORECASE)


def strip_eea_relevance(title: str) -> str:
    """An instrument's title without the EEA-relevance note it ends with."""
    return _EEA_RELEVANCE.sub("", str(title or "")).strip()


def theme_key(value: str) -> str:
    """Normalise a theme tag so what the operator typed on a law matches what they typed
    on the theme — "Data Protection", "data-protection" and "data protection" are one."""
    return re.sub(r"[^a-z0-9]+", "-", str(value or "").casefold()).strip("-")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def format_export_date(when: datetime | str | None = None) -> str:
    """``27 May 2026`` — no leading zero, and no %-d (which is not portable)."""
    if isinstance(when, str):
        try:
            when = datetime.fromisoformat(when.replace("Z", "+00:00"))
        except ValueError:
            when = None
    when = when or _now()
    return f"{when.day} {when.strftime('%B %Y')}"


def apply_placeholders(text: str, *, when: datetime | None = None, count: int = 0) -> str:
    """Substitute the ``<dateexported>``-style tokens BEFORE the markup is sanitised —
    the sanitiser drops tags it doesn't know, which is exactly what a placeholder is."""
    when = when or _now()
    tokens = {
        "<dateexported>": format_export_date(when),
        "<datetimeexported>": f"{format_export_date(when)}, {when.strftime('%H:%M')} UTC",
        "<yearexported>": str(when.year),
        "<count>": str(count),
    }
    for token, value in tokens.items():
        # a lambda replacement, so a value containing a backslash isn't read as a group ref
        text = re.sub(re.escape(token), lambda _m, v=value: v, text or "",
                      flags=re.IGNORECASE)
    return text


def slugify_filename(value: str, fallback: str = "document") -> str:
    """A filename stem the operator chose, reduced to something safe to write and link.
    Path separators and dot-dot can't survive: these become files in one flat folder."""
    stem = (value or "").strip().casefold().removesuffix(".html")
    stem = _SLUG_SAFE.sub("-", stem.replace("/", "-")).strip("-._")
    return stem or fallback


def default_output_dir(config: Config) -> Path:
    """Beside the catalogue, the raw store and the text store — one bind mount holds the
    lot, so a scheduled export lands on the same disk as everything else."""
    return config.data_dir / "exports" / "site"


def resolve_output_dir(config: Config, configured: str | None) -> Path:
    value = (configured or "").strip()
    return Path(value).expanduser() if value else default_output_dir(config)


def zip_path(config: Config) -> Path:
    return config.data_dir / "exports" / "bundle.zip"


def _stored(key: str, config: Config | None) -> str | None:
    """The freshest value of a UI-managed JSON row.

    Settings normally resolve env-first, and ``apply_to_env`` never overwrites a variable
    already in the process environment — so a value read at boot would be frozen for the
    life of that process. The API edits this plan while the SCHEDULER (a different
    container) exports it, so the scheduler must see the edit without a restart: read the
    file when we know where it is, and fall back to the environment when we don't.
    """
    if config is not None:
        try:
            from .settings import SettingsStore

            value = SettingsStore(config.settings_path)._read_file().get(key)
            if value:
                return str(value)
        except OSError:
            pass
    return os.environ.get(key)


def _clean_webhook(raw) -> dict:
    """The one HTTP call fired when a run finishes, normalised.

    Deliberately a raw URL + method + headers + body template rather than a named
    integration: ntfy, a Slack/Discord incoming hook, a shell listener that starts an
    ``scp``, or anything else that accepts a request all work without new code here.
    """
    data = raw if isinstance(raw, dict) else {}
    url = str(data.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        url = ""
    method = str(data.get("method") or "POST").strip().upper()
    if method not in {"POST", "GET", "PUT"}:
        method = "POST"
    headers: dict[str, str] = {}
    raw_headers = data.get("headers")
    if isinstance(raw_headers, dict):
        for name, value in raw_headers.items():
            name = str(name).strip()
            if name and "\n" not in name:
                headers[name] = str(value).replace("\n", " ").strip()
    elif isinstance(raw_headers, str):
        # One "Name: value" per line — what an operator types into a textarea.
        for line in raw_headers.splitlines():
            if ":" in line:
                name, _, value = line.partition(":")
                if name.strip():
                    headers[name.strip()] = value.strip()
    return {
        "enabled": bool(data.get("enabled")) and bool(url),
        "url": url,
        "method": method,
        "headers": headers,
        "body": str(data.get("body") or ""),
    }


def _clean_only(raw) -> list[str]:
    """The provisions an item is cut down to, de-duplicated and in the order given.

    Accepts a comma-separated string as well as a list, because that is what an operator
    types into a settings field: ``Article 101, Article 102, Article 267``. Empty means
    the whole instrument, which is what every item without this field gets."""
    if isinstance(raw, str):
        raw = raw.split(",")
    seen, out = set(), []
    for label in raw or []:
        name = re.sub(r"\s+", " ", str(label or "")).strip()
        key = name.casefold()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(name)
    return out


def _clean_tags(raw) -> list[str]:
    """An item's themes, de-duplicated and in the order given. A law may carry several —
    the Data Protection Act belongs under privacy AND under law enforcement, and the
    index should list it under both rather than making the operator choose."""
    if isinstance(raw, str):
        raw = raw.split(",")
    seen, out = set(), []
    for tag in raw or []:
        name = str(tag or "").strip()
        key = theme_key(name)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(name)
    return out


def _clean_groups(raw) -> list[dict]:
    """The themed subsections, IN THE OPERATOR'S ORDER — which is the whole point of
    storing them as a list rather than deriving them from the tags in use. A group with
    no name is dropped; a group with no tag of its own is keyed on its name, so typing
    only "Data Protection and Privacy" is enough to start using it."""
    seen, out = set(), []
    for entry in raw or []:
        if isinstance(entry, str):
            entry = {"name": entry}
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        if not name:
            continue
        key = theme_key(entry.get("tag") or name)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append({"name": name, "tag": key})
    return out


def load_config(config: Config | None = None) -> dict:
    """The stored plan, normalised."""
    raw = _stored(CONFIG_KEY, config)
    data: dict = {}
    if raw:
        try:
            parsed = json.loads(raw)
            data = parsed if isinstance(parsed, dict) else {}
        except (ValueError, TypeError):
            data = {}
    items = []
    for entry in data.get("items") or []:
        if not isinstance(entry, dict):
            continue
        stable_id = str(entry.get("stable_id") or "").strip()
        if not stable_id:
            continue
        items.append({
            "stable_id": stable_id,
            "slug": slugify_filename(entry.get("slug") or "", fallback=""),
            "title": str(entry.get("title") or "").strip(),
            "short": str(entry.get("short") or "").strip(),
            "note": str(entry.get("note") or ""),
            "tags": _clean_tags(entry.get("tags")),
            # Optional. A list of provision labels ("Article 101", "s. 5") builds a
            # SUBSET edition of that instrument instead of the whole thing — the point
            # being weight, not display: the TFEU's 191,183 incoming edges make a
            # whole-instrument edition of it unusable, while its competition Articles
            # alone are an ordinary page.
            "only": _clean_only(entry.get("only")),
        })
    groups = _clean_groups(data.get("groups"))
    try:
        max_snippets = max(1, min(int(data.get("max_snippets") or 4), 12))
    except (TypeError, ValueError):
        max_snippets = 4
    out = {
        "items": items,
        "groups": groups,
        "index_title": str(data.get("index_title") or DEFAULT_INDEX_TITLE),
        "index_text": str(data.get("index_text") if data.get("index_text") is not None
                          else DEFAULT_INDEX_TEXT),
        "max_snippets": max_snippets,
        "output_dir": str(data.get("output_dir") or ""),
        "index_wordart": bool(data.get("index_wordart")),
        "sources_page": bool(data.get("sources_page")),
        "sources_intro": str(data.get("sources_intro") or ""),
        "webhook": _clean_webhook(data.get("webhook")),
    }
    if config is not None:
        out["resolved_output_dir"] = str(resolve_output_dir(config, out["output_dir"]))
    return out


def save_config(settings, patch: dict, config: Config | None = None) -> dict:
    """Persist the plan. Slugs are made safe and de-duplicated here rather than at write
    time, so what the operator sees in the table is exactly what lands on disk."""
    current = load_config()
    merged = {**current, **{k: v for k, v in (patch or {}).items() if k in
                            ("items", "groups", "index_title", "index_text",
                             "max_snippets", "output_dir", "index_wordart", "sources_page",
                             "sources_intro", "webhook")}}
    items, used = [], set()
    for entry in merged.get("items") or []:
        if not isinstance(entry, dict):
            continue
        stable_id = str(entry.get("stable_id") or "").strip()
        if not stable_id:
            continue
        stem = slugify_filename(
            entry.get("slug") or entry.get("title") or stable_id, fallback="document")
        candidate, n = stem, 2
        while candidate in used:
            candidate, n = f"{stem}-{n}", n + 1
        used.add(candidate)
        items.append({
            "stable_id": stable_id,
            "slug": candidate,
            "title": str(entry.get("title") or "").strip(),
            "short": str(entry.get("short") or "").strip()[:40],
            "note": str(entry.get("note") or ""),
            "tags": _clean_tags(entry.get("tags")),
            # Optional. A list of provision labels ("Article 101", "s. 5") builds a
            # SUBSET edition of that instrument instead of the whole thing — the point
            # being weight, not display: the TFEU's 191,183 incoming edges make a
            # whole-instrument edition of it unusable, while its competition Articles
            # alone are an ordinary page.
            "only": _clean_only(entry.get("only")),
        })
    merged["items"] = items
    merged["groups"] = _clean_groups(merged.get("groups"))
    try:
        merged["max_snippets"] = max(1, min(int(merged.get("max_snippets") or 4), 12))
    except (TypeError, ValueError):
        merged["max_snippets"] = 4
    merged["webhook"] = _clean_webhook(merged.get("webhook"))
    merged["index_wordart"] = bool(merged.get("index_wordart"))
    merged["sources_page"] = bool(merged.get("sources_page"))
    stored = {k: merged[k] for k in
              ("items", "groups", "index_title", "index_text", "max_snippets",
               "output_dir", "index_wordart", "sources_page", "sources_intro", "webhook")}
    settings.update({CONFIG_KEY: json.dumps(stored, ensure_ascii=False)})
    settings.apply_to_env()  # visible to this process (and this request) immediately
    return load_config(config)


def last_run(config: Config | None = None) -> dict:
    """What the last export did — written by whichever process ran it, so this reads the
    file too (a scheduled run happens in the scheduler container, not the API's)."""
    raw = _stored(LAST_RUN_KEY, config)
    if not raw:
        return {}
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def _record_last_run(facade: Facade, info: dict) -> None:
    try:
        facade.update_settings({LAST_RUN_KEY: json.dumps(info, ensure_ascii=False)})
    except Exception:  # noqa: BLE001 — a bookkeeping failure must not fail the export
        pass


# --- the index page --------------------------------------------------------
# The index title, optionally, as nostalgic WordArt. Transcribed from css-wordart
# (MIT, Shalom Yerushalmy — `raglex design docs/css-wordart/styles.scss`): its `.wordart`
# base and `rainbow` theme, plus the drop shadow the same package builds for its
# `superhero`/`horizon` themes out of a `:before` layer carrying `attr(data-text)`.
# Inlined rather than depended on, because an edition is one self-contained file with no
# build step and no external request.
#
# Only the index page ever wears it. Inside an edition the title is the name of a legal
# instrument, and it stays in the same Times as the law beneath it.
_WORDART_STYLE = """
.wordart {
  font-family: Arial, Helvetica, sans-serif;
  font-size: clamp(2.2rem, 7vw, 4.4rem);
  font-weight: bold;
  position: relative;
  z-index: 1;
  display: inline-flex;
  max-width: 100%;
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
}
.wordart.rainbow {
  /* css-wordart's rainbow is scale(1, 1.5) — tall and narrow, which is faithful to the
     original but reads as cramped at heading size. Widened, and anchored at the left so
     the extra width grows into the page rather than overflowing both margins. */
  transform: scale(1.12, 1.4);
  -webkit-transform: scale(1.12, 1.4);
  transform-origin: left center;
  -webkit-transform-origin: left center;
  letter-spacing: .015em;
  margin: .5em 0 .9em;
}
.wordart.rainbow .text {
  position: relative;
  background: #f42e2c;
  background: linear-gradient(to right, #b306a9, #ef2667, #f42e2c, #ffa509, #fdfc00, #55ac2f, #0b13fd, #a804af);
  -webkit-background-clip: text;
  background-clip: text;
  -webkit-text-fill-color: transparent;
}
/* The shadow sits BEHIND the gradient as its own layer: a text-shadow on the element
   itself would be clipped away with the fill. */
.wordart.rainbow .text::before {
  content: attr(data-text);
  position: absolute;
  left: 0;
  top: 0;
  z-index: -1;
  -webkit-text-fill-color: #b9b2a4;
  text-shadow: 0.02em 0.02em 0 #b9b2a4, 0.04em 0.04em 0 #cdc7ba;
}
/* Print and very old engines get plain, legible ink rather than an invisible title:
   without background-clip support the fill would be transparent over nothing. */
@supports not ((-webkit-background-clip: text) or (background-clip: text)) {
  .wordart .text { -webkit-text-fill-color: currentColor; color: var(--ink); }
  .wordart .text::before { display: none; }
}
@media print {
  .wordart { transform: none; font-size: 2.2rem; }
  .wordart .text { -webkit-text-fill-color: currentColor; color: var(--ink); background: none; }
  .wordart .text::before { display: none; }
}
"""
# Deliberately the same typography as the editions themselves (it reuses their stylesheet)
# and deliberately relative links: every file sits at one level, so the folder works
# opened from disk, served from a static host, or unzipped anywhere.
_INDEX_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light">
  <title>__PAGE_TITLE__</title>
  <style>__STYLE____WORDART_STYLE__
.export-group { margin: 0 0 1.3rem; max-width: 52rem; }
.export-group h2 {
  display: flex;
  align-items: center;
  gap: .5rem;
  margin: 0 0 .15rem;
  padding-bottom: .2rem;
  border-bottom: 1px solid var(--ink);
  font-size: 1.15rem;
  font-weight: 500;
}
.export-group h2 .flag-icon { width: 1.1em; height: 1.1em; }
/* A themed subsection inside a country: italic, the same size as the running text, and
   with no rule of its own above it — the country's solid rule already opened the block,
   and the first theme sits straight beneath it. */
.export-group h3 {
  margin: .85rem 0 0;
  font-size: 1rem;
  font-style: italic;
  font-weight: 400;
}
.export-group h3:first-of-type { margin-top: .35rem; }
/* The rule that CLOSES a subsection. Same width as the country rule above it, dotted
   rather than solid, so the page reads as one document with a hierarchy rather than as
   a stack of boxes. An empty <p> rather than <hr>: it carries no meaning, only the
   spacing of the paragraph it replaces. */
.export-rule {
  margin: .55rem 0 0;
  max-width: 52rem;
  border-bottom: 1px dotted var(--ink);
}
.export-list { list-style: none; margin: 0; padding: 0; max-width: 52rem; }
/* No rule between items: the bold short name already starts each one, and a ruled row
   per instrument turned a reading list into a table. */
.export-list li { padding: .55rem 0 0; }
.export-list a { font-size: 1.15rem; }
.export-short { font-weight: 700; }
/* The annotations read as prose continuing the item's own line — the law's ink, the
   law's size, and stacked at ordinary line spacing rather than as separated blocks. */
.export-meta, .export-note { margin: 0; color: var(--ink); font-size: 1rem; }
</style>
</head>
<body class="no-sidebar">
  <header class="page-head">
    <div>
      <h1>__TITLE__</h1>
      __INTRO__
      __SOURCES_LINK__
    </div>
  </header>
  <div class="page">
    <main>
__ITEMS__
    </main>
  </div>
</body>
</html>
"""

_SOURCES_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light">
  <title>Sources</title>
  <style>
html { color: #111; background: #fff; font-family: "Times New Roman", Times, serif; }
body { max-width: 56rem; margin: 2rem auto; padding: 0 1.25rem 3rem; font-size: 12pt; line-height: 1.35; }
h1 { font-size: 24pt; margin: 0 0 .5rem; }
h2 { font-size: 16pt; margin: 1.5rem 0 .25rem; border-bottom: 1px solid #111; }
h3 { font-size: 12pt; margin: .8rem 0 .15rem; font-style: italic; font-weight: normal; }
p { margin: .45rem 0; }
ul { margin: .15rem 0 .7rem 1.35rem; padding: 0; }
li { margin: .18rem 0; }
details { margin: .18rem 0; }
summary { cursor: pointer; }
details > ul { margin-top: .35rem; }
a { color: #0000ee; text-decoration: underline; }
.back { margin-bottom: 1rem; }
  </style>
</head>
<body>
  <p class="back"><a href="index.html">Back to the statute index</a></p>
  <h1>Sources</h1>
__INTRO__
__JURISDICTIONS__
</body>
</html>
"""

_FRENCH_ADMIN_RE = re.compile(
    r"^(?:CAA|Cour\s+Administrative\s+d['’]Appel|Cour\s+administrative\s+d['’]appel)\s+de\s+(.+)$",
    re.IGNORECASE,
)


def _name_key(value: str) -> str:
    """Accent-, punctuation- and typography-insensitive key for display-name merging."""
    decomposed = unicodedata.normalize("NFKD", value.replace("’", "'").casefold())
    return " ".join("".join(
        char for char in decomposed if not unicodedata.combining(char)).split())


def _verbose_body_name(facade: Facade, court: str | None, source: str) -> str:
    """A publication-grade court/body name, never a storage slug.

    Most names come from the same court registry as Explore. French DILA has several
    capitalisation and abbreviation variants for each administrative appeal court; they
    are normalised here so their counts merge. GDPRhub's ``court-xx`` is a country marker,
    not the identity of a court, so it is described honestly instead of printed as a slug.
    """
    raw = str(court or "").strip()
    if not raw:
        return ""
    if raw.casefold().startswith("court-"):
        key = raw.casefold()
        code = key.split("-", 1)[1]
        country = facade._BODY_JURISDICTIONS.get(key) or facade._DPA_COUNTRY.get(code)
        return (f"{country} courts and tribunals (GDPRhub collection)" if country
                else "Courts and tribunals (GDPRhub collection)")
    match = _FRENCH_ADMIN_RE.match(raw)
    if match:
        place = match.group(1).strip().lower().title()
        return f"Cour administrative d’appel de {place}"
    if _name_key(raw) == "conseil d'etat":
        return "Conseil d’État"
    # RIS abbreviations are names, not user-facing labels.  Expand them here while the
    # raw value is still available; the parent grouping below then collects each level.
    austrian = (
        (r"^OLG\s+(.+)$", "Oberlandesgericht {}"),
        (r"^LG\s+(.+)$", "Landesgericht {}"),
        (r"^BG\s+(.+)$", "Bezirksgericht {}"),
    )
    for pattern, template in austrian:
        if m := re.match(pattern, raw, re.IGNORECASE):
            return template.format(m.group(1).strip())
    austrian_bodies = {
        "datenschutzbehörde": "Austrian Data Protection Authority",
        "datenschutzkommission": "Austrian Data Protection Commission (pre-2014)",
        "gleichbehandlungskommission": "Equal Treatment Commission (Austria)",
        "bundes gleichbehandlungskommission": "Federal Equal Treatment Commission (Austria)",
        "bundesvergabeamt": "Federal Procurement Office (Austria, pre-2014)",
        "bundes vergabekontrollkommission": "Federal Procurement Review Commission (Austria)",
        "vergabekontrollsenat wien": "Vienna Procurement Review Senate",
        "vergabekontrollsenat salzburg": "Salzburg Procurement Review Senate",
        "parlamentarisches datenschutzkomitee": "Parliamentary Data Protection Committee (Austria)",
    }
    if raw.casefold() in austrian_bodies:
        return austrian_bodies[raw.casefold()]
    if "ausl" in raw.casefold():
        upper = raw.upper().replace("_", " ")
        reference_names = (
            ("OGH", "Austrian Supreme Court"),
            ("EGMR", "European Court of Human Rights"),
            ("EGMRH", "European Court of Human Rights"),
            ("EKMR", "European Commission of Human Rights"),
            ("EUGH", "Court of Justice of the European Union"),
            ("BAG", "German Federal Labour Court"),
            ("BGH", "German Federal Court of Justice"),
            ("RG", "German Reich Court"),
        )
        names = list(dict.fromkeys(name for token, name in reference_names
                                   if re.search(rf"\b{token}\b", upper)))
        if names:
            return "Recorded court references: " + "; ".join(names)
    finnish = {
        "korkein hallinto-oikeus": "Supreme Administrative Court of Finland",
        "korkein oikeus": "Supreme Court of Finland",
        "markkinaoikeus": "Market Court of Finland",
        "vakuutusoikeus": "Insurance Court of Finland",
        "työtuomioistuin": "Labour Court of Finland",
        "oikeuskanslerinvirasto": "Office of the Chancellor of Justice (Finland)",
        "tietosuojavaltuutetun toimisto": "Office of the Data Protection Ombudsman (Finland)",
        "valtioneuvosto": "Finnish Government",
    }
    if raw.casefold() in finnish:
        return finnish[raw.casefold()]
    label = str(facade.court_label(raw, source) or "").strip()
    # A registry miss prettifies "nswcatod" as "Nswcatod". That is still a slug, and
    # publishing it would imply a name we do not know. Keep the rows separate but make
    # the limitation explicit and readable rather than presenting the token as a body.
    if re.fullmatch(r"[A-Za-z0-9_-]{2,20}", raw) and re.sub(
            r"[^a-z0-9]", "", label.casefold()) == re.sub(
                r"[^a-z0-9]", "", raw.casefold()):
        return f"Other court or tribunal ({raw.upper()} collection)"
    return label or facade.source_label(source)


def _source_domains(cat) -> dict[tuple[str, str, str], set[str]]:
    """Root hosts represented by full-text records, keyed by source/type/body.

    PostgreSQL extracts and groups hosts in the database, returning only a few hundred
    rows instead of shipping millions of URLs into Python. SQLite is the small/dev path
    and can parse its rows locally. Only records with text participate, matching the
    counts on the page.
    """
    out: dict[tuple[str, str, str], set[str]] = {}
    if cat.backend == "postgres":
        rows = cat.conn.execute(
            "SELECT source, doc_type, court, lower(substring(landing_url from "
            "'^[a-zA-Z][a-zA-Z0-9+.-]*://([^/:?#]+)')) AS host "
            "FROM documents WHERE has_text = 1 AND landing_url IS NOT NULL "
            "GROUP BY source, doc_type, court, host").fetchall()
        pairs = ((r["source"], r["doc_type"], r["court"], r["host"]) for r in rows)
    else:
        rows = cat.conn.execute(
            "SELECT source, doc_type, court, landing_url FROM documents "
            "WHERE has_text = 1 AND landing_url IS NOT NULL").fetchall()
        pairs = ((r["source"], r["doc_type"], r["court"],
                  urlsplit(r["landing_url"] or "").hostname) for r in rows)
    for source, doc_type, court, host in pairs:
        host = str(host or "").casefold().removeprefix("www.").strip(".")
        if host:
            key = (str(source or ""), str(doc_type or ""), str(court or ""))
            out.setdefault(key, set()).add(host)
    return out


def _manual_source(source: str) -> bool:
    value = (source or "").casefold()
    return ("user-import" in value or value.startswith(("manual", "westlaw"))
            or value in {"user", "import"})


def _coe_parent(label: str) -> str:
    """Institutional Council of Europe author, never an individual report author."""
    key = _name_key(label)
    groups = (
        (("committee of ministers", "comite des ministres"),
         "Committee of Ministers"),
        (("parliamentary assembly", "pace"),
         "Parliamentary Assembly of the Council of Europe"),
        (("cepej", "efficiency of justice", "efficacite de la justice"),
         "European Commission for the Efficiency of Justice (CEPEJ)"),
        (("greta", "trafficking in human beings", "traite des etres humains"),
         "Group of Experts on Action against Trafficking in Human Beings (GRETA)"),
        (("grevio", "violence against women", "violence a l'egard des femmes"),
         "Group of Experts on Action against Violence against Women (GREVIO)"),
        (("edqm", "quality of medicines", "qualite du medicament"),
         "European Directorate for the Quality of Medicines & HealthCare (EDQM)"),
        (("venice commission", "democracy through law"),
         "Venice Commission"),
        (("ecri", "racism and intolerance"),
         "European Commission against Racism and Intolerance (ECRI)"),
        (("prevention of torture", "prevention de la torture", "cpt"),
         "Committee for the Prevention of Torture (CPT)"),
        (("cddh", "steering committee for human rights", "directeur pour les droits"),
         "Steering Committee for Human Rights (CDDH)"),
        (("cdadi", "anti discrimination", "anti-discrimination"),
         "Steering Committee on Anti-discrimination, Diversity and Inclusion (CDADI)"),
        (("pompidou group", "groupe pompidou"), "Pompidou Group"),
        (("modern languages", "langues vivantes"),
         "European Centre for Modern Languages"),
        (("secretary general", "secretaire general"), "Secretary General"),
        (("european court of human rights",), "European Court of Human Rights"),
    )
    for needles, name in groups:
        if any(needle in key for needle in needles):
            return name
    return "Other Council of Europe reports and publications"


def _legal_parent(country: str, section: str, label: str) -> str | None:
    """A legally coherent expandable parent for a court/body label."""
    key = _name_key(label)
    if country == "Council of Europe" and section == "Guidance, reports and commentary":
        return _coe_parent(label)
    if section != "Case law":
        return None
    if country == "France":
        rules = (
            (("cour administrative d'appel",), "Cours administratives d’appel"),
            (("cour d'appel",), "Cours d’appel"),
            (("cour de cassation",), "Cour de cassation and its chambers"),
            (("tribunal administratif",), "Tribunaux administratifs"),
            (("tribunal judiciaire",), "Tribunaux judiciaires"),
            (("tribunal de grande instance",), "Tribunaux de grande instance"),
            (("tribunal d'instance",), "Tribunaux d’instance"),
            (("conseil de prud'hommes", "conseil de prud'hommes"),
             "Conseils de prud’hommes"),
            (("cour d'assises",), "Cours d’assises"),
            (("tribunal de commerce",), "Tribunaux de commerce"),
        )
    elif country == "Netherlands":
        rules = (
            (("rechtbank",), "Rechtbanken (district courts)"),
            (("gerechtshof",), "Gerechtshoven (courts of appeal)"),
        )
    elif country == "Germany":
        rules = (
            (("landessozialgericht",), "Landessozialgerichte (state social courts)"),
            (("sozialgericht",), "Sozialgerichte (social courts)"),
            (("landesarbeitsgericht",), "Landesarbeitsgerichte (state labour courts)"),
            (("arbeitsgericht",), "Arbeitsgerichte (labour courts)"),
            (("oberverwaltungsgericht", "verwaltungsgerichtshof"),
             "Oberverwaltungsgerichte / Verwaltungsgerichtshöfe"),
            (("verwaltungsgericht",), "Verwaltungsgerichte (administrative courts)"),
            (("oberlandesgericht",), "Oberlandesgerichte (higher regional courts)"),
            (("landgericht",), "Landgerichte (regional courts)"),
            (("amtsgericht",), "Amtsgerichte (local courts)"),
            (("finanzgericht",), "Finanzgerichte (fiscal courts)"),
            (("verfassungsgerichtshof", "staatsgerichtshof", "landesverfassungsgericht"),
             "State constitutional courts"),
        )
    elif country == "Austria":
        rules = (
            (("landesverwaltungsgericht",), "Landesverwaltungsgerichte (state administrative courts)"),
            (("oberlandesgericht",), "Oberlandesgerichte (higher regional courts)"),
            (("landesgericht",), "Landesgerichte (regional courts)"),
            (("bezirksgericht",), "Bezirksgerichte (district courts)"),
            (("ausl ", "ausl_", "ausl;", "ogh, ausl", "ogh; ausl", "ogh,ausl",
              "recorded court references"),
             "Decisions carrying foreign or international court references"),
        )
    elif country == "United Kingdom":
        rules = (
            (("court of session",), "Court of Session (Scotland)"),
            (("ni high court",), "High Court of Justice in Northern Ireland"),
        )
    else:
        return None
    for needles, parent in rules:
        if any(needle in key for needle in needles):
            return parent
    return None


def _combine_items(label: str, children: list[dict]) -> dict:
    years_from = [c["year_from"] for c in children if c.get("year_from") is not None]
    years_to = [c["year_to"] for c in children if c.get("year_to") is not None]
    return {
        "section": children[0]["section"], "label": label,
        "count": sum(int(c["count"]) for c in children),
        "year_from": min(years_from) if years_from else None,
        "year_to": max(years_to) if years_to else None,
        "domains": sorted({v for c in children for v in c.get("domains") or []}),
        "sources": sorted({v for c in children for v in c.get("sources") or []}),
        "manual": any(c.get("manual") for c in children),
        "details": sorted(children, key=lambda c: (-int(c["count"]), c["label"])),
    }


def _group_source_entries(country: str, entries: list[dict]) -> list[dict]:
    """Collapse long institutional tails, preserving every original row in ``details``."""
    parents: dict[tuple[str, str], list[dict]] = {}
    loose: list[dict] = []
    for item in entries:
        parent = _legal_parent(country, item["section"], item["label"])
        if parent:
            parents.setdefault((item["section"], parent), []).append(item)
        else:
            loose.append(item)
    combined: list[dict] = []
    for (_section, parent), children in parents.items():
        # Council of Europe author strings are never useful top-level headings even when
        # a committee happened to contribute only one item. Elsewhere one member is not a
        # group and remains an ordinary row.
        if len(children) > 1 or country == "Council of Europe":
            combined.append(_combine_items(parent, children))
        else:
            loose.extend(children)

    # A residual handful of one-off bodies should not make a country hundreds of lines
    # long. This is explicitly a browse bucket, not a claim of legal equivalence, and its
    # disclosure retains every court/body and its own count, dates and sources.
    tail_names = {
        "Case law": "Other courts and tribunals",
        "Administrative decisions": "Other administrative bodies",
        "Guidance, reports and commentary": "Other guidance, reports and commentary",
    }
    for section, parent in tail_names.items():
        tail = [item for item in loose if item["section"] == section
                and int(item["count"]) <= 5]
        if len(tail) >= 3:
            loose = [item for item in loose if item not in tail]
            combined.append(_combine_items(parent, tail))
    return loose + combined


def build_sources_summary(facade: Facade, *, current_year: int | None = None) -> dict:
    """Full-text corpus inventory for the optional sources page.

    It reads the Explore roll-up, so the country totals and material classification are
    exactly the ones a reader sees in the application and no multi-million-row recount is
    introduced. Future-dated records count as held but cannot push a displayed year range
    beyond the year in which the page is generated.
    """
    current_year = int(current_year or _now().year)
    with facade._open() as (cat, _rs, _ts):
        rows = cat.corpus_shape_stats()
        if not rows:
            cat.refresh_corpus_shape_stats()
            rows = cat.corpus_shape_stats()
        domains = _source_domains(cat)

    jurisdictions: dict[str, dict] = {}
    corpus_total = 0
    for row in rows:
        corpus_total += int(row["n"] or 0)
        count = int(row["with_text"] or 0)
        if count <= 0:
            continue
        source = str(row["source"] or "")
        court = str(row["court"] or "")
        doc_type = str(row["doc_type"] or "other")
        # BIPT republishes a tiny set of CJEU judgments already held from the Court's
        # own register. They are neither Belgian case law nor an additional EU source,
        # so listing them creates exactly the misleading orphan row this page avoids.
        if source == "be-bipt-judgments" and court.casefold() == "cjeu":
            continue
        jurisdiction = facade._doc_bucket(source, court)
        country = jurisdictions.setdefault(
            jurisdiction, {"name": jurisdiction, "total": 0, "groups": {}})
        country["total"] += count

        kind = facade._doc_kind(source, doc_type, court)
        if jurisdiction == "European Union" and court.casefold() == "advocate general":
            section, label = "Opinions of the Advocates General", "Opinions of the Advocates General"
        elif kind == "legislation":
            section, label = "Legislation", "Legislation"
        elif kind == "cases":
            section = "Case law"
            label = _verbose_body_name(facade, court, source) or "Other case law"
        elif kind == "administrative":
            section = "Administrative decisions"
            label = _verbose_body_name(facade, court, source) or "Administrative decisions"
            if label.startswith("Other court or tribunal ("):
                label = facade.source_label(source)
        elif kind == "guidance":
            section = "Guidance, reports and commentary"
            label = (_verbose_body_name(facade, court, source)
                     or "Guidance, reports and commentary")
            if label.startswith("Other court or tribunal ("):
                label = facade.source_label(source)
        elif kind == "preparatory":
            section, label = "Legislative and preparatory materials", "Legislative and preparatory materials"
        elif kind == "explanatory":
            section, label = "Explanatory materials", "Explanatory materials"
        else:
            section = "Other materials"
            label = doc_type.replace("_", " ").capitalize()

        # Label is part of the key: equivalent DILA spellings merge after normalisation,
        # while genuinely distinct courts remain separate entries.
        key = (section, _name_key(label))
        item = country["groups"].setdefault(key, {
            "section": section, "label": label, "count": 0,
            "years": set(), "sources": set(), "domains": set(), "manual": False,
        })
        item["count"] += count
        item["sources"].add(source)
        item["domains"].update(domains.get((source, doc_type, court), set()))
        item["manual"] = item["manual"] or _manual_source(source)
        year = str(row["yr"] or "")
        if year.isdigit():
            parsed_year = int(year)
            # Ancient legislation is real; a modern court recorded in year 201 is not.
            # DILA contains a handful of truncated 201x dates, which must not make a
            # court's published coverage claim read "201–2022".
            if kind == "legislation" or parsed_year >= 1000:
                item["years"].add(min(parsed_year, current_year))

    section_order = {
        "Legislation": 0, "Case law": 1, "Opinions of the Advocates General": 2,
        "Administrative decisions": 3, "Guidance, reports and commentary": 4,
        "Legislative and preparatory materials": 5, "Explanatory materials": 6,
        "Other materials": 7,
    }
    countries = []
    for country in jurisdictions.values():
        entries = []
        for item in country.pop("groups").values():
            years = sorted(item.pop("years"))
            item["year_from"] = years[0] if years else None
            item["year_to"] = years[-1] if years else None
            item["domains"] = sorted(item["domains"])
            item["sources"] = sorted(item["sources"])
            entries.append(item)
        entries.sort(key=lambda item: (
            section_order.get(item["section"], 99), -item["count"], item["label"]))
        entries = _group_source_entries(country["name"], entries)
        entries.sort(key=lambda item: (
            section_order.get(item["section"], 99), -item["count"], item["label"]))
        country["entries"] = entries
        countries.append(country)
    countries.sort(key=lambda country: (-country["total"], country["name"]))
    return {"corpus_total": corpus_total, "full_text_total": sum(c["total"] for c in countries),
            "jurisdictions": countries, "current_year": current_year}


def _source_links(item: dict, facade: Facade) -> str:
    values = []
    for host in item.get("domains") or []:
        note = (" (third-party bulk download, not scraped)"
                if host in {"bailii.org", "canlii.org"} else "")
        values.append(f'<a href="https://{escape(host, quote=True)}/">{escape(host)}</a>{note}')
    if item.get("manual"):
        values.append("manual uploads")
    if not values:
        values.extend(escape(facade.source_label(source))
                      for source in item.get("sources") or [])
    return ", ".join(dict.fromkeys(values))


def render_sources_html(summary: dict, *, intro: str, facade: Facade,
                        generated_at: datetime | None = None) -> str:
    generated_at = generated_at or _now()
    intro_html = editorial_paragraphs(
        apply_placeholders(intro, when=generated_at,
                           count=int(summary.get("full_text_total") or 0)), "attribution")
    def item_text(item: dict) -> str:
        years = ""
        if item.get("year_from") is not None:
            years = (f", {item['year_from']}–{item['year_to']}"
                     if item["year_from"] != item["year_to"] else f", {item['year_from']}")
        sources = _source_links(item, facade)
        count = int(item["count"])
        noun = "document" if count == 1 else "documents"
        return (f"{escape(item['label'])} ({count:,} {noun}{years})"
                + (f" from {sources}" if sources else ""))

    blocks = []
    for country in summary.get("jurisdictions") or []:
        parts = [f'  <section>\n    <h2>{escape(country["name"])} '
                 f'({int(country["total"]):,} full-text documents)</h2>']
        current_section = None
        for index, item in enumerate(country["entries"]):
            if item["section"] != current_section:
                current_section = item["section"]
                parts.append(f"    <h3>{escape(current_section)}</h3>\n    <ul>")
            if item.get("details"):
                parts.append(f"      <li><details><summary>{item_text(item)}</summary>\n"
                             "        <ul>")
                parts.extend(f"          <li>{item_text(child)}</li>"
                             for child in item["details"])
                parts.append("        </ul>\n      </details></li>")
            else:
                parts.append(f"      <li>{item_text(item)}</li>")
            # Close before the next heading (or after the final item).
            next_index = index + 1
            if (next_index == len(country["entries"])
                    or country["entries"][next_index]["section"] != current_section):
                parts.append("    </ul>")
        parts.append("  </section>")
        blocks.append("\n".join(parts))
    return (_SOURCES_TEMPLATE.replace("__INTRO__", "\n".join(f"  {p}" for p in intro_html))
            .replace("__JURISDICTIONS__", "\n".join(blocks)))


def _themed_sections(group: list[dict], groups: list[dict]) -> list[tuple[str, list[dict]]]:
    """One jurisdiction's editions split into ``(heading, entries)`` themed sections.

    Three rules, all the operator's:

    * the SECTIONS come in the order the themes were configured, not the order the laws
      were added and not alphabetically — a reading list has a shape, and "Data
      Protection and Privacy" before "Platform Regulation" is an editorial judgement;
    * WITHIN a section the laws are ordered by how heavily cited they are, so the ones a
      reader is most likely to want are at the top of each theme;
    * a law may carry SEVERAL themes and is listed under each of them. The Data
      Protection Act belongs under privacy and under law enforcement both, and making
      the operator pick one would misrepresent the law to save a line.

    Returns a single unheaded section when no theme applies, so a set that has never
    been themed renders exactly as it always did.
    """
    if not groups:
        return [("", list(group))]

    def weight(entry: dict) -> tuple:
        # Total citations, then citing documents, then the title — a deterministic order
        # even between two laws nothing has cited yet.
        return (-int(entry.get("mentions") or 0), -int(entry.get("documents") or 0),
                str(entry.get("title") or ""))

    keyed = [(entry, {theme_key(t) for t in entry.get("tags") or []}) for entry in group]
    sections: list[tuple[str, list[dict]]] = []
    placed: set[int] = set()
    for theme in groups:
        rows = []
        for n, (entry, tags) in enumerate(keyed):
            if theme["tag"] in tags:
                rows.append(entry)
                placed.add(n)
        if rows:
            sections.append((theme["name"], sorted(rows, key=weight)))
    rest = [entry for n, (entry, _tags) in enumerate(keyed) if n not in placed]
    if not sections:
        # No theme reached this jurisdiction at all — render it exactly as an unthemed
        # set always has, in the order the operator arranged.
        return [("", list(group))]
    if rest:
        # Never silently dropped. An untagged law keeps its place in the index under an
        # honest heading, and tagging it makes the heading disappear.
        sections.append((_UNTHEMED, sorted(rest, key=weight)))
    return sections


def render_index_html(
    entries: list[dict], *, title: str, intro: str,
    generated_at: datetime | None = None, wordart: bool = False,
    groups: list[dict] | None = None,
    corpus_total: int | None = None, sources_page: bool = False,
) -> str:
    """``entries`` are the built editions: filename, title, short name, jurisdiction,
    last-updated date, both counts, note, themes.

    Editions are grouped by the jurisdiction that made the instrument, in the order those
    jurisdictions first appear in the operator's own list — so the set stays arranged the
    way it was configured, only sectioned. ``groups`` subdivides each jurisdiction into
    named themes; see :func:`_themed_sections`.
    """
    from html import escape

    groups = _clean_groups(groups)

    generated_at = generated_at or _now()
    intro_paragraphs = editorial_paragraphs(
        apply_placeholders(intro, when=generated_at, count=len(entries)), "attribution")
    flags = _flag_assets({
        str(entry.get("jurisdiction") or "") for entry in entries
        if entry.get("jurisdiction")
    })

    grouped: dict[str, list[dict]] = {}
    for entry in entries:
        grouped.setdefault(str(entry.get("jurisdiction") or _UNPLACED), []).append(entry)

    def render_rows(section: list[dict]) -> str:
        rows = []
        for entry in section:
            # One sentence, not a row of dotted fields: the same facts read as the
            # annotation they are.
            meta = [
                "Last updated "
                + str(entry.get("exported") or format_export_date(generated_at))
            ]
            if entry.get("documents"):
                meta.append(f"cited by {int(entry['documents']):,} documents")
            # Always more than the document count once anything cites a law twice, and it
            # is the number a reader of the edition itself will see.
            if entry.get("mentions"):
                meta.append(f"{int(entry['mentions']):,} citations in all")
            # Only where there ARE any: a UK statute has nothing before the Court of
            # Justice, and a clause saying "0 pending" would be noise on every line.
            pending = int(entry.get("pending") or 0)
            if pending:
                meta.append(f"{pending:,} pending CJEU "
                            f"{'case' if pending == 1 else 'cases'}")
            note_paragraphs = editorial_paragraphs(
                apply_placeholders(entry.get("note") or "", when=generated_at,
                                   count=len(entries)),
                "export-note")
            short = str(entry.get("short") or "").strip()
            # The EEA-relevance note is a statement about territorial scope, not part of
            # the name — and repeated down a page of EU instruments it is the same eight
            # words over and over. The edition itself keeps the official title.
            name = strip_eea_relevance(entry["title"])
            # "DSA: Regulation (EU) 2022/2065 …" — the operator's own shorthand, bold, and
            # only here: inside an edition the instrument speaks under its full name.
            label = (
                f'<span class="export-short">{escape(short)}:</span> {escape(name)}'
                if short else escape(name)
            )
            rows.append(
                "          <li>\n"
                f'            <a href="{escape(entry["filename"], quote=True)}">{label}</a>\n'
                f'            <p class="export-meta">{escape(", ".join(meta))}.</p>\n'
                + "".join(f"            {paragraph}\n"
                          for paragraph in note_paragraphs)
                + "          </li>"
            )
        return "\n".join(rows)

    blocks = []
    for jurisdiction, group in grouped.items():
        flag = flags.get(jurisdiction)
        icon = f'<img class="flag-icon" src="{escape(flag, quote=True)}" alt="">' if flag else ""
        sections = []
        for heading, section in _themed_sections(group, groups):
            # Italic, no rule of its own above it, and a dotted rule CLOSING it — the
            # subsection shape of an ordinary Word document. The country keeps the one
            # solid rule under its own name, so the first theme sits straight beneath it.
            sections.append(
                (f"        <h3>{escape(heading)}</h3>\n" if heading else "")
                + '        <ul class="export-list">\n'
                + render_rows(section) + "\n"
                + "        </ul>\n"
                + ('        <p class="export-rule"></p>\n' if heading else "")
            )
        blocks.append(
            '      <section class="export-group">\n'
            f"        <h2>{icon}{escape(jurisdiction)}</h2>\n"
            + "".join(sections)
            + "      </section>"
        )

    # The WordArt is a decoration on the SAME <h1>, not a replacement for it — the
    # heading stays one element with the title as its text, so a screen reader, a search
    # engine and a print stylesheet all still find it.
    heading = (
        f'<span class="wordart rainbow"><span class="text" '
        f'data-text="{escape(title, quote=True)}">{escape(title)}</span></span>'
        if wordart else escape(title)
    )
    sources_link = ""
    if sources_page:
        sources_link = (
            f'<p class="attribution">{int(corpus_total or 0):,} documents analysed '
            'to make this system — <a href="sources.html">information about the '
            'sources here</a>.</p>')
    page = _INDEX_TEMPLATE
    for token, value in {
        "__PAGE_TITLE__": escape(title, quote=True),
        "__TITLE__": heading,
        "__INTRO__": "\n      ".join(intro_paragraphs),
        "__SOURCES_LINK__": sources_link,
        "__STYLE__": _STYLE,
        "__WORDART_STYLE__": _WORDART_STYLE if wordart else "",
        "__ITEMS__": "\n".join(blocks),
    }.items():
        page = page.replace(token, value)
    return page


# --- telling something else the folder changed ------------------------------
# One outbound request when a run finishes, so whatever should happen next — an ntfy
# push, an scp to a public host, a rebuild — is started by the machine that cares,
# without RagLex knowing anything about it.
WEBHOOK_PLACEHOLDERS = (
    "{documents} {output_dir} {bytes} {finished_at} {started_at} {titles} {zip}")


def webhook_body(template: str, result: dict) -> str:
    """Fill the operator's template. An empty template sends the run summary as JSON —
    which is what a generic listener wants, while ntfy wants a line of text."""
    if not (template or "").strip():
        return json.dumps(result, ensure_ascii=False, default=str)
    values = {
        "documents": str(result.get("documents") or 0),
        "output_dir": str(result.get("output_dir") or ""),
        "bytes": str(result.get("bytes") or 0),
        "finished_at": str(result.get("finished_at") or ""),
        "started_at": str(result.get("started_at") or ""),
        "zip": str(result.get("zip") or ""),
        "titles": ", ".join(
            str(f.get("title") or "") for f in (result.get("files") or [])),
    }
    out = template
    for token, value in values.items():
        out = out.replace("{" + token + "}", value)
    return out


def fire_webhook(webhook: dict | None, result: dict, *, timeout: float = 20.0) -> dict | None:
    """Send it, and never let it fail the export — the folder is already written, and a
    dead notification endpoint must not turn a good run into a failed job. What happened
    is recorded in the run summary instead."""
    hook = _clean_webhook(webhook)
    if not hook["enabled"]:
        return None
    body = webhook_body(hook["body"], result)
    try:
        import httpx

        headers = dict(hook["headers"])
        headers.setdefault(
            "Content-Type",
            "application/json" if not hook["body"].strip() else "text/plain; charset=utf-8")
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            if hook["method"] == "GET":
                response = client.get(hook["url"], headers=headers)
            else:
                response = client.request(
                    hook["method"], hook["url"], headers=headers,
                    content=body.encode("utf-8"))
        return {"url": hook["url"], "status": response.status_code,
                "ok": response.is_success,
                "at": _now().isoformat(timespec="seconds")}
    except Exception as exc:  # noqa: BLE001 — reported, never raised
        return {"url": hook["url"], "error": str(exc)[:300],
                "ok": False, "at": _now().isoformat(timespec="seconds")}


# --- the build ------------------------------------------------------------


def _repoint_to_current_versions(facade: Facade, items: list[dict]) -> list[dict]:
    """Move each edition onto the newest readable expression of its law, in place.

    Returns the moves made, for the run record — an operator should be able to see that
    an edition changed which text it publishes, and to what.
    """
    moves: list[dict] = []
    with facade._open() as (cat, _rs, _ts):
        for item in items:
            current = str(item.get("stable_id") or "")
            try:
                newer = cat.latest_readable_version(current)
            except Exception:  # noqa: BLE001 — a bad id must not fail the whole export
                continue
            if not newer or newer == current:
                continue
            row = cat.get_document(newer)
            if row is None:
                continue
            moves.append({"from": current, "to": newer,
                          "title": item.get("title") or ""})
            item["stable_id"] = newer
            # The title moves with it: a consolidation carries the date in its own name,
            # and an edition labelled with the superseded one would misdescribe the text
            # underneath. The operator's SHORT name and note are theirs, and are kept.
            if row["title"]:
                item["title"] = row["title"]
    return moves


def _refresh_revised_in_place_items(facade: Facade, items: list[dict]) -> list[dict]:
    """Refresh legislation.gov.uk bases before deciding which expression is latest.

    A dated ``@...`` snapshot can only be compared fairly with the revised-in-place base
    when the base has just disclosed its current ``currency.as_at``.  Otherwise an old
    snapshot wins merely because its date is explicit — exactly how the UK GDPR static
    page remained on 2026-03-01 while the publisher's base had moved to 2026-06-19.
    """
    bases: list[str] = []
    with facade._open() as (cat, _rs, _ts):
        for item in items:
            current = str(item.get("stable_id") or "")
            base, _version = cat.version_base_and_date(current)
            row = cat.get_document(base)
            if row is not None and row["source"] == "uk-legislation" and base not in bases:
                bases.append(base)
    results = []
    for base in bases:
        result = facade.ensure_uk_legislation_current(stable_id=base)
        results.append(result)
        if result.get("error"):
            raise RuntimeError(
                f"could not verify the current legislation.gov.uk text for {base}: "
                f"{result['error']}")
    return results


def build_bundle(
    facade: Facade,
    params: dict | None = None,
    on_progress: Callable[..., None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> dict:
    """Build every configured edition, write the folder, and (when asked) a zip.

    ``refresh`` (default true) re-reads the corpus for each edition. With it off, an
    already-built payload is re-rendered instead — which is what changing a note, the
    preamble or the index text needs, and takes seconds rather than hours.
    """
    params = dict(params or {})
    config = load_config(facade.config)
    items = config["items"]
    if not items:
        return {"error": "no documents are configured for the static bundle"}
    refresh = params.get("refresh", True)
    base_url = public_base_url()
    want_zip = bool(params.get("zip"))
    max_snippets = config["max_snippets"]
    out_dir = resolve_output_dir(facade.config, config["output_dir"])
    started = _now()

    def emit(done: int, item: str, *, sub_done: int = 0, sub_total: int = 0) -> None:
        if on_progress:
            on_progress(stage="editions", done=done, total=len(items) + 1, item=item,
                        sub_done=sub_done, sub_total=sub_total)

    def check_cancelled() -> None:
        if cancel_check and cancel_check():
            raise RuntimeError("static bundle export cancelled")

    # Check revised-in-place UK sources first. Repointing without this check compares an
    # explicitly dated snapshot against a possibly stale/undated base and can select the
    # old snapshot forever. A non-refresh render deliberately reuses the prior payload.
    source_refreshes = _refresh_revised_in_place_items(facade, items) if refresh else []

    # An edition names one expression of a law, and laws are consolidated again. Left
    # alone, a set published from "the ePrivacy Directive as at 2009-12-19" keeps
    # publishing that text for ever, quietly going out of date. So before building,
    # every item is re-pointed at the newest held expression that HAS TEXT and is in
    # force today — the same rule the reader applies when it opens an act — and the
    # move is written back to the configuration, so the set tracks forward from here
    # rather than needing this decision made again next time.
    repointed = _repoint_to_current_versions(facade, items)
    if repointed:
        save_config(facade.settings, {"items": items}, facade.config)

    entries: list[dict] = []
    files: list[tuple[str, bytes]] = []
    for index, item in enumerate(items):
        check_cancelled()
        stable_id = item["stable_id"]
        label = item["title"] or stable_id
        position = f"{index + 1} of {len(items)}"

        def item_progress(done: int, total: int, _label=label, _pos=position, _i=index) -> None:
            check_cancelled()
            emit(_i, f"{_label} ({_pos}) — reading excerpts from {done:,} of "
                     f"{total:,} citing documents", sub_done=done, sub_total=total)

        only = list(item.get("only") or []) or None
        status = static_export_status(
            facade.config, stable_id, max_snippets=max_snippets, only=only)
        if refresh or not status.get("ready"):
            emit(index, f"{label} ({position}) — gathering the law and everything citing it")
            build_static_export_cache(
                facade, stable_id, max_snippets=max_snippets,
                on_progress=lambda **p: item_progress(
                    int(p.get("done") or 0), int(p.get("total") or 0)),
                cancel_check=cancel_check, only=only,
            )
            status = static_export_status(
                facade.config, stable_id, max_snippets=max_snippets, only=only)
        else:
            emit(index, f"{label} ({position}) — re-using the edition built "
                        f"{format_export_date(status.get('generated_at'))}")
        if not status.get("ready"):
            raise RuntimeError(f"could not build the static edition of {stable_id}")

        emit(index, f"{label} ({position}) — writing {item['slug']}.html")
        # Every edition links back to the index by its title — the set reads as one site,
        # not a scatter of files, however a reader arrived at this one.
        html_bytes = render_cached_export(
            status, note=item.get("note"),
            canonical=(f"{base_url}/{item['slug']}.html" if base_url else None),
            index_link={"href": "index.html", "title": config["index_title"]},
            # "Crossreferenced AI Act - UCL Digital Laws": a tab, a bookmark and a
            # browser history entry all get the short name plus the set it belongs to,
            # never the instrument's 40-word official title.
            page_title=cached_export_page_title(
                status, short=item.get("short"), index_title=config["index_title"]),
            # The contents column heads itself with the name the SET gave this law
            # ("GDPR"), not the citable stem the catalogue holds it under.
            short_title=item.get("short") or None)
        filename = f"{item['slug']}.html"
        files.append((filename, html_bytes))
        entries.append({
            "filename": filename,
            "slug": item["slug"],
            "stable_id": stable_id,
            "title": status.get("title") or label,
            "short": item.get("short") or "",
            "jurisdiction": status.get("jurisdiction") or "",
            "note": item.get("note") or "",
            "tags": list(item.get("tags") or []),
            "subset": list(status.get("subset") or []),
            "documents": int(status.get("documents") or 0),
            "mentions": int(status.get("mentions") or 0),
            "pending": int(status.get("pending") or 0),
            "bytes": len(html_bytes),
            "exported": format_export_date(status.get("generated_at") or started),
        })

    check_cancelled()
    emit(len(items), f"writing index.html for {len(entries)} editions")
    sources_summary = None
    if config.get("sources_page"):
        emit(len(items), "summarising the full-text corpus for sources.html")
        sources_summary = build_sources_summary(facade, current_year=started.year)
        files.append(("sources.html", render_sources_html(
            sources_summary, intro=config.get("sources_intro") or "", facade=facade,
            generated_at=started).encode("utf-8")))
    index_html = render_index_html(
        entries, title=config["index_title"], intro=config["index_text"],
        generated_at=started, wordart=config["index_wordart"],
        groups=config.get("groups"),
        corpus_total=(sources_summary or {}).get("corpus_total"),
        sources_page=bool(sources_summary),
    ).encode("utf-8")
    files.append(("index.html", index_html))

    # A crawler finds pages by being told where they are. Without a sitemap it has to
    # discover each edition through the index, and without robots.txt it has no pointer to
    # the sitemap at all. Both need absolute urls, so both are written only when the site's
    # address is configured (RAGLEX_STATIC_BASE_URL / RAGLEX_PUBLIC_URL) rather than
    # guessing a hostname that would send crawlers somewhere wrong.
    if base_url:
        # ``started`` is a datetime (_now()), not the ISO string this once assumed —
        # slicing it raised at the very end of a build, after every page was written.
        today = started.date().isoformat() if started else ""
        urls = [("index.html", "1.0")]
        if sources_summary:
            urls.append(("sources.html", "0.7"))
        urls += [(e["filename"], "0.8") for e in entries]
        sitemap = ['<?xml version="1.0" encoding="UTF-8"?>',
                   '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
        for filename, priority in urls:
            sitemap.append(
                f"  <url><loc>{_xml(base_url)}/{_xml(filename)}</loc>"
                + (f"<lastmod>{_xml(today)}</lastmod>" if today else "")
                + f"<changefreq>weekly</changefreq><priority>{priority}</priority></url>")
        sitemap.append("</urlset>")
        files.append(("sitemap.xml", "\n".join(sitemap).encode("utf-8")))
        files.append(("robots.txt", (
            "User-agent: *\n"
            "Allow: /\n"
            f"Sitemap: {base_url}/sitemap.xml\n"
        ).encode("utf-8")))
        emit(len(items), f"writing sitemap.xml for {len(urls)} pages")

    # The folder is written every run — scheduled or manual — and same-named files are
    # replaced outright. It is a mirror of the current corpus, not an archive.
    out_dir.mkdir(parents=True, exist_ok=True)
    if not sources_summary:
        # Turning the optional page off must turn it off on disk too. Leaving the prior
        # file behind would keep publishing stale corpus claims at a known URL.
        (out_dir / "sources.html").unlink(missing_ok=True)
    for filename, payload in files:
        temporary = out_dir / f".{filename}.tmp"
        temporary.write_bytes(payload)
        temporary.replace(out_dir / filename)

    result = {
        "documents": len(entries),
        "output_dir": str(out_dir),
        "files": [
            {k: entry[k] for k in
             ("filename", "title", "jurisdiction", "documents", "mentions", "pending",
              "bytes", "exported")}
            for entry in entries
        ],
        "bytes": sum(len(payload) for _name, payload in files),
        "finished_at": _now().isoformat(timespec="seconds"),
        "started_at": started.isoformat(timespec="seconds"),
        "refreshed": bool(refresh),
        # Editions moved onto a newer consolidation by this run — the set changed which
        # text it publishes, which the operator should be told rather than discover.
        **({"repointed": repointed} if repointed else {}),
        **({"source_refreshes": source_refreshes} if source_refreshes else {}),
    }
    if want_zip:
        emit(len(items) + 1, f"packing {len(files)} files into a zip")
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for filename, payload in files:
                archive.writestr(filename, payload)
        target = zip_path(facade.config)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".zip.tmp")
        temporary.write_bytes(buffer.getvalue())
        temporary.replace(target)
        result["zip"] = str(target)
        result["zip_bytes"] = target.stat().st_size
        result["zip_filename"] = (
            f"raglex-static-export-{started.strftime('%Y%m%d')}.zip")
    hook = fire_webhook(config.get("webhook"), result)
    if hook:
        result["webhook"] = hook
    _record_last_run(facade, result)
    emit(len(items) + 1, f"done — {len(entries)} editions in {out_dir}")
    return result
