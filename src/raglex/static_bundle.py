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
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .config import Config
from .facade import Facade
from .static_export import (
    _STYLE,
    _flag_assets,
    build_static_export_cache,
    render_cached_export,
    sanitise_editorial_html,
    static_export_status,
)

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
        })
    try:
        max_snippets = max(1, min(int(data.get("max_snippets") or 4), 12))
    except (TypeError, ValueError):
        max_snippets = 4
    out = {
        "items": items,
        "index_title": str(data.get("index_title") or DEFAULT_INDEX_TITLE),
        "index_text": str(data.get("index_text") if data.get("index_text") is not None
                          else DEFAULT_INDEX_TEXT),
        "max_snippets": max_snippets,
        "output_dir": str(data.get("output_dir") or ""),
        "index_wordart": bool(data.get("index_wordart")),
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
                            ("items", "index_title", "index_text", "max_snippets",
                             "output_dir", "index_wordart", "webhook")}}
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
        })
    merged["items"] = items
    try:
        merged["max_snippets"] = max(1, min(int(merged.get("max_snippets") or 4), 12))
    except (TypeError, ValueError):
        merged["max_snippets"] = 4
    merged["webhook"] = _clean_webhook(merged.get("webhook"))
    merged["index_wordart"] = bool(merged.get("index_wordart"))
    stored = {k: merged[k] for k in
              ("items", "index_title", "index_text", "max_snippets", "output_dir",
               "index_wordart", "webhook")}
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
.export-group { margin: 0 0 2.2rem; max-width: 52rem; }
.export-group h2 {
  display: flex;
  align-items: center;
  gap: .5rem;
  margin: 0 0 .2rem;
  padding-bottom: .35rem;
  border-bottom: 1px solid var(--ink);
  font-size: 1.15rem;
  font-weight: 500;
}
.export-group h2 .flag-icon { width: 1.1em; height: 1.1em; }
.export-list { list-style: none; margin: 0; padding: 0; max-width: 52rem; }
.export-list li { padding: 1.1rem 0; border-bottom: 1px dotted var(--faint-rule); }
.export-list li:last-child { border-bottom: 0; }
.export-list a { font-size: 1.15rem; }
.export-short { font-weight: 700; }
.export-meta { margin: .3rem 0 0; color: var(--quiet); font-size: .95rem; }
.export-note { margin: .45rem 0 0; color: var(--quiet); font-size: 1rem; line-height: 1.45; }
</style>
</head>
<body class="sidebar-closed">
  <header class="page-head">
    <div>
      <h1>__TITLE__</h1>
      __INTRO__
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


