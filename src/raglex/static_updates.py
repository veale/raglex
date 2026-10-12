"""Chronological, static citing-document updates for one published law.

The ordinary static edition is organised around the law's provisions. This companion is
organised around time: it lets a reader follow new cases by default, switch to statutes
or other material, and download the current view as CSV. The sibling Atom feed is cases
only and follows RagLex's immutable first-seen clock.
"""

from __future__ import annotations

import hashlib
import html
import json
from datetime import datetime, timezone
from pathlib import Path


_KIND_LABELS = {
    "cases": "Cases",
    "legislation": "Statutes and other legislation",
    "administrative": "Administrative decisions",
    "guidance": "Guidance and reports",
    "preparatory": "Preparatory documents",
    "explanatory": "Explanatory notes",
    "other": "Other documents",
}


def _script_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).replace(
        "</", "<\\/"
    ).replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def _iso(value: str | None, fallback: str) -> str:
    raw = str(value or fallback or "").strip()
    if len(raw) == 10:
        return raw + "T00:00:00Z"
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except ValueError:
        return fallback


def _plain_snippet(snippet: dict) -> str:
    return " ".join(str(snippet.get("text") or "").split())


def _updates_rows(data: dict) -> list[dict]:
    rows = []
    for group in data.get("groups") or []:
        links = list(group.get("links") or [])
        snippets = []
        for snippet in group.get("snippets") or []:
            text = str(snippet.get("text") or "")
            if not text:
                continue
            snippets.append({
                "text": text,
                "mark": snippet.get("mark"),
                "where": snippet.get("where"),
                "raw": snippet.get("raw"),
                "url": snippet.get("passage_url"),
            })
        rows.append({
            "id": group.get("id"),
            "title": group.get("title") or group.get("cite") or group.get("id"),
            "cite": group.get("cite") or group.get("title") or group.get("id"),
            "document_date": group.get("date"),
            "added": group.get("added"),
            "kind": group.get("kind") or "other",
            "kind_label": _KIND_LABELS.get(group.get("kind"), group.get("kind") or "Other"),
            "jurisdiction": group.get("jurisdiction") or "Not recorded",
            "court": group.get("court"),
            "source": group.get("source_label"),
            "url": links[0].get("url") if links else None,
            "snippets": snippets,
        })
    return rows


_STYLE = """
html { color: #000; background: #fff; font-family: "Times New Roman", Times, serif; }
body { max-width: 58rem; margin: 1.25in auto; padding: 0 .75in 2in; font-size: 12pt; line-height: 1.25; }
h1 { margin: 0 0 6pt; font-size: 22pt; font-weight: bold; }
h2 { margin: 22pt 0 4pt; font-size: 15pt; text-decoration: underline; }
p { margin: 6pt 0; }
a { color: #0000ee; text-decoration: underline; }
.rule { border-top: 1px solid #000; margin: 12pt 0; }
.controls { border: 1px solid #000; padding: 10pt; margin: 16pt 0; }
.controls label { display: inline-block; margin: 0 14pt 8pt 0; font-weight: bold; }
select, button { font: 12pt "Times New Roman", Times, serif; color: #000; background: #fff; border: 1px solid #000; padding: 3pt 7pt; }
button { cursor: pointer; text-decoration: underline; }
.count { font-weight: bold; }
article { border-top: 1px solid #000; padding: 12pt 0 8pt; }
article h2 { margin-top: 0; }
.details { margin: 4pt 0 9pt; }
.details strong { text-decoration: underline; }
blockquote { margin: 8pt 18pt; padding-left: 10pt; border-left: 2px solid #000; }
mark { color: #000; background: #fff200; padding: 0; }
.where { font-style: italic; }
.empty { border: 1px solid #000; padding: 10pt; }
@media (max-width: 700px) { body { margin: 0; padding: 18pt; } .controls label { display: block; } }
@media print { .controls { display: none; } body { margin: 0; padding: 0; } a { color: #000; } }
"""


_SCRIPT = r"""
(() => {
  const rows = JSON.parse(document.getElementById("updates-data").textContent);
  const $ = (id) => document.getElementById(id);
  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) =>
    ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[c]);
  const fmt = (value) => {
    if (!value) return "Not recorded";
    const d = new Date(value.length === 10 ? value + "T00:00:00Z" : value);
    return Number.isNaN(d.valueOf()) ? value : d.toLocaleDateString("en-GB", {
      day: "numeric", month: "long", year: "numeric"
    });
  };
  function marked(snippet) {
    const text = String(snippet.text || "");
    const mark = snippet.mark;
    let body = esc(text);
    if (Array.isArray(mark) && mark.length === 2 && mark[0] >= 0 && mark[1] > mark[0]) {
      body = esc(text.slice(0, mark[0])) + "<mark>" + esc(text.slice(mark[0], mark[1]))
        + "</mark>" + esc(text.slice(mark[1]));
    }
    const where = snippet.where ? `<p class="where">Location: ${esc(snippet.where)}</p>` : "";
    const linked = snippet.url ? `<a href="${esc(snippet.url)}" target="_blank" rel="noopener noreferrer">${body}</a>` : body;
    return `<blockquote>${linked}${where}</blockquote>`;
  }
  function selected() {
    const kind = $("kind").value;
    const key = $("sort").value;
    const direction = $("direction").value === "oldest" ? 1 : -1;
    return rows.filter((row) => kind === "all" || row.kind === kind).sort((a, b) => {
      const av = key === "added" ? a.added : a.document_date;
      const bv = key === "added" ? b.added : b.document_date;
      if (!av && !bv) return String(a.cite).localeCompare(String(b.cite));
      if (!av) return 1;
      if (!bv) return -1;
      return direction * String(av).localeCompare(String(bv)) || String(a.cite).localeCompare(String(b.cite));
    });
  }
  function render() {
    const visible = selected();
    $("count").textContent = `${visible.length.toLocaleString("en-GB")} document${visible.length === 1 ? "" : "s"}`;
    $("results").innerHTML = visible.length ? visible.map((row) => {
      const heading = row.url
        ? `<a href="${esc(row.url)}" target="_blank" rel="noopener noreferrer">${esc(row.cite)}</a>`
        : esc(row.cite);
      const details = [
        `<strong>Jurisdiction:</strong> ${esc(row.jurisdiction)}`,
        `<strong>Document type:</strong> ${esc(row.kind_label)}`,
        row.court ? `<strong>Court or body:</strong> ${esc(row.court)}` : "",
        row.source ? `<strong>Source:</strong> ${esc(row.source)}` : "",
        `<strong>Document date:</strong> ${esc(fmt(row.document_date))}`,
        `<strong>Added to RagLex:</strong> ${esc(fmt(row.added))}`
      ].filter(Boolean).join(" · ");
      const snippets = row.snippets.length
        ? row.snippets.map(marked).join("")
        : "<p>RagLex records the citation, but no citation excerpt is available.</p>";
      return `<article id="doc-${esc(row.id)}"><h2>${heading}</h2><p class="details">${details}</p>${snippets}</article>`;
    }).join("") : '<p class="empty">No documents match this selection.</p>';
  }
  function csvCell(value) { return `"${String(value ?? "").replace(/"/g, '""')}"`; }
  $("csv").addEventListener("click", () => {
    const header = ["Added to RagLex", "Document date", "Jurisdiction", "Document type",
      "Court or body", "Citation", "Title", "RagLex identifier", "Source URL", "Citation excerpts"];
    const body = selected().map((row) => [row.added, row.document_date, row.jurisdiction,
      row.kind_label, row.court, row.cite, row.title, row.id, row.url,
      row.snippets.map((s) => String(s.text || "").replace(/\s+/g, " ").trim()).join(" | ")]);
    const csv = "\ufeff" + [header, ...body].map((line) => line.map(csvCell).join(",")).join("\r\n");
    const url = URL.createObjectURL(new Blob([csv], {type: "text/csv;charset=utf-8"}));
    const a = document.createElement("a"); a.href = url;
    a.download = document.body.dataset.slug + "-" + $("kind").value + ".csv";
    document.body.appendChild(a); a.click(); a.remove(); URL.revokeObjectURL(url);
  });
  for (const id of ["kind", "sort", "direction"]) $(id).addEventListener("change", render);
  render();
})();
"""