def render_index_html(
    entries: list[dict], *, title: str, intro: str,
    generated_at: datetime | None = None, wordart: bool = False,
) -> str:
    """``entries`` are the built editions: filename, title, short name, jurisdiction,
    last-updated date, both counts, note.

    Editions are grouped by the jurisdiction that made the instrument, in the order those
    jurisdictions first appear in the operator's own list — so the set stays arranged the
    way it was configured, only sectioned.
    """
    from html import escape

    generated_at = generated_at or _now()
    intro_html = sanitise_editorial_html(
        apply_placeholders(intro, when=generated_at, count=len(entries)))
    flags = _flag_assets({
        str(entry.get("jurisdiction") or "") for entry in entries
        if entry.get("jurisdiction")
    })

    grouped: dict[str, list[dict]] = {}
    for entry in entries:
        grouped.setdefault(str(entry.get("jurisdiction") or _UNPLACED), []).append(entry)

    blocks = []
    for jurisdiction, group in grouped.items():
        flag = flags.get(jurisdiction)
        icon = f'<img class="flag-icon" src="{escape(flag, quote=True)}" alt="">' if flag else ""
        rows = []
        for entry in group:
            meta = [
                "Last updated: "
                + str(entry.get("exported") or format_export_date(generated_at))
            ]
            if entry.get("documents"):
                meta.append(f"{int(entry['documents']):,} citing documents")
            # Always more than the document count once anything cites a law twice, and it
            # is the number a reader of the edition itself will see.
            if entry.get("mentions"):
                meta.append(f"{int(entry['mentions']):,} citations")
            note_html = sanitise_editorial_html(
                apply_placeholders(entry.get("note") or "", when=generated_at,
                                   count=len(entries)))
            short = str(entry.get("short") or "").strip()
            # "DSA: Regulation (EU) 2022/2065 …" — the operator's own shorthand, bold, and
            # only here: inside an edition the instrument speaks under its full name.
            label = (
                f'<span class="export-short">{escape(short)}:</span> {escape(entry["title"])}'
                if short else escape(entry["title"])
            )
            rows.append(
                "          <li>\n"
                f'            <a href="{escape(entry["filename"], quote=True)}">{label}</a>\n'
                f'            <p class="export-meta">{escape(" · ".join(meta))}</p>\n'
                + (f'            <p class="export-note">{note_html}</p>\n'
                   if note_html.strip() else "")
                + "          </li>"
            )
        blocks.append(
            '      <section class="export-group">\n'
            f"        <h2>{icon}{escape(jurisdiction)}</h2>\n"
            '        <ul class="export-list">\n'
            + "\n".join(rows) + "\n"
            "        </ul>\n"
            "      </section>"
        )

    # The WordArt is a decoration on the SAME <h1>, not a replacement for it — the
    # heading stays one element with the title as its text, so a screen reader, a search
    # engine and a print stylesheet all still find it.
    heading = (
        f'<span class="wordart rainbow"><span class="text" '
        f'data-text="{escape(title, quote=True)}">{escape(title)}</span></span>'
        if wordart else escape(title)
    )
    page = _INDEX_TEMPLATE
    for token, value in {
        "__PAGE_TITLE__": escape(title, quote=True),
        "__TITLE__": heading,
        "__INTRO__": f'<p class="attribution">{intro_html}</p>' if intro_html.strip() else "",
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

        status = static_export_status(facade.config, stable_id, max_snippets=max_snippets)
        if refresh or not status.get("ready"):
            emit(index, f"{label} ({position}) — gathering the law and everything citing it")
            build_static_export_cache(
                facade, stable_id, max_snippets=max_snippets,
                on_progress=lambda **p: item_progress(
                    int(p.get("done") or 0), int(p.get("total") or 0)),
                cancel_check=cancel_check,
            )
            status = static_export_status(
                facade.config, stable_id, max_snippets=max_snippets)
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
            index_link={"href": "index.html", "title": config["index_title"]})
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
            "documents": int(status.get("documents") or 0),
            "mentions": int(status.get("mentions") or 0),
            "bytes": len(html_bytes),
            "exported": format_export_date(status.get("generated_at") or started),
        })

    check_cancelled()
    emit(len(items), f"writing index.html for {len(entries)} editions")
    index_html = render_index_html(
        entries, title=config["index_title"], intro=config["index_text"],
        generated_at=started, wordart=config["index_wordart"],
    ).encode("utf-8")
    files.append(("index.html", index_html))

    # The folder is written every run — scheduled or manual — and same-named files are
    # replaced outright. It is a mirror of the current corpus, not an archive.
    out_dir.mkdir(parents=True, exist_ok=True)
    for filename, payload in files:
        temporary = out_dir / f".{filename}.tmp"
        temporary.write_bytes(payload)
        temporary.replace(out_dir / filename)

    result = {
        "documents": len(entries),
        "output_dir": str(out_dir),
        "files": [
            {k: entry[k] for k in
             ("filename", "title", "jurisdiction", "documents", "mentions", "bytes",
              "exported")}
            for entry in entries
        ],
        "bytes": sum(len(payload) for _name, payload in files),
        "finished_at": _now().isoformat(timespec="seconds"),
        "started_at": started.isoformat(timespec="seconds"),
        "refreshed": bool(refresh),
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