def render_updates_html(data: dict, *, slug: str, atom_filename: str,
                        canonical: str | None = None) -> bytes:
    law = data.get("law") or {}
    title = str(law.get("short_title") or law.get("title") or law.get("stable_id"))
    rows = _updates_rows(data)
    kinds = list(dict.fromkeys(row["kind"] for row in rows))
    ordered_kinds = [kind for kind in _KIND_LABELS if kind in kinds]
    ordered_kinds.extend(kind for kind in kinds if kind not in ordered_kinds)
    options = ['<option value="all">All citing documents</option>'] + [
        f'<option value="{html.escape(kind, quote=True)}"'
        + (' selected' if kind == "cases" else '')
        + f'>{html.escape(_KIND_LABELS.get(kind, kind))}</option>'
        for kind in ordered_kinds
    ]
    canonical_link = (f'<link rel="canonical" href="{html.escape(canonical, quote=True)}">'
                      if canonical else "")
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Citing updates for {html.escape(title)}</title>{canonical_link}
<link rel="alternate" type="application/atom+xml" title="New cases citing {html.escape(title)}" href="{html.escape(atom_filename, quote=True)}">
<style>{_STYLE}</style></head>
<body data-slug="{html.escape(slug, quote=True)}">
<h1>Citing updates for {html.escape(title)}</h1>
<p><strong>Law:</strong> {html.escape(str(law.get('title') or title))}</p>
<p><strong>Jurisdiction:</strong> {html.escape(str(law.get('jurisdiction') or 'Not recorded'))}</p>
<p>This page lists documents in RagLex that cite this law. It opens with cases and can also show statutes, administrative decisions, guidance and other materials. Citation wording is highlighted in yellow.</p>
<p><a href="{html.escape(atom_filename, quote=True)}">Subscribe to the Atom feed of newly added citing cases</a>.</p>
<div class="controls">
<label>Documents <select id="kind">{''.join(options)}</select></label>
<label>Sort date <select id="sort"><option value="added">Date added to RagLex</option><option value="document_date">Date of judgment or document</option></select></label>
<label>Order <select id="direction"><option value="newest">Newest first</option><option value="oldest">Oldest first</option></select></label>
<button id="csv" type="button">Export current view as CSV</button>
<p class="count" id="count"></p>
</div>
<div id="results"></div>
<div class="rule"></div>
<p>Generated {html.escape(str(data.get('generated_at') or ''))}. For older holdings, “added to RagLex” uses the earliest catalogue timestamp retained by this installation.</p>
<script id="updates-data" type="application/json">{_script_json(rows)}</script>
<script>{_SCRIPT}</script></body></html>"""
    return page.encode("utf-8")


def render_updates_atom(data: dict, *, page_url: str | None, feed_url: str | None) -> bytes:
    law = data.get("law") or {}
    title = str(law.get("short_title") or law.get("title") or law.get("stable_id"))
    generated = _iso(data.get("generated_at"), datetime.now(timezone.utc).isoformat())
    cases = sorted(
        (row for row in _updates_rows(data) if row["kind"] == "cases"),
        key=lambda row: (row.get("added") or "", row.get("id") or ""), reverse=True,
    )
    updated = _iso(cases[0].get("added"), generated) if cases else generated
    feed_id = feed_url or f"urn:raglex:citing-cases:{law.get('stable_id') or title}"
    parts = ['<?xml version="1.0" encoding="utf-8"?>',
             '<feed xmlns="http://www.w3.org/2005/Atom">',
             f'<id>{html.escape(feed_id)}</id>',
             f'<title>New cases citing {html.escape(title)}</title>',
             f'<updated>{html.escape(updated)}</updated>']
    if feed_url:
        parts.append(f'<link rel="self" type="application/atom+xml" href="{html.escape(feed_url, quote=True)}"/>')
    if page_url:
        parts.append(f'<link rel="alternate" type="text/html" href="{html.escape(page_url, quote=True)}"/>')
    for row in cases:
        url = row.get("url") or page_url
        digest = hashlib.sha256(str(row.get("id") or row.get("cite")).encode()).hexdigest()[:20]
        entry_id = f"urn:raglex:citing-case:{digest}"
        details = [row.get("jurisdiction"), row.get("court"), row.get("source")]
        prose = " · ".join(str(bit) for bit in details if bit)
        excerpts = " ".join(_plain_snippet(s) for s in row.get("snippets") or [])
        content = "<p>" + html.escape(prose) + "</p>"
        if excerpts:
            content += "<blockquote>" + html.escape(excerpts) + "</blockquote>"
        parts.extend(["<entry>", f"<id>{entry_id}</id>",
                      f"<title>{html.escape(str(row.get('cite') or row.get('title')))}</title>",
                      f"<updated>{html.escape(_iso(row.get('added'), generated))}</updated>"])
        if row.get("document_date"):
            parts.append(f"<published>{html.escape(_iso(row['document_date'], generated))}</published>")
        if url:
            parts.append(f'<link href="{html.escape(str(url), quote=True)}"/>')
        parts.append(f'<content type="html">{html.escape(content)}</content>')
        parts.append("</entry>")
    parts.append("</feed>")
    return ("\n".join(parts) + "\n").encode("utf-8")


def render_cached_updates(status: dict, *, slug: str,
                          base_url: str | None = None) -> tuple[bytes, bytes]:
    data = json.loads(Path(status["_path"]).read_text(encoding="utf-8"))
    html_name, atom_name = f"{slug}-updates.html", f"{slug}-updates.atom"
    page_url = f"{base_url}/{html_name}" if base_url else None
    feed_url = f"{base_url}/{atom_name}" if base_url else None
    return (
        render_updates_html(data, slug=f"{slug}-updates", atom_filename=atom_name,
                            canonical=page_url),
        render_updates_atom(data, page_url=page_url, feed_url=feed_url),
    )
