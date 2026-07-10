import { createContext, Fragment, useContext, useEffect, useRef, useState } from "react";
import { api, Hit, Setting } from "./api";

// --- Peek (margin-note / bottom-sheet) overlay -----------------------------
// You should never have to leave the page to glance at a cited authority, a
// backlink, or an attach-commentary form: they pop up in a side panel (desktop)
// or a dismissible bottom sheet (mobile), stackable, with "open full" to navigate.
type Peek = { kind: "doc"; id: string; anchor?: string; raw?: string } | { kind: "augment"; docId: string; anchor?: string };
// turn a recognised citation into a peek target (resolved doc, or the candidate/raw
// for a not-yet-held reference the peek can offer to fetch)
function citePeek(c: any): Peek {
  return { kind: "doc", id: c.resolved_id || c.candidate_id || c.raw, anchor: c.pinpoint, raw: c.raw };
}
// One peek at a time — a new link replaces the previous one (no stacking).
const PeekCtx = createContext<{ current: Peek | null; push: (p: Peek) => void; close: () => void } | null>(null);
export function usePeek() {
  return useContext(PeekCtx) ?? { current: null, push: () => {}, close: () => {} };
}
export function PeekProvider({ children }: { children: any }) {
  const [current, setCurrent] = useState<Peek | null>(null);
  useEffect(() => { document.body.classList.toggle("has-peek", !!current); }, [current]);
  return <PeekCtx.Provider value={{ current, push: setCurrent, close: () => setCurrent(null) }}>{children}</PeekCtx.Provider>;
}

const REL_TYPES = [
  "analyses", "criticises", "summarises", "annotates", "follows", "distinguishes",
  "overrules", "applies", "considers", "interprets", "mentions",
];
const DOC_TYPES = ["judgment", "decision", "opinion", "legislation", "guidance", "commentary", "article", "note", "annotation"];
// treatments a citation edge can carry — for the inline reclassify control
const TREATMENTS = ["mentions", "follows", "distinguishes", "overrules", "applies", "considers", "interprets", "implements"];

// Start a background job and poll it to completion, reporting progress as it goes.
async function runJob(kind: "radiate" | "harvest-all", body: Record<string, unknown>,
                      onProgress: (p: any) => void): Promise<any> {
  const { job_id } = await api.startJob(kind, body);
  for (;;) {
    await new Promise((r) => setTimeout(r, 1200));
    const s = await api.jobStatus(job_id);
    if (s.progress) onProgress(s.progress);
    if (s.status === "done") return s.result;
    if (s.status === "error") throw new Error(s.result?.error || "job failed");
    if (s.status === "unknown") throw new Error("job lost");
  }
}

function useAsync<T>(fn: () => Promise<T>, deps: unknown[]): [T | null, string, () => void, boolean] {
  const [data, setData] = useState<T | null>(null);
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);
  useEffect(() => {
    let live = true;
    setLoading(true); setErr("");
    fn().then((d) => live && setData(d)).catch((e) => live && setErr(String(e)))
      .finally(() => live && setLoading(false));
    return () => { live = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick]);
  return [data, err, () => setTick((t) => t + 1), loading];
}

// --- Search ----------------------------------------------------------------
export function SearchView({ open }: { open: (id: string) => void }) {
  const [q, setQ] = useState("right to erasure of personal data");
  const [filters, setFilters] = useState<{ source?: string; doc_type?: string; year_from?: string; tag?: string }>({});
  const [hits, setHits] = useState<Hit[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [stats] = useAsync(() => api.stats(), []);

  async function run() {
    setBusy(true); setErr("");
    try {
      const f: Record<string, string> = {};
      Object.entries(filters).forEach(([k, v]) => v && (f[k] = v));
      setHits(await api.search(q, 10, f));
    } catch (e: any) { setErr(String(e)); } finally { setBusy(false); }
  }
  const sources = Object.keys(stats?.by_source ?? {});
  const tags = Object.keys(stats?.by_tag ?? {});

  return (
    <div>
      <div className="panel">
        <div className="row">
          <input value={q} autoFocus onChange={(e) => setQ(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && run()} placeholder="hybrid keyword + semantic search" />
          <button className="primary" style={{ flex: "0 0 auto" }} onClick={run} disabled={busy}>
            {busy ? "Searching…" : "Search"}
          </button>
        </div>
        <div className="row" style={{ marginTop: 8 }}>
          <select value={filters.source ?? ""} onChange={(e) => setFilters({ ...filters, source: e.target.value })}>
            <option value="">any source</option>
            {sources.map((s) => <option key={s}>{s}</option>)}
          </select>
          <select value={filters.doc_type ?? ""} onChange={(e) => setFilters({ ...filters, doc_type: e.target.value })}>
            <option value="">any type</option>
            {DOC_TYPES.map((s) => <option key={s}>{s}</option>)}
          </select>
          <select value={filters.tag ?? ""} onChange={(e) => setFilters({ ...filters, tag: e.target.value })}>
            <option value="">any tag</option>
            {tags.map((s) => <option key={s}>{s}</option>)}
          </select>
          <input style={{ maxWidth: 110 }} value={filters.year_from ?? ""} placeholder="year from"
            onChange={(e) => setFilters({ ...filters, year_from: e.target.value })} />
        </div>
        {err && <p className="err">{err}</p>}
      </div>
      {hits !== null && (
        <div className="panel">
          <p className="muted">{hits.length} result{hits.length === 1 ? "" : "s"} · keyword + semantic, fused (RRF), with graph neighbours</p>
          {hits.length === 0 && <p className="muted">No matches. Try fewer filters, or embed first (Dashboard → Embed pending).</p>}
          {hits.map((h, i) => (
            <div className="hit" key={i}>
              <div>
                <a onClick={() => open(h.doc_id)}>{h.ecli || h.title || h.doc_id}</a>{" "}
                <span className="muted">· {h.source}/{h.court} · {h.structural_unit} · score {h.score.toFixed(4)}</span>
              </div>
              <div className="snippet">{h.chunk_text.slice(0, 300)}</div>
              {h.neighbours.length > 0 && (
                <div className="nbr">graph: {h.neighbours.slice(0, 3).map((n, j) =>
                  <span key={j}>{n.direction === "out" ? "→" : "←"} {n.relationship_type} <a onClick={() => open(n.id)}>{n.id}</a>; </span>)}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// --- Corpus browse ---------------------------------------------------------
const PAGE = 100;
export function CorpusView({ open, initialFilter }: { open: (id: string) => void; initialFilter?: Record<string, string> }) {
  const [filters, setFilters] = useState<{ source?: string; doc_type?: string; tag?: string; query?: string; court?: string; id_prefix?: string }>(initialFilter ?? {});
  // when the Corpus Map deep-links here ("see this list"), adopt its filter
  useEffect(() => { if (initialFilter && Object.keys(initialFilter).length) setFilters(initialFilter); }, [JSON.stringify(initialFilter)]);
  const [page, setPage] = useState(0);
  const [stats] = useAsync(() => api.stats(), []);
  const filt = () => { const f: Record<string, string> = {}; Object.entries(filters).forEach(([k, v]) => v && (f[k] = v)); return f; };
  const [total] = useAsync(() => api.countDocuments(filt()).then((r) => r.total), [JSON.stringify(filters)]);
  const [docs, err, reload, loading] = useAsync(() => {
    return api.listDocuments({ ...filt(), limit: String(PAGE), offset: String(page * PAGE) });
  }, [JSON.stringify(filters), page]);
  // reset to page 0 whenever the filters change
  useEffect(() => { setPage(0); }, [JSON.stringify(filters)]);
  const [sel, setSel] = useState<Set<string>>(new Set());
  const [coll, setColl] = useState("");
  const [msg, setMsg] = useState("");
  const sources = Object.keys(stats?.by_source ?? {});
  const tags = Object.keys(stats?.by_tag ?? {});
  const toggle = (id: string) => setSel((s) => { const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n; });

  async function addToCollection() {
    if (!coll || sel.size === 0) return;
    const r = await api.tagMany([...sel], coll);
    setMsg(`Added ${r.written} to “${coll}”.`); setSel(new Set()); reload();
  }
  return (
    <div>
      <div className="panel">
        <div className="row">
          <input placeholder="filter by title / id" value={filters.query ?? ""}
            onChange={(e) => setFilters({ ...filters, query: e.target.value })} />
          <select value={filters.source ?? ""} onChange={(e) => setFilters({ ...filters, source: e.target.value })}>
            <option value="">any source</option>{sources.map((s) => <option key={s}>{s}</option>)}
          </select>
          <select value={filters.doc_type ?? ""} onChange={(e) => setFilters({ ...filters, doc_type: e.target.value })}>
            <option value="">any type</option>{DOC_TYPES.map((s) => <option key={s}>{s}</option>)}
          </select>
          <select value={filters.tag ?? ""} onChange={(e) => setFilters({ ...filters, tag: e.target.value })}>
            <option value="">any tag / collection</option>{tags.map((s) => <option key={s}>{s}</option>)}
          </select>
          <button onClick={reload} style={{ flex: "0 0 auto" }}>↻</button>
        </div>
        {(filters.court || filters.id_prefix) && (
          <div className="row" style={{ marginTop: 8, gap: 8 }}>
            <span className="filter-chip">
              {filters.court ? `court: ${filters.court}` : `id: ${filters.id_prefix}*`}
              <a onClick={() => setFilters({ ...filters, court: undefined, id_prefix: undefined })} title="clear this filter"> ✕</a>
            </span>
          </div>
        )}
      </div>
      <div className="panel">
        {err && <p className="err">{err}</p>}
        <div className="row" style={{ justifyContent: "space-between" }}>
          <p className="muted" style={{ margin: 0 }}>
            {total != null
              ? `${total.toLocaleString()} documents${total > PAGE ? ` · showing ${page * PAGE + 1}–${Math.min((page + 1) * PAGE, total)}` : ""}`
              : `${docs?.length ?? 0} documents`}
            {sel.size > 0 ? ` · ${sel.size} selected` : ""}
            {total != null && total > PAGE && <>
              {" "}<button disabled={page === 0} onClick={() => setPage((p) => Math.max(0, p - 1))}>‹ prev</button>
              {" "}<button disabled={(page + 1) * PAGE >= total} onClick={() => setPage((p) => p + 1)}>next ›</button>
            </>}
          </p>
          <div className="row" style={{ flex: "0 0 auto" }}>
            <input value={coll} onChange={(e) => setColl(e.target.value)} placeholder="collection / tag name" style={{ maxWidth: 180 }} />
            <button className="primary" disabled={sel.size === 0 || !coll} style={{ flex: "0 0 auto" }} onClick={addToCollection}>+ Add {sel.size || ""} to collection</button>
          </div>
        </div>
        {msg && <p className="ok">{msg}</p>}
        {loading && !docs && <p className="muted loading-pulse">⏳ Loading documents…</p>}
        <table><thead><tr><th></th><th>id / ECLI</th><th>title</th><th>court</th><th>type</th><th>date</th></tr></thead><tbody>
          {(docs ?? []).map((d) => (
            <tr key={d.stable_id}>
              <td><input type="checkbox" checked={sel.has(d.stable_id)} onChange={() => toggle(d.stable_id)} /></td>
              <td><a onClick={() => open(d.stable_id)}>{d.ecli || d.stable_id}</a></td>
              <td>{d.title || ""}</td><td>{d.court || ""}</td>
              <td>{d.doc_type}{d.added_by === "user" ? " ·user" : ""}</td><td className="muted">{d.decision_date || ""}</td>
            </tr>
          ))}
        </tbody></table>
      </div>
    </div>
  );
}

function segId(label: string): string {
  return "seg-" + (label || "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
}

// Link a paragraph cross-reference to an in-page jump ONLY when it is an explicit
// *self*-reference — "para [43] above", "at [21] above/below". Bare "[57]" or
// "Delo … at 131" is a pinpoint into the *cited* case, not this judgment, so it's
// left as plain text (linking it would be wrong/confusing). Also requires that the
// number names a real paragraph here (so citation years like "[2023]" never match).
function renderRun(text: string, key: string, paraSet?: Set<string>, onPara?: (n: string) => void) {
  if (!onPara || !paraSet || paraSet.size === 0) return text;
  const out: any[] = [];
  // [N] (optionally a range/list) immediately followed by above|below
  const re = /\[(\d{1,3})\](?:\s*[-–]\s*\[\d{1,3}\]|\s*,?\s*(?:and|to)\s*\[\d{1,3}\])?\s+(above|below)\b/gi;
  let last = 0, m: RegExpExecArray | null, k = 0;
  while ((m = re.exec(text))) {
    const n = m[1];
    if (!paraSet.has(n)) continue;
    if (m.index > last) out.push(text.slice(last, m.index));
    out.push(<a key={`${key}-p${k++}`} className="pararef" title={`go to paragraph ${n} (this judgment)`}
      onClick={() => onPara(n)}>{m[0]}</a>);
    last = re.lastIndex;
  }
  if (last < text.length) out.push(text.slice(last));
  return out.length ? out : text;
}

// Render a slice of text with its recognised citations wrapped as live links to the
// cited authority (JADE-style inline links) — resolved → peek the authority (+ pinpoint),
// pending → marked as a citation we've parsed but don't yet hold. Paragraph refs jump.
function renderCited(text: string, segStart: number, segEnd: number, cites: any[],
                     onCite: (c: any) => void, paraSet?: Set<string>, onPara?: (n: string) => void) {
  const within = cites
    .filter((c) => c.char_start >= segStart && c.char_end <= segEnd)
    .sort((a, b) => a.char_start - b.char_start);
  const nodes: any[] = [];
  let cursor = segStart;
  within.forEach((c, k) => {
    if (c.char_start < cursor) return; // skip overlaps
    if (c.char_start > cursor) nodes.push(renderRun(text.slice(cursor, c.char_start), `g${k}`, paraSet, onPara));
    const label = text.slice(c.char_start, c.char_end);
    const state = c.state || (c.resolved_id ? "resolved" : c.candidate_id ? "pending" : "maybe");
    // heuristic "carried-forward" provision (e.g. a bare "section 5" linked to the
    // last-named statute) — flag it as an uncertain guess for the reader.
    const guess = c.method === "carry_forward" || c.extracted_via === "inferred";
    const title = guess ? `inferred: “${label}” taken to mean ${c.pinpoint || ""} of ${c.candidate_id || c.resolved_id} — uncertain, click to check`
      : state === "resolved" ? `${c.entity_kind}${c.pinpoint ? " · " + c.pinpoint : ""} → ${c.resolved_id}`
      : state === "pending" ? `${c.entity_kind}: ${c.candidate_id} — not in the corpus yet (click to fetch)`
      : `${c.entity_kind} reference — not resolvable automatically (click to search)`;
    nodes.push(<a key={k} className={`cite cite-${state}${guess ? " cite-inferred" : ""}`} title={title} onClick={() => onCite(c)}>{label}</a>);
    cursor = c.char_end;
  });
  if (cursor < segEnd) nodes.push(renderRun(text.slice(cursor, segEnd), "tail", paraSet, onPara));
  return nodes.length ? nodes : text.slice(segStart, segEnd);
}

// the set of paragraph numbers in this document (from segment labels like "43.")
function paraNumbers(segs: any[]): Set<string> {
  const s = new Set<string>();
  for (const seg of segs || []) { const m = /^(\d{1,4})\b/.exec((seg.label || "").trim()); if (m) s.add(m[1]); }
  return s;
}

function scrollToSeg(id: string) {
  const el = document.getElementById(id);
  if (el) { el.scrollIntoView({ behavior: "smooth", block: "center" }); el.classList.add("seg-flash"); setTimeout(() => el.classList.remove("seg-flash"), 2000); }
}

// The side panel itself — renders the top of the peek stack (with back/close), as
// a margin column on desktop and a bottom sheet on mobile (CSS).
export function PeekPanel({ open }: { open: (id: string, a?: string) => void }) {
  const { current, push, close } = usePeek();
  if (!current) return null;
  return (
    <aside className="peek" role="dialog" aria-label="preview">
      <div className="peek-head">
        <span className="muted" style={{ flex: 1, fontSize: 12 }}>{current.kind === "augment" ? "Attach commentary" : "Preview"}</span>
        <button onClick={close} title="dismiss">✕</button>
      </div>
      <div className="peek-body">
        {current.kind === "doc"
          ? <DocPeek id={current.id} anchor={current.anchor} raw={current.raw} onCite={(c) => push(citePeek(c))} openFull={(id, a) => { close(); open(id, a); }} />
          : <AugmentPanel docId={current.docId} onDone={close} pinAnchor={current.anchor} clearPin={() => {}} />}
      </div>
    </aside>
  );
}

// match an anchor ("para 80", "Article 17", "s. 14") to a segment; paragraph
// pinpoints match by number, legislation pinpoints by normalised label.
function matchSegIndex(segs: any[], anchor?: string): number {
  if (!anchor || !segs?.length) return -1;
  const para = /para\.?\s*(\d+)|^\[?(\d+)\]?$/i.exec(anchor.trim());
  const num = para && (para[1] || para[2]);
  if (num) {
    const i = segs.findIndex((s) => new RegExp(`^\\[?${num}[.\\]]?\\b`).test((s.label || "").trim()));
    if (i >= 0) return i;
  }
  const norm = (x: string) => (x || "").toLowerCase().replace(/[^a-z0-9]+/g, "");
  const a = norm(anchor);
  let i = segs.findIndex((s) => norm(s.label) === a);
  if (i < 0 && a.length > 2) i = segs.findIndex((s) => norm(s.label).includes(a));
  return i;
}

// A compact, self-contained preview of a cited authority — its name, how often it's
// cited, and either the pinpointed provision or its opening — without leaving the
// page. If it isn't in the corpus yet, it offers to fetch it.
function DocPeek({ id, anchor, raw, onCite, openFull }:
  { id: string; anchor?: string; raw?: string; onCite: (c: any) => void; openFull: (id: string, a?: string) => void }) {
  const [doc, , reload] = useAsync(() => api.document(id), [id]);
  const [body] = useAsync(() => api.documentBody(id), [id]);
  const segs = (body?.segments || []) as any[];
  // jump to the pinpointed paragraph/section once the full text has rendered
  useEffect(() => {
    if (!body?.text) return;
    const idx = matchSegIndex(segs, anchor);
    const el = idx >= 0 ? document.getElementById("peek-seg-" + idx) : null;
    if (el) setTimeout(() => { el.scrollIntoView({ behavior: "smooth", block: "start" }); el.classList.add("seg-flash"); setTimeout(() => el.classList.remove("seg-flash"), 2000); }, 60);
  }, [body, anchor]);
  if (doc?.error) return <FetchPrompt refId={id} raw={raw} onDone={reload} />;
  const d = doc?.document;
  const cites = body?.citations || [];
  return (
    <div>
      <div className="peek-doc-head">
        <b>{d?.title || id}</b>
        <div className="muted" style={{ fontSize: 12 }}>{d?.court}{d?.decision_date ? " · " + String(d.decision_date).slice(0, 10) : ""}
          {doc?.cited_by_count ? ` · cited by ${doc.cited_by_count}` : ""}{anchor ? ` · ${anchor}` : ""}</div>
        <button style={{ marginTop: 4 }} onClick={() => openFull(id, anchor)}>open full ↗</button>
      </div>
      {!body?.text && doc && <p className="muted">No text yet (metadata only).</p>}
      {body?.text && segs.length > 0 && (
        <div className="reader">
          {segs.map((s, i) => (
            <div className={`seg lvl${Math.min(s.level, 2)} kind-${s.kind}`} key={i} id={"peek-seg-" + i}>
              <span className="seg-label">{s.label}</span>
              <span className="seg-body">{renderCited(body.text, s.char_start, s.char_end, cites, onCite)}</span>
            </div>
          ))}
        </div>
      )}
      {body?.text && !segs.length && <div className="reader"><div className="seg-body">{renderCited(body.text, 0, body.text.length, cites, onCite)}</div></div>}
    </div>
  );
}

// Shown in the peek when a cited authority isn't in the corpus — try a targeted
// fetch (routable ids), and offer a URL paste as a fallback (e.g. a report citation
// with no neutral citation — paste the BAILII / Find Case Law link).
function FetchPrompt({ refId, raw, onDone }: { refId: string; raw?: string; onDone: () => void }) {
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);
  const [url, setUrl] = useState("");
  async function fetchIt() {
    setBusy(true); setMsg("fetching…");
    try {
      const r = await api.harvestReference(refId);
      if (r.resolved || r.stored) { setMsg("✓ fetched — opening…"); setTimeout(onDone, 600); }
      else setMsg(r.error ? "couldn't auto-fetch — paste a URL below" : "not found at source — paste a URL below");
    } catch (e: any) { setMsg("error: " + e); } finally { setBusy(false); }
  }
  async function fetchUrl() {
    if (!url) return;
    setBusy(true); setMsg("fetching from URL…");
    try { await api.resolveReferenceUrl(raw || refId, url); setMsg("✓ added — opening…"); setTimeout(onDone, 600); }
    catch (e: any) { setMsg("error: " + e); } finally { setBusy(false); }
  }
  return (
    <div>
      <p><b>Not in the corpus yet</b></p>
      <p className="muted" style={{ fontSize: 13, wordBreak: "break-word" }}>{raw || refId}</p>
      <button className="primary" disabled={busy} onClick={fetchIt}>⤓ Try to fetch this</button>
      <div className="row" style={{ marginTop: 8 }}>
        <input value={url} onChange={(e) => setUrl(e.target.value)} placeholder="…or paste a URL (BAILII / Find Case Law) to add it" />
        <button disabled={busy || !url} style={{ flex: "0 0 auto" }} onClick={fetchUrl}>add</button>
      </div>
      {msg && <p className={msg.startsWith("error") ? "err" : "ok"} style={{ fontSize: 12 }}>{msg}</p>}
    </div>
  );
}

// --- Structured reader (legislation hierarchy / judgment paragraphs) -------
function Reader({ id, incoming, pinpoint }: { id: string; incoming: any[]; pinpoint?: string | null }) {
  const [body] = useAsync(() => api.documentBody(id), [id]);
  const peek = usePeek();
  const onCite = (c: any) => peek.push(citePeek(c));
  const onPara = (n: string) => scrollToSeg(segId(n + "."));   // jump to paragraph n
  const paraSet = paraNumbers(body?.segments || []);
  // deep-link: when opened at a pinpoint (a paragraph "para 80" or a section
  // "Article 17"), scroll to the matching segment.
  useEffect(() => {
    if (!body || !pinpoint) return;
    const idx = matchSegIndex(body.segments || [], pinpoint);
    if (idx >= 0) setTimeout(() => scrollToSeg(segId(body.segments[idx].label)), 80);
  }, [body, pinpoint]);
  if (!body) return <p className="muted">Loading text…</p>;
  if (!body.text) return <p className="muted">No extracted text (metadata-only, or not yet extracted).</p>;
  const segs = body.segments as { label: string; kind: string; level: number; char_start: number; char_end: number }[];
  const cites = body.citations || [];
  const pinned = (label: string) => (incoming || []).filter((r) => r.dst_anchor === label);
  const content = (!segs || segs.length === 0)
    ? <div className="reader"><div className="seg"><div className="seg-body">{renderCited(body.text, 0, body.text.length, cites, onCite, paraSet, onPara)}</div></div></div>
    : (
      <div className="reader">
        {segs.map((s, i) => (
          <div className={`seg lvl${Math.min(s.level, 2)} kind-${s.kind}`} key={i} id={segId(s.label)}>
            <span className="seg-label">{s.label}
              <a className="pin" title="Attach commentary to this part" onClick={() => peek.push({ kind: "augment", docId: id, anchor: s.label })}> ＋link</a>
            </span>
            <span className="seg-body">{renderCited(body.text, s.char_start, s.char_end, cites, onCite, paraSet, onPara)}</span>
            {pinned(s.label).map((r, j) => (
              <div className="pinned" key={j}>💬 {r.relationship_type}: <a onClick={() => peek.push({ kind: "doc", id: r.src_id })}>{r.src_title || r.src_id}</a>
                {r.src_anchor && <span className="muted"> ({r.src_anchor})</span>}</div>
            ))}
          </div>
        ))}
      </div>
    );
  return <SelectionShorthand>{content}</SelectionShorthand>;
}

// --- Type-ahead that finds a case / act by name as you type ----------------
export function DocAutocomplete({ initial, onPick, placeholder }:
  { initial?: string; onPick: (id: string, title: string) => void; placeholder?: string }) {
  const [q, setQ] = useState(initial || "");
  const [opts, setOpts] = useState<any[]>([]);
  const [hi, setHi] = useState(0);
  useEffect(() => {
    let live = true;
    if (q.trim().length < 2) { setOpts([]); return; }
    const t = setTimeout(async () => {
      try {
        const r = await api.listDocuments({ query: q.trim(), limit: "8" });
        if (live) { setOpts(r); setHi(0); }
      } catch { /* ignore */ }
    }, 160);
    return () => { live = false; clearTimeout(t); };
  }, [q]);
  const pick = (o: any) => o && onPick(o.stable_id, o.title || o.stable_id);
  return (
    <div className="ac">
      <input autoFocus value={q} placeholder={placeholder || "find a case or act by name…"}
        onChange={(e) => setQ(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "ArrowDown") { e.preventDefault(); setHi((h) => Math.min(h + 1, opts.length - 1)); }
          else if (e.key === "ArrowUp") { e.preventDefault(); setHi((h) => Math.max(h - 1, 0)); }
          else if (e.key === "Enter") { e.preventDefault(); pick(opts[hi]); }
        }} />
      {opts.length > 0 && <div className="ac-list">
        {opts.map((o, i) => (
          <div key={o.stable_id} className={`ac-opt${i === hi ? " hi" : ""}`}
            onMouseEnter={() => setHi(i)}
            onMouseDown={(e) => { e.preventDefault(); pick(o); }}>
            <b>{o.title || o.stable_id}</b>
            <span className="muted"> {o.source}/{o.doc_type} · {o.stable_id}</span>
          </div>
        ))}
      </div>}
    </div>
  );
}

// --- Highlight a word → make it a shorthand rule for a case/act ------------
function SelectionShorthand({ children }: { children: any }) {
  const ref = useRef<HTMLDivElement>(null);
  const [sel, setSel] = useState<{ text: string; x: number; y: number } | null>(null);
  const [open, setOpen] = useState(false);
  const [msg, setMsg] = useState("");
  useEffect(() => {
    function onUp(e: MouseEvent) {
      if ((e.target as HTMLElement)?.closest?.(".sel-pop")) return;  // clicking inside our popover
      const s = window.getSelection();
      const text = s?.toString().trim() || "";
      if (!text || text.length > 140 || !ref.current || !s?.anchorNode || !ref.current.contains(s.anchorNode)) {
        setSel(null); setOpen(false); return;
      }
      const rect = s.getRangeAt(0).getBoundingClientRect();
      setSel({ text, x: rect.left + rect.width / 2, y: rect.bottom });
      setOpen(false); setMsg("");
    }
    document.addEventListener("mouseup", onUp);
    return () => document.removeEventListener("mouseup", onUp);
  }, []);
  const create = async (id: string, title: string) => {
    if (!sel) return;
    try { await api.createAlias(sel.text, id); setMsg(`“${sel.text}” → ${title}`); }
    catch (e: any) { setMsg("error: " + e.message); }
    setOpen(false);
    setTimeout(() => { setSel(null); setMsg(""); window.getSelection()?.removeAllRanges(); }, 2200);
  };
  return (
    <div ref={ref} style={{ position: "relative" }}>
      {children}
      {sel && <div className="sel-pop" style={{ position: "fixed", left: sel.x, top: sel.y + 6, transform: "translateX(-50%)" }}>
        {msg ? <span className="ok" style={{ fontSize: 12 }}>✓ rule saved {msg}</span>
          : !open ? (
            <button onClick={() => setOpen(true)}>🔖 Make “{sel.text.length > 28 ? sel.text.slice(0, 28) + "…" : sel.text}” a shorthand</button>
          ) : (
            <div style={{ minWidth: 320 }}>
              <div className="muted" style={{ fontSize: 11, marginBottom: 4 }}>“{sel.text}” always links to:</div>
              <DocAutocomplete initial={sel.text} onPick={create} />
            </div>
          )}
      </div>}
    </div>
  );
}

// --- Document reader + augment ---------------------------------------------
export function DocumentView({ id, open, openGraph, pinpoint }: { id: string; open: (id: string, a?: string) => void; openGraph: (id: string) => void; pinpoint?: string | null }) {
  const [doc, err, reload] = useAsync(() => api.document(id), [id]);
  const [pinAnchor, setPinAnchor] = useState("");
  const [editing, setEditing] = useState(false);
  if (err) return <p className="err">{err}</p>;
  if (!doc) return <p className="muted">Loading…</p>;
  if (doc.error) return <p className="err">{doc.error}: {id}</p>;
  const d = doc.document;
  const versions = doc.versions || [];
  return (
    <div>
      <div className="panel">
        <div className="row" style={{ alignItems: "flex-start" }}>
          <h2 style={{ marginTop: 0, flex: 1 }}>{d.title || d.stable_id}</h2>
          <Snowball seed={d.stable_id} onDone={reload} />
          <button onClick={() => setEditing((e) => !e)} style={{ flex: "0 0 auto" }}>✎ {editing ? "cancel" : "fix metadata"}</button>
          <button onClick={() => openGraph(d.stable_id)} style={{ flex: "0 0 auto" }}>◴ View citation graph</button>
        </div>
        <p className="muted">{d.ecli || d.stable_id} · {d.source}/{d.court} · {d.doc_type}
          {" "}· added_by <b>{d.added_by}</b> · v{d.version} · {d.upstream_status}
          {d.landing_url && <> · <a href={d.landing_url} target="_blank" rel="noreferrer">open original ↗</a></>}</p>
        {editing && <MetadataEditor d={d} onDone={() => { setEditing(false); reload(); }} />}
        <div>{(doc.tags || []).map((t: any, i: number) => (
          <span className="tag" key={i}>{t.tag} · {t.method}
            {t.method === "manual" && <a title="remove tag" style={{ cursor: "pointer", marginLeft: 4 }}
              onClick={async () => { await api.untag(d.stable_id, t.tag); reload(); }}>✗</a>}
          </span>
        ))}</div>
        {versions.length > 0 && <p className="versions">Version history: v{d.version} (latest){versions.map((v: any) =>
          <span key={v.version}> · v{v.version} archived {String(v.archived_at).slice(0, 10)}</span>)}</p>}
      </div>
      {(doc.incoming || []).length > 0 && <CitedByPanel incoming={doc.incoming} count={doc.cited_by_count} inferred={doc.inferred_by_count} />}
      <div className="panel">
        <h3>{d.doc_type === "legislation" ? "Legislation" : "Document"} text
          <span className="muted"> — citations & ¶-refs pop up on the side; ＋link attaches commentary</span></h3>
        <Reader id={d.stable_id} incoming={doc.incoming || []} pinpoint={pinpoint} />
      </div>
      {d.doc_type === "legislation" && <EffectsBanner id={d.stable_id} open={open} />}
      {d.doc_type === "legislation" && <ChangesPanel id={d.stable_id} open={open} />}
      {d.doc_type === "legislation" && <VersionPanel id={d.stable_id} open={open} />}
      <AugmentPanel docId={d.stable_id} onDone={reload} pinAnchor={pinAnchor} clearPin={() => setPinAnchor("")} />
      <div className="grid2">
        <div className="panel">
          <h3>Citations (outgoing) <span className="muted">— reclassify, re-point, or reject (✗) a wrong citation</span></h3>
          {(doc.relations || []).length === 0 && <p className="muted">none</p>}
          <table><tbody>
            {(doc.relations || []).map((r: any) => (
              <RelationRow key={r.relation_id} r={r} open={open} onDone={reload} />
            ))}
          </tbody></table>
          {doc.suppressed_count > 0 && <p className="muted">+ {doc.suppressed_count} suppressed (rejected) citation(s) hidden</p>}
        </div>
        <div className="panel">
          <h3>Attachments</h3>
          {(doc.assets || []).length === 0 && <p className="muted">none</p>}
          {(doc.assets || []).map((a: any, i: number) => (
            <div key={i}>{a.kind}: {a.title} <span className="muted">({a.added_by})</span></div>
          ))}
        </div>
      </div>
    </div>
  );
}

// "Cited by" — JADE's reverse-citation gloss, but treatment-aware: it shows not
// just who cites this authority, but HOW (follows / distinguishes / overrules …).
function CitedByPanel({ incoming, count, inferred }: { incoming: any[]; count?: number; inferred?: number }) {
  const peek = usePeek();
  const open = (id: string, a?: string) => peek.push({ kind: "doc", id, anchor: a });
  const byType: Record<string, number> = {};
  for (const r of incoming) byType[r.relationship_type] = (byType[r.relationship_type] || 0) + 1;
  const order = ["overrules", "distinguishes", "applies", "follows", "considers", "mentions"];
  const colour: Record<string, string> = { overrules: "#ff6b6b", distinguishes: "#ffb454", applies: "#7ee787", follows: "#7ee787" };
  return (
    <div className="panel">
      <h3>Cited by <span className="muted">({count ?? incoming.length}) — later documents that cite this one, and how</span>
        {inferred ? <span className="muted" style={{ fontWeight: 400 }}> {" "}
          <Info t={`Plus ${inferred} inferred link${inferred === 1 ? "" : "s"} — heuristic carry-forwards (a bare "Section 12" pinned to the last-named Act), not citations anyone made. Excluded from the count above so they don't inflate it.`} />
          {" +"}{inferred} inferred</span> : null}</h3>
      <div className="row" style={{ flexWrap: "wrap", gap: 6, marginBottom: 6 }}>
        {order.filter((t) => byType[t]).map((t) => (
          <span key={t} className="tag" style={{ borderColor: colour[t] || "var(--line)", color: colour[t] || "inherit" }}>
            {byType[t]} {t}</span>
        ))}
      </div>
      <table><tbody>
        {incoming.slice(0, 50).map((r, i) => (
          <tr key={i}>
            <td style={{ whiteSpace: "nowrap", color: colour[r.relationship_type] || "var(--muted)" }}>{r.relationship_type}</td>
            <td><a onClick={() => open(r.src_id, r.dst_anchor)}>{r.src_title || r.src_id}</a>
              {r.src_court && <span className="muted"> · {r.src_court}</span>}
              {r.dst_anchor && <span className="muted"> → {r.dst_anchor}</span>}</td>
            <td className="muted" style={{ whiteSpace: "nowrap" }}>{r.src_date ? String(r.src_date).slice(0, 4) : ""}</td>
          </tr>
        ))}
      </tbody></table>
    </div>
  );
}

function AugmentPanel({ docId, onDone, pinAnchor, clearPin }: { docId: string; onDone: () => void; pinAnchor?: string; clearPin?: () => void }) {
  const [action, setAction] = useState("note");
  const [text, setText] = useState("");
  const [rel, setRel] = useState("analyses");
  const [tag, setTag] = useState("");
  const [linkTo, setLinkTo] = useState("");
  const [srcAnchor, setSrcAnchor] = useState("");
  const [dstAnchor, setDstAnchor] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [msg, setMsg] = useState("");

  // A "＋link" click on a law part jumps here, pre-filling the target fragment.
  useEffect(() => {
    if (pinAnchor) { setAction("link"); setDstAnchor(pinAnchor); }
  }, [pinAnchor]);

  async function go() {
    setMsg("…");
    try {
      let r: any;
      if (action === "note") r = await api.importNote({ text, link_to: docId, relationship: "summarises" });
      else if (action === "url") r = await api.importUrl({ url: text, doc_type: "commentary", link_to: docId, relationship: rel });
      else if (action === "file" && file) r = await api.importFile(file, { doc_type: "commentary", link_to: docId, relationship: rel });
      else if (action === "attach" && file) r = await api.attach(docId, file, "exhibit");
      else if (action === "tag") r = await api.tag(docId, tag);
      // link: this doc's fragment (dstAnchor) is analysed by another doc's fragment (srcAnchor)
      else if (action === "link") r = await api.link(linkTo, docId, rel, srcAnchor, dstAnchor);
      setMsg("✓ " + JSON.stringify(r)); onDone(); clearPin?.();
    } catch (e: any) { setMsg("error: " + e); }
  }
  return (
    <div className="panel">
      <h3>Augment this document <span className="muted">— attach secondary material, link a fragment, or tag</span></h3>
      <div className="row">
        <select value={action} onChange={(e) => setAction(e.target.value)} style={{ flex: "0 0 auto", minWidth: 160 }}>
          <option value="note">Write a note</option>
          <option value="url">Import commentary from URL</option>
          <option value="file">Upload commentary file</option>
          <option value="attach">Attach exhibit (file)</option>
          <option value="tag">Add a tag</option>
          <option value="link">Link a fragment (e.g. handbook pages → this article)</option>
        </select>
        {(action === "note") && <textarea value={text} onChange={(e) => setText(e.target.value)} placeholder="your summary / annotation" />}
        {(action === "url") && <input value={text} onChange={(e) => setText(e.target.value)} placeholder="https://…/article.pdf" />}
        {(action === "file" || action === "attach") && <input type="file" onChange={(e) => setFile(e.target.files?.[0] ?? null)} />}
        {(action === "tag") && <input value={tag} onChange={(e) => setTag(e.target.value)} placeholder="tag, e.g. landmark" />}
        {(action === "link") && <input value={linkTo} onChange={(e) => setLinkTo(e.target.value)} placeholder="commentary stable_id (the source doc)" />}
        {(action !== "tag" && action !== "note" && action !== "attach") && (
          <select value={rel} onChange={(e) => setRel(e.target.value)} style={{ flex: "0 0 auto" }}>
            {REL_TYPES.map((r) => <option key={r}>{r}</option>)}
          </select>
        )}
        <button className="primary" style={{ flex: "0 0 auto" }} onClick={go}>Apply</button>
      </div>
      {action === "link" && (
        <div className="row" style={{ marginTop: 6 }}>
          <input value={srcAnchor} onChange={(e) => setSrcAnchor(e.target.value)} placeholder="source fragment, e.g. pp. 45-47 / ch. 3" />
          <input value={dstAnchor} onChange={(e) => setDstAnchor(e.target.value)} placeholder="this doc's part, e.g. Article 17" />
        </div>
      )}
      {msg && <p className={msg.startsWith("error") ? "err" : "ok"} style={{ wordBreak: "break-all" }}>{msg}</p>}
    </div>
  );
}

// Citation-network actions for this document: snowball OUT (what it cites, N hops)
// and discover IN (new judgments that cite it, via the live source).
function Snowball({ seed, onDone }: { seed: string; onDone: () => void }) {
  const [degrees, setDegrees] = useState(2);
  const [busy, setBusy] = useState<"" | "out" | "in">("");
  const [msg, setMsg] = useState("");
  async function out() {
    setBusy("out"); setMsg("radiating…");
    try {
      const r = await runJob("radiate", { seeds: [seed], degrees },
        (p) => setMsg(`${p.stage}: fetched ${p.done}/${p.total}…`));
      const got = (r.degrees || []).reduce((a: number, d: any) => a + d.harvested, 0);
      setMsg(`✓ fetched ${got} doc(s) across ${r.degrees?.length || 0} degree(s)`); onDone();
    } catch (e: any) { setMsg("error: " + e); } finally { setBusy(""); }
  }
  async function inbound() {
    setBusy("in"); setMsg("searching the source for cases citing this…");
    try {
      const r = await api.discoverCiting(seed);
      setMsg(r.error ? "error: " + r.error : `✓ found ${r.count} new case(s) citing this (via ${r.via})`); onDone();
    } catch (e: any) { setMsg("error: " + e); } finally { setBusy(""); }
  }
  return (
    <span style={{ flex: "0 0 auto", display: "inline-flex", alignItems: "center", gap: 4 }}>
      <button disabled={!!busy} onClick={out} title="Fetch what this cites, then what those cite, N degrees out">❅ {busy === "out" ? "snowballing…" : "Snowball"}</button>
      <select value={degrees} onChange={(e) => setDegrees(+e.target.value)} disabled={!!busy} style={{ width: 78 }}>
        {[1, 2, 3].map((n) => <option key={n} value={n}>{n} deg</option>)}
      </select>
      <button disabled={!!busy} onClick={inbound} title="Find NEW judgments that cite this, via Find Case Law / CELLAR">🔎 {busy === "in" ? "finding…" : "Find citing"}</button>
      {msg && <span className={msg.startsWith("error") ? "err" : "muted"} style={{ fontSize: 11 }}>{msg}</span>}
    </span>
  );
}

// Fix a misclassified document's metadata (type / court / title / language).
function MetadataEditor({ d, onDone }: { d: any; onDone: () => void }) {
  const [doc_type, setDocType] = useState(d.doc_type || "");
  const [court, setCourt] = useState(d.court || "");
  const [title, setTitle] = useState(d.title || "");
  const [lang, setLang] = useState(d.source_language || "");
  const [msg, setMsg] = useState("");
  return (
    <div className="row" style={{ flexWrap: "wrap", marginTop: 6 }}>
      <select value={doc_type} onChange={(e) => setDocType(e.target.value)} style={{ flex: "0 0 auto" }}>
        {DOC_TYPES.map((t) => <option key={t}>{t}</option>)}
      </select>
      <input value={court} onChange={(e) => setCourt(e.target.value)} placeholder="court" style={{ maxWidth: 140 }} />
      <input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="title" />
      <input value={lang} onChange={(e) => setLang(e.target.value)} placeholder="lang" style={{ maxWidth: 70 }} />
      <button className="primary" style={{ flex: "0 0 auto" }} onClick={async () => {
        const r = await api.updateDocument(d.stable_id, { doc_type, court, title, source_language: lang });
        if (r.error) setMsg("error: " + r.error); else onDone();
      }}>Save</button>
      {msg && <span className="err">{msg}</span>}
    </div>
  );
}

// One citation edge with inline corrections: reclassify treatment, re-point to the
// right document, or reject as a false positive (✗).
function RelationRow({ r, open, onDone }: { r: any; open: (id: string) => void; onDone: () => void }) {
  const [repoint, setRepoint] = useState(false);
  const [dst, setDst] = useState("");
  async function correct(body: Record<string, unknown>) { await api.correctCitation({ relation_id: r.relation_id, ...body }); onDone(); }
  return (
    <tr>
      <td>
        <select value={r.relationship_type} title="reclassify treatment"
          onChange={(e) => correct({ treatment: e.target.value })}
          style={{ background: "transparent", border: "none", color: "inherit", cursor: "pointer" }}>
          {[...new Set([r.relationship_type, ...TREATMENTS])].map((t) => <option key={t}>{t}</option>)}
        </select>
        {r.extracted_via === "manual" && <span className="muted" title="human-corrected"> ✎</span>}
      </td>
      <td>{r.dst_id ? <a onClick={() => open(r.dst_id)}>{r.dst_id}</a> : <span className="muted">{r.raw_citation_string}</span>}
        {r.dst_anchor && <span className="muted"> ◆ {r.dst_anchor}</span>}</td>
      <td className="muted">{r.resolution_status}</td>
      <td style={{ whiteSpace: "nowrap" }}>
        <a title="re-point to the correct document" style={{ cursor: "pointer" }} onClick={() => setRepoint((v) => !v)}>⤳</a>{" "}
        <a title="reject as a false positive" style={{ cursor: "pointer" }} onClick={() => correct({ suppress: true })}>✗</a>
        {repoint && <div className="row" style={{ marginTop: 4 }}>
          <input value={dst} onChange={(e) => setDst(e.target.value)} placeholder="correct stable_id" style={{ minWidth: 180 }} />
          <button style={{ flex: "0 0 auto" }} onClick={() => dst && correct({ dst_id: dst })}>set</button>
        </div>}
      </td>
    </tr>
  );
}

// --- Dashboard -------------------------------------------------------------
export function Dashboard({ open }: { open: (id: string) => void }) {
  const [sources, , reloadSources] = useAsync(() => api.sources(), []);
  const [queues, , reloadQueues] = useAsync(() => api.queues(), []);
  const [alerts, , reloadAlerts] = useAsync(() => api.alerts(), []);
  const [stats, , reloadStats] = useAsync(() => api.stats(), []);
  const [worklist, , reloadWork] = useAsync(() => api.worklist(20), []);
  const [srcList] = useAsync(() => api.sourceList(), []);
  const [health] = useAsync(() => api.embeddingHealth(), []);
  const [msg, setMsg] = useState("");
  const [harvestSrc, setHarvestSrc] = useState("");
  const [backfill, setBackfill] = useState(false);
  const [pages, setPages] = useState(1);

  const refresh = () => { reloadSources(); reloadQueues(); reloadAlerts(); reloadStats(); reloadWork(); };
  async function act(p: Promise<any>, label: string) {
    setMsg(label + "…");
    try { const r = await p; setMsg(`${label}: ` + JSON.stringify(r)); refresh(); }
    catch (e: any) { setMsg("error: " + e); }
  }
  return (
    <div>
      <div className="panel">
        <div className="row" style={{ alignItems: "center" }}>
          <b style={{ flex: 1 }}>Operations</b>
          <button onClick={refresh} style={{ flex: "0 0 auto" }}>↻ Refresh</button>
          <button onClick={() => act(api.embed(), "embed")} style={{ flex: "0 0 auto" }}>Embed pending</button>
          <button onClick={() => act(api.resolve(), "resolve")} style={{ flex: "0 0 auto" }}>Resolve citations</button>
          <button onClick={() => act(api.backfillTitles(), "eurlex names")} style={{ flex: "0 0 auto" }}
            title="Pull CJEU case names + subjects from the EUR-Lex webservice (needs credentials in Settings)">EU case names</button>
          <button onClick={() => act(api.startJob("rescan-citations", {}).then((j) => `started job ${j.job_id.slice(0,8)} (watch Jobs)`), "re-scan citations")}
            style={{ flex: "0 0 auto" }}
            title="Re-extract EVERY document with the current grammars — run after a new adapter/grammar (e.g. ECHR) so existing docs pick up the new citations. Runs in the background.">↻ Re-scan all citations</button>
          <button onClick={() => act(api.startJob("expand-citing", {}).then((j) => j.error ? j.error : `started job ${j.job_id.slice(0,8)} (watch Jobs)`), "pull citing cases")}
            style={{ flex: "0 0 auto" }}
            title="Find and pull every case that CITES an EU case already in the corpus (via CELLAR's citation graph). Backward citation expansion. Runs in the background.">⇊ Pull cases citing EU cases</button>
        </div>
        <div className="row" style={{ marginTop: 8, alignItems: "center", flexWrap: "wrap" }}>
          <span className="muted" style={{ flex: "0 0 auto" }}>Harvest from</span>
          <select value={harvestSrc} onChange={(e) => setHarvestSrc(e.target.value)} style={{ flex: "0 0 auto", minWidth: 150 }}>
            <option value="">choose a source…</option>
            {(srcList ?? []).map((s) => <option key={s}>{s}</option>)}
          </select>
          <label style={{ flex: "0 0 auto", display: "flex", alignItems: "center", gap: 4 }} title="Off: only items new since the last run. On: re-pull from the beginning.">
            <input type="checkbox" checked={backfill} onChange={(e) => setBackfill(e.target.checked)} /> backfill (all history)
          </label>
          <label style={{ flex: "0 0 auto", display: "flex", alignItems: "center", gap: 4 }} title="Each page is one batch from the source's listing (~tens of items).">
            pages <input type="number" min={1} max={50} value={pages} onChange={(e) => setPages(+e.target.value || 1)} style={{ width: 52 }} />
          </label>
          <button className="primary" disabled={!harvestSrc} style={{ flex: "0 0 auto" }}
            onClick={() => act(api.harvest({ source: harvestSrc, backfill, max_pages: pages }), "harvest")}>Run</button>
          {health && <span className={health.healthy ? "ok" : "err"} style={{ flex: 1, textAlign: "right" }}>
            embeddings: {health.provider}/{health.model} {health.healthy ? "✓" : "✗ (set a key in Settings)"}</span>}
        </div>
        <p className="muted" style={{ marginTop: 6, fontSize: 12 }}>
          {harvestSrc
            ? <>Fetches documents from <b>{harvestSrc}</b>, newest first — {backfill
                ? <>re-pulling <b>from the beginning</b> ({pages} page{pages > 1 ? "s" : ""}, ~tens of items each)</>
                : <>only items <b>new since the last run</b> (incremental; the source remembers a watermark)</>}.
                Each harvest then extracts citations, resolves them, and applies tag rules. Already-seen documents are skipped by content hash.</>
            : <>Pick a source to pull documents from. Curated sources (e.g. <i>uk-grc</i>, <i>eu-cellar</i>) are pre-scoped; legislation sources fetch a configured set of acts. To pull a <i>specific</i> case or act, use the Unresolved tab’s harvest buttons instead.</>}
        </p>
        {msg && <p className="muted" style={{ wordBreak: "break-all" }}>{msg}</p>}
      </div>

      <div className="panel">
        <h3>Alerts</h3>
        {(alerts ?? []).length === 0 ? <p className="ok">All healthy.</p> :
          (alerts ?? []).map((a, i) => <div key={i} className={`sev-${a.severity}`}>[{a.severity}] {a.code} ({a.subject}): {a.message}</div>)}
      </div>
      <div className="grid2">
        <div className="panel">
          <h3>Sources</h3>
          <table><thead><tr><th>source</th><th>docs</th><th>fails</th><th>last yield</th></tr></thead><tbody>
            {(sources ?? []).map((s) => (
              <tr key={s.key}><td>{s.key}</td><td>{s.documents}</td>
                <td className={s.consecutive_failures ? "err" : ""}>{s.consecutive_failures}</td>
                <td className="muted">{s.last_yield_at?.slice(0, 10) || "—"}</td></tr>
            ))}
          </tbody></table>
        </div>
        <div className="panel">
          <h3>Pipeline queues</h3>
          <table><tbody>{Object.entries(queues ?? {}).map(([k, v]) => <tr key={k}><td>{k}</td><td>{v}</td></tr>)}</tbody></table>
        </div>
      </div>
      {(worklist ?? []).length > 0 && (
        <div className="panel">
          <p className="muted" style={{ margin: 0 }}>{worklist!.length}+ citations not yet in the corpus —
            see the <b>Unresolved</b> tab for the full most-cited harvest worklist with one-click harvest.</p>
        </div>
      )}
      {stats && (
        <div className="panel">
          <h3>Corpus · {stats.total} documents · resolution {Math.round((stats.resolution?.coverage || 0) * 100)}%</h3>
          <div>{Object.entries(stats.by_doc_type || {}).map(([k, v]: any) => <span className="tag" key={k}>{k}: {v}</span>)}</div>
          <div>{Object.entries(stats.by_source || {}).map(([k, v]: any) => <span className="tag" key={k}>{k}: {v}</span>)}</div>
          <div>{Object.entries(stats.by_tag || {}).map(([k, v]: any) =>
            <span className="tag" key={k}><a onClick={() => open("")} title="filter in Corpus tab">#{k}: {v}</a></span>)}</div>
        </div>
      )}
    </div>
  );
}

// --- Import (new / bulk) ---------------------------------------------------
// Paste any text (a judgment, an email, a reading list) → detect every citation in it
// and seed the graph forwards (what they cite) and backwards (what cites them).
function SeedTextPanel({ open }: { open: (id: string) => void }) {
  const [text, setText] = useState("");
  const [detected, setDetected] = useState<any[] | null>(null);
  const [degrees, setDegrees] = useState(1);
  const [citing, setCiting] = useState(true);
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);
  const detect = async () => {
    setBusy(true); setMsg("");
    try { const r = await api.detectCitations(text); setDetected(r.citations || []); }
    catch (e: any) { setMsg("error: " + e.message); }
    finally { setBusy(false); }
  };
  const seed = async () => {
    setMsg("starting…");
    try {
      const { job_id } = await api.startJob("seed-text", { text, degrees, include_citing: citing });
      setMsg(`✓ seeding job ${job_id.slice(0, 8)} started — watch progress in Jobs below`);
    } catch (e: any) { setMsg("error: " + e.message); }
  };
  const routable = (detected || []).filter((c) => c.routable).length;
  return (
    <div className="panel">
      <h3 style={{ marginTop: 0 }}>Seed from pasted text
        <span className="muted"> — paste anything with citations; detect & pull them, then radiate</span></h3>
      <textarea value={text} onChange={(e) => setText(e.target.value)} style={{ minHeight: 110 }}
        placeholder="Paste a judgment, a reading list, an email… ECLIs, neutral citations ([2021] UKSC 12), CELEX, and Acts are all detected." />
      <div className="row" style={{ alignItems: "center", marginTop: 8 }}>
        <button disabled={busy || !text.trim()} onClick={detect} style={{ flex: "0 0 auto" }}>🔎 Detect</button>
        <label style={{ display: "flex", alignItems: "center", gap: 4, flex: "0 0 auto", margin: 0 }}>
          degrees out
          <select value={degrees} onChange={(e) => setDegrees(Number(e.target.value))} style={{ width: 60 }}>
            <option value={0}>0</option><option value={1}>1</option><option value={2}>2</option>
          </select>
        </label>
        <label style={{ display: "flex", alignItems: "center", gap: 4, flex: "0 0 auto", margin: 0 }}>
          <input type="checkbox" checked={citing} onChange={(e) => setCiting(e.target.checked)} style={{ width: "auto" }} />
          also pull what cites them
        </label>
        <button className="primary" disabled={!detected || detected.length === 0} onClick={seed} style={{ flex: "0 0 auto" }}>
          ⤓ Seed & radiate</button>
      </div>
      {msg && <p className={msg.startsWith("error") ? "err" : "ok"} style={{ fontSize: 12 }}>{msg}</p>}
      {detected && (
        <div style={{ marginTop: 6 }}>
          <p className="muted" style={{ fontSize: 12 }}>{detected.length} citation(s) detected · {routable} routable</p>
          {detected.map((c, i) => (
            <span key={i} className="tag" title={c.form}>
              {c.in_corpus ? <a onClick={() => open(c.candidate)} style={{ cursor: "pointer" }}>{c.candidate} ✓</a> : c.candidate}
              <span className="muted"> · {c.form}{!c.routable ? " · no adapter" : ""}</span>
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

export function ImportView({ open }: { open?: (id: string) => void }) {
  const [msg, setMsg] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [docType, setDocType] = useState("commentary");
  const [linkTo, setLinkTo] = useState("");
  const show = (r: any) => setMsg(typeof r === "string" ? r : JSON.stringify(r));
  return (
    <div>
      <SeedTextPanel open={open || (() => {})} />
      <div className="panel">
        <p className="muted">Import standalone secondary material here. To attach material to a <i>specific</i> case or
          law section, open it in Search/Corpus and use its “Augment” panel instead.</p>
        <h3>Upload a PDF / HTML file</h3>
        <div className="row">
          <input type="file" onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
          <select value={docType} onChange={(e) => setDocType(e.target.value)}>
            {DOC_TYPES.map((t) => <option key={t}>{t}</option>)}
          </select>
        </div>
        <label>Link to (stable_id of a case/law section — optional)</label>
        <input value={linkTo} onChange={(e) => setLinkTo(e.target.value)} placeholder="ECLI:EU:C:2020:559" />
        <p><button className="primary" onClick={async () => {
          if (!file) return setMsg("choose a file");
          try { show(await api.importFile(file, { doc_type: docType, link_to: linkTo })); } catch (e: any) { show("error: " + e); }
        }}>Import file</button></p>
      </div>
      <div className="panel">
        <h3>Zotero library</h3>
        <p className="muted">Uses the credentials saved in Settings.</p>
        <button className="primary" onClick={async () => { try { show(await api.importZotero({ limit: 50 })); } catch (e: any) { show("error: " + e); } }}>Import from Zotero</button>
      </div>
      {msg && <div className="panel"><pre style={{ whiteSpace: "pre-wrap", wordBreak: "break-all" }}>{msg}</pre></div>}
    </div>
  );
}

// --- Settings --------------------------------------------------------------
export function SettingsView() {
  const [settings, setSettings] = useState<Setting[]>([]);
  const [path, setPath] = useState("");
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [msg, setMsg] = useState("");
  const [health] = useAsync(() => api.embeddingHealth(), [msg]);
  const load = () => api.getSettings().then((r) => { setSettings(r.settings); setPath(r.path); });
  useEffect(() => { load(); }, []);
  const groups = [...new Set(settings.map((s) => s.group))];
  return (
    <div>
      <div className="panel">
        {health && <p className={health.healthy ? "ok" : "err"}>Embedding provider: {health.provider}/{health.model} ({health.dimensions}d) — {health.healthy ? "ready ✓" : "needs an API key ✗"}</p>}
        <p className="muted">Stored in <span className="kbd">{path}</span> (bind-mount the data dir to persist).
          An environment variable, if set, overrides the file value.</p>
        {groups.map((g) => (
          <div key={g}>
            <h3>{g}</h3>
            {settings.filter((s) => s.group === g).map((s) => (
              <div key={s.key}>
                <label>{s.label} <span className="kbd">{s.key}</span>
                  {s.source === "env" && <span className="muted"> · set via environment (overrides file)</span>}
                  {s.source === "file" && s.set && <span className="ok"> · {s.secret ? s.display : "saved"}</span>}
                </label>
                <input type={s.secret ? "password" : "text"} placeholder={s.placeholder || (s.set ? s.display : "")}
                  disabled={s.source === "env"}
                  value={edits[s.key] ?? ""} onChange={(e) => setEdits({ ...edits, [s.key]: e.target.value })} />
              </div>
            ))}
          </div>
        ))}
        <p>
          <button className="primary" onClick={async () => {
            try { const r = await api.saveSettings(edits); setSettings(r.settings); setEdits({}); setMsg("Saved."); }
            catch (e: any) { setMsg("error: " + e); }
          }}>Save settings</button>{" "}
          <span className={msg.startsWith("error") ? "err" : "ok"}>{msg}</span>
        </p>
      </div>
    </div>
  );
}

// --- Watches (scheduled keyword harvest + autosnowball) --------------------
export function WatchesView() {
  const [cat] = useAsync(() => api.sourceCatalog(), []);
  const [watches, , reload] = useAsync(() => api.watches(), []);
  const [name, setName] = useState("");
  const [source, setSource] = useState("");
  const [keywords, setKeywords] = useState("");
  const [cites, setCites] = useState("");
  const [citing, setCiting] = useState("");
  const [degrees, setDegrees] = useState(2);
  const [tag, setTag] = useState("");
  const [cadence, setCadence] = useState(1440);
  const [maxPages, setMaxPages] = useState(1);
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState<number | "new" | null>(null);

  const info = (cat ?? []).find((s) => s.key === source);

  async function create() {
    if (!name || (!source && !cites && !citing)) { setMsg("give a name and a source, a ‘discover citing’ target, or a ‘cites’ rule"); return; }
    setBusy("new"); setMsg("");
    const spec: any = { degrees, max_pages: maxPages };
    if (source) spec.source = source;
    if (keywords.trim()) spec.keywords = keywords.split(",").map((k) => k.trim()).filter(Boolean);
    if (citing.trim()) spec.discover = { citing: citing.trim(), via: "auto" };
    if (cites.trim()) spec.seed_rule = { cites: cites.trim() };
    if (tag.trim()) spec.tag = tag.trim();
    try {
      await api.createWatch({ name, spec, cadence_minutes: cadence });
      setName(""); setKeywords(""); setCites(""); setCiting(""); setTag(""); reload();
    } catch (e: any) { setMsg("error: " + e); } finally { setBusy(null); }
  }
  async function run(id: number) {
    setBusy(id); setMsg("running watch… (harvest + snowball; may take a while)");
    try { const r = await api.runWatch(id); setMsg(`✓ watch #${id}: ` + summariseRun(r)); reload(); }
    catch (e: any) { setMsg("error: " + e); } finally { setBusy(null); }
  }
  return (
    <div>
      <div className="panel">
        <h3>New watch <span className="muted">— a saved harvest plan: keyword-limit a source, then enrich each new case with its citations.</span></h3>
        <p className="muted" style={{ fontSize: 12, marginTop: 0 }}>
          Scheduling pays off when new material keeps arriving. The two <b style={{ color: "#7ee787" }}>growing</b> watch
          types: a <b>source/keyword</b> harvest (new decisions are handed down), and <b>🔎 discover cases citing X</b> —
          forward-citation discovery via Find Case Law / CELLAR, which finds <i>new</i> judgments that cite a landmark
          as they appear. The snowball then back-fills each new case’s authorities. A pure <b>graph rule</b> (no source/
          discovery) is largely <i>one-shot</i> — a backward snowball converges; for a one-off radiate from a single
          document, use the <b>❅ Snowball</b> button there instead.
        </p>
        <div className="row" style={{ flexWrap: "wrap" }}>
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder="watch name, e.g. ‘UK DP cases’" style={{ minWidth: 180 }} />
          <select value={source} onChange={(e) => setSource(e.target.value)} style={{ flex: "0 0 auto" }}>
            <option value="">— source (optional) —</option>
            {(cat ?? []).map((s) => <option key={s.key} value={s.key}>{s.label}</option>)}
          </select>
        </div>
        {/* morph: explain what THIS source supports */}
        {info && <p className="muted" style={{ fontSize: 12 }}>{info.description}{" "}
          {info.keyword_search
            ? <b style={{ color: "#7ee787" }}>Keywords are searched in the source API.</b>
            : <b style={{ color: "#ffb454" }}>This source has no API search — keywords filter the harvested results.</b>}</p>}
        <div className="row" style={{ flexWrap: "wrap", marginTop: 4 }}>
          {source && <input value={keywords} onChange={(e) => setKeywords(e.target.value)}
            placeholder={info?.keyword_search ? "keywords (searched at source), comma-sep" : "keywords (post-filter), comma-sep"} style={{ minWidth: 220 }} />}
          <input value={citing} onChange={(e) => setCiting(e.target.value)}
            title="Find NEW cases that cite this, via Find Case Law search (UK) or CELLAR (EU CELEX). This grows over time."
            placeholder="🔎 discover NEW cases citing… e.g. 32016R0679 (GDPR) or [2014] UKSC 38" style={{ minWidth: 280, color: "#7ee787" }} />
          <input value={cites} onChange={(e) => setCites(e.target.value)}
            placeholder="…or graph rule: corpus docs citing id" style={{ minWidth: 200 }} />
        </div>
        <div className="row" style={{ flexWrap: "wrap", marginTop: 4, alignItems: "center" }}>
          <label style={{ flex: "0 0 auto" }} title="Enrich each newly-found case by fetching what it cites, N hops out">enrich each case <select value={degrees} onChange={(e) => setDegrees(+e.target.value)}>{[0, 1, 2, 3].map((n) => <option key={n} value={n}>{n} degree{n !== 1 ? "s" : ""}</option>)}</select></label>
          {source && <label style={{ flex: "0 0 auto" }}>pages <input type="number" min={1} max={20} value={maxPages} onChange={(e) => setMaxPages(+e.target.value || 1)} style={{ width: 50 }} /></label>}
          <input value={tag} onChange={(e) => setTag(e.target.value)} placeholder="tag results into collection (optional)" style={{ maxWidth: 220 }} />
          <label style={{ flex: "0 0 auto" }}>every <input type="number" min={5} value={cadence} onChange={(e) => setCadence(+e.target.value || 1440)} style={{ width: 70 }} /> min</label>
          <button className="primary" disabled={busy === "new"} style={{ flex: "0 0 auto" }} onClick={create}>{busy === "new" ? "saving…" : "+ Create watch"}</button>
        </div>
        {msg && <p className={msg.startsWith("error") ? "err" : "ok"} style={{ wordBreak: "break-word" }}>{msg}</p>}
      </div>
      <div className="panel">
        <h3>Watches</h3>
        {(watches ?? []).length === 0 && <p className="muted">No watches yet.</p>}
        <table className="grid"><thead><tr><th></th><th>name</th><th>plan</th><th>every</th><th>last run</th><th></th></tr></thead>
          <tbody>{(watches ?? []).map((w) => (
            <tr key={w.watch_id}>
              <td><input type="checkbox" checked={w.enabled} title="enabled"
                onChange={async () => { await api.updateWatch(w.watch_id, { enabled: !w.enabled }); reload(); }} /></td>
              <td>{w.name}</td>
              <td className="muted" style={{ fontSize: 12 }}>
                {w.spec.source ? <>harvest <b>{w.spec.source}</b>{w.spec.keywords ? ` · “${w.spec.keywords.join(", ")}”` : ""}</> : null}
                {w.spec.discover ? <span style={{ color: "#7ee787" }}>🔎 cases citing <b>{w.spec.discover.citing}</b></span> : null}
                {w.spec.seed_rule ? <> seed: cites <b>{w.spec.seed_rule.cites}</b></> : null}
                {` · ❅ ${w.spec.degrees ?? 1}°`}{w.spec.tag ? ` · →#${w.spec.tag}` : ""}
                {!(w.spec.source || w.spec.discover) && <span title="No renewing source — a backward snowball converges, so scheduling adds little" style={{ color: "#ffb454" }}> · one-shot</span>}
                {w.last_result && <span> · <i>{summariseRun(w.last_result)}</i></span>}
              </td>
              <td className="muted">{w.spec.source || w.spec.discover ? `${w.cadence_minutes}m` : "—"}</td>
              <td className="muted">{w.last_run_at ? String(w.last_run_at).slice(0, 16).replace("T", " ") : "never"}</td>
              <td style={{ whiteSpace: "nowrap" }}>
                <button disabled={busy === w.watch_id} onClick={() => run(w.watch_id)}>{busy === w.watch_id ? "…" : "▸ run"}</button>{" "}
                <a style={{ cursor: "pointer" }} title="delete" onClick={async () => { await api.deleteWatch(w.watch_id); reload(); }}>✗</a>
              </td>
            </tr>
          ))}</tbody>
        </table>
      </div>
    </div>
  );
}

// Opt-in slow drain: the scheduler service fetches N routable references each tick
// (~15 min), so the whole worklist completes over time — survives closing the tab
// AND restarts (it's a separate, persistent service). 0 = off.
function AutoDrain() {
  const [val, setVal] = useState<string>("");
  const [saved, setSaved] = useState(false);
  useEffect(() => {
    api.getSettings().then((s) => {
      const row = s.settings.find((x: any) => x.key === "RAGLEX_AUTOHARVEST");
      setVal(row?.display || "0");
    }).catch(() => {});
  }, []);
  async function set(v: string) {
    setVal(v); await api.saveSettings({ RAGLEX_AUTOHARVEST: v }); setSaved(true); setTimeout(() => setSaved(false), 1500);
  }
  return (
    <label className="muted" style={{ flex: "0 0 auto", fontSize: 12, display: "flex", alignItems: "center", gap: 4 }}
      title="The scheduler slowly drains the worklist in the background, even if you close this tab or the app restarts">
      auto-drain
      <select value={val || "500"} onChange={(e) => set(e.target.value)} style={{ width: 88 }}>
        {["0", "10", "25", "50", "100", "500"].map((n) => <option key={n} value={n}>{n === "0" ? "off" : n + "/tick"}</option>)}
      </select>
      {saved && <span className="ok">✓</span>}
    </label>
  );
}

// How long to remember a failed harvest before retrying — prevents burning drain
// budget on dead URLs (pre-digital cases, absent CELLAR renditions).
function MissTTL() {
  const [val, setVal] = useState<string>("");
  const [saved, setSaved] = useState(false);
  useEffect(() => {
    api.getSettings().then((s) => {
      const row = s.settings.find((x: any) => x.key === "RAGLEX_MISS_TTL_DAYS");
      setVal(row?.display || "90");
    }).catch(() => {});
  }, []);
  async function set(v: string) {
    setVal(v); await api.saveSettings({ RAGLEX_MISS_TTL_DAYS: v }); setSaved(true); setTimeout(() => setSaved(false), 1500);
  }
  return (
    <label className="muted" style={{ flex: "0 0 auto", fontSize: 12, display: "flex", alignItems: "center", gap: 4 }}
      title="Days to skip a URL that returned 404 before retrying. Higher = less wasted drain budget on old cases that are simply not available online.">
      miss cooldown
      <select value={val || "90"} onChange={(e) => set(e.target.value)} style={{ width: 72 }}>
        <option value="14">14d</option>
        <option value="30">30d</option>
        <option value="90">90d</option>
        <option value="180">180d</option>
        <option value="365">1yr</option>
      </select>
      {saved && <span className="ok">✓</span>}
    </label>
  );
}

// The single, app-wide jobs panel (rendered once in App): a floating, collapsible card
// that shows every background job with a live progress bar AND a verbose, item-by-item
// log. Polls the job list while anything runs, and the open job's log for detail.
export function JobsPanel() {
  const [jobs, setJobs] = useState<any[]>([]);
  const [openId, setOpenId] = useState<string | null>(null);
  const [detail, setDetail] = useState<any>(null);
  const [collapsed, setCollapsed] = useState(false);
  const anyRunning = jobs.some((j) => j.status === "running");
  useEffect(() => {
    let live = true;
    const tick = async () => {
      try {
        const j = await api.jobsList();
        if (!live) return;
        setJobs(j);
        // auto-open the newest running job so its log is visible without a click
        const running = j.find((x: any) => x.status === "running");
        setOpenId((cur) => cur && j.some((x: any) => x.id === cur) ? cur : (running?.id ?? null));
      } catch { /* ignore */ }
    };
    tick();
    // Poll fast while something runs, slowly when idle: the panel used to hit /jobs every
    // 1.5s forever, ~40 req/min of pure noise even on a quiet system.
    const iv = setInterval(tick, anyRunning ? 1500 : 10000);
    return () => { live = false; clearInterval(iv); };
  }, [anyRunning]);
  // poll the open job's full log (only one job at a time → cheap)
  useEffect(() => {
    if (!openId) { setDetail(null); return; }
    let live = true;
    const tick = async () => { try { const d = await api.jobStatus(openId); if (live) setDetail(d); } catch { /* ignore */ } };
    tick();
    // only worth fast-polling a job that's actually moving
    const openRunning = jobs.find((j) => j.id === openId)?.status === "running";
    const iv = setInterval(tick, openRunning ? 1200 : 8000);
    return () => { live = false; clearInterval(iv); };
  }, [openId, jobs]);

  const active = jobs.filter((j) => j.status === "running");
  const recent = jobs.filter((j) => j.status !== "running").slice(0, 4);
  if (active.length === 0 && recent.length === 0) return null;
  const icon = (s: string) => (s === "cancelled" ? "⊘" : s === "error" ? "✗" : s === "done" ? "✓" : "●");
  return (
    <div className={`jobs-dock${collapsed ? " collapsed" : ""}`}>
      <div className="jobs-head" onClick={() => setCollapsed((c) => !c)}>
        <b>Jobs</b>{active.length > 0 && <span className="jobs-spin"> ● {active.length} running</span>}
        <span style={{ flex: 1 }} />
        <span className="muted">{collapsed ? "▸" : "▾"}</span>
      </div>
      {!collapsed && <div className="jobs-body">
        {active.map((j) => {
          const p = j.progress || {};
          const pct = p.total ? Math.round((100 * (p.done || 0)) / p.total) : 0;
          const isOpen = openId === j.id;
          return (
            <div key={j.id} className={`job${j.stalled ? " job-stalled" : ""}`}>
              <div className="row" style={{ alignItems: "center", gap: 6 }}>
                <a onClick={() => setOpenId(isOpen ? null : j.id)} style={{ flex: 1, cursor: "pointer", fontSize: 12 }}>
                  {isOpen ? "▾" : "▸"} {j.label || j.kind}
                  {j.origin === "scheduler" && <span className="tag" style={{ marginLeft: 6, fontSize: 10 }} title="Started by the background scheduler, not from this UI">scheduler</span>}</a>
                {j.stalled && <span className="job-stall-tag" title={`No progress for ${Math.round(j.idle_s)}s — the job is probably frozen (its network connection died, e.g. after the host slept). Restart to resume from where the data left off; it skips work already done.`}>frozen?</span>}
                <button className="mini" title="Re-run this job from where its saved data left off (skips work already done). Use it when a job has frozen after the machine slept/woke." onClick={() => api.restartJob(j.id)}>↻ restart</button>
                <button className="mini" onClick={() => api.cancelJob(j.id)}>cancel</button>
              </div>
              <div className="job-bar"><div style={{ width: `${pct}%` }} /></div>
              <div className="muted" style={{ fontSize: 11 }}>{j.last || (p.stage ? `${p.stage} ${p.done ?? 0}/${p.total ?? "?"}` : "starting…")}</div>
              {isOpen && detail?.log && (
                <pre className="job-log">{(detail.log || []).slice(-14).join("\n")}</pre>
              )}
            </div>
          );
        })}
        {recent.map((j) => (
          <div key={j.id} className="job-done muted row" title={j.last} style={{ alignItems: "center", gap: 6 }}>
            <span style={{ flex: 1 }}>{icon(j.status)} {j.label || j.kind} — {j.last || j.status}</span>
            <button className="mini" title="Run this job again from where its saved data left off" onClick={() => api.restartJob(j.id)}>↻ restart</button>
          </div>
        ))}
      </div>}
    </div>
  );
}

function summariseRun(r: any): string {
  if (!r) return "";
  const got = (r.radiate?.degrees || []).reduce((a: number, d: any) => a + d.harvested, 0);
  const stored = r.harvest?.stored ?? 0;
  const disc = r.discover?.count ?? 0;
  return [stored ? `harvested ${stored}` : "", disc ? `discovered ${disc}` : "",
          `snowballed ${got}`, r.tagged ? `tagged ${r.tagged}` : ""].filter(Boolean).join(" · ");
}

// --- Unresolved references -------------------------------------------------
// The hanging references the corpus cites but can't satisfy. Each can be resolved
// by supplying the missing identifier, linking to an existing item, scraping a
// URL, or uploading the source file (§5b).
export function UnresolvedView({ open, navigate }: { open: (id: string) => void; navigate?: (f: Record<string, string>) => void }) {
  const [rows, err, reload] = useAsync(() => api.unresolved(200), []);
  const [cov, , reloadCov] = useAsync(() => api.coverage(), []);
  // after any harvest/resolve, refresh BOTH the list and the per-source "remaining"
  // counts (which come from coverage — the server invalidates its cache on harvest)
  const reloadAll = () => { reload(); reloadCov(); };
  // coverage scans >1M edges; the API returns {_warming} on a cold load — poll until ready
  useEffect(() => {
    if (!cov?._warming) return;
    const iv = setInterval(() => reloadCov(), 2500);
    return () => clearInterval(iv);
  }, [cov?._warming]);
  const [active, setActive] = useState<string | null>(null);
  const [bulk, setBulk] = useState("");
  const [srcFilter, setSrcFilter] = useState("");   // suggested_adapter, or "" = all
  const [legFilter, setLegFilter] = useState("");    // primary|secondary|assimilated, or ""
  if (err) return <div className="panel"><p className="err">{err}</p></div>;
  const all = rows ?? [];
  // source options come from the corpus-wide routable breakdown (so even sources not on
  // this loaded page appear); fall back to whatever's in the page.
  const byCat: Record<string, number> = cov?.routable_by_category ?? {};
  const sources = Object.keys(byCat).filter((k) => !k.includes(":")).sort();
  const showLeg = srcFilter === "uk-legislation";
  // filter the displayed rows by the selected source / UK-legislation sub-category
  const list = all.filter((r) =>
    (!srcFilter || r.suggested_adapter === srcFilter) &&
    (!legFilter || r.leg_kind === legFilter));
  // the routable count for the CURRENT filter (corpus-wide), for the harvest button
  const catKey = showLeg && legFilter ? `uk-legislation:${legFilter}` : srcFilter;
  const routableCount = catKey ? (byCat[catKey] ?? 0)
    : (cov?.routable_references ?? all.filter((r) => r.suggested_adapter && r.confidence !== "low" && !r.needs_identifier).length);

  async function harvestAll() {
    const label = srcFilter ? `${legFilter ? legFilter + " " : ""}${srcFilter}` : "all routable";
    setBulk(`harvesting ${label} references… (runs in the background — you can leave this page)`);
    try {
      const body: Record<string, unknown> = { limit: 20000 };
      if (srcFilter) body.adapter = srcFilter;
      if (showLeg && legFilter) body.leg_kind = legFilter;
      const r = await runJob("harvest-all", body,
        (p) => setBulk(p.total ? `${p.stage}: ${p.done}/${p.total}…` : `${p.stage}…`));
      // Explain a do-nothing run instead of silently claiming success: an empty attempt
      // is almost always the whole candidate set still cooling off after earlier failures.
      if (r.rate_limited) {
        setBulk(`⏸ the source began rate-limiting — stopped after ${r.harvested} to avoid burning the rest of the worklist. Try again shortly.`);
      } else if (r.attempted === 0 && r.skipped_recent_fail > 0) {
        setBulk(`nothing attempted — all ${r.skipped_recent_fail} routable references are cooling off after earlier failures. Use “retry failed” to clear the cool-down if a source was just unavailable.`);
      } else {
        setBulk(`✓ fetched ${r.harvested}/${r.attempted} · resolved ${r.resolved_edges} edge(s)` +
          (r.absent ? ` · ${r.absent} absent (cooled 90d)` : "") +
          (r.retry_later ? ` · ${r.retry_later} unreachable (retry ~6h)` : "") +
          (r.remaining ? ` · ${r.remaining} still routable` : ""));
      }
      reloadAll();
    } catch (e: any) { setBulk("error: " + e); }
  }

  async function retryFailed() {
    setBulk("clearing the failure cool-down…");
    try {
      await api.retryFailed();
      setBulk("✓ cool-down cleared — every reference is eligible again on the next harvest");
      reloadAll();
    } catch (e: any) { setBulk("error: " + e); }
  }

  const cooling = cov?.cooling_off ?? 0;
  const ready = cov?.ready_references;
  return (
    <div>
    <CorpusMap cov={cov} navigate={navigate} />
    <div className="panel">
      <div className="row" style={{ justifyContent: "space-between", alignItems: "flex-start", flexWrap: "wrap" }}>
        <h3 style={{ margin: 0 }}>Harvest worklist <span className="muted">— citations the corpus can’t find (yet), most-cited first. Resolve by harvest, identifier, existing item, scrape, or upload.</span></h3>
        <div className="row" style={{ flex: "0 0 auto", alignItems: "center" }}>
          <select className="theme-select" value={srcFilter}
            onChange={(e) => { setSrcFilter(e.target.value); setLegFilter(""); }} title="Filter by source">
            <option value="">All sources</option>
            {sources.map((s) => <option key={s} value={s}>{s} ({byCat[s]})</option>)}
          </select>
          {showLeg && <select className="theme-select" value={legFilter}
            onChange={(e) => setLegFilter(e.target.value)} title="UK legislation type">
            <option value="">All UK legislation</option>
            <option value="primary">Primary ({byCat["uk-legislation:primary"] ?? 0})</option>
            <option value="secondary">Secondary ({byCat["uk-legislation:secondary"] ?? 0})</option>
            <option value="assimilated">Assimilated EU ({byCat["uk-legislation:assimilated"] ?? 0})</option>
          </select>}
          <AutoDrain />
          <MissTTL />
          {cooling > 0 && <button className="mini" style={{ flex: "0 0 auto" }} onClick={retryFailed}
            title={`${cooling} routable references are cooling off after an earlier failure (${cov?.cooling_off_absent ?? 0} the source said don't exist, ${cov?.cooling_off_retry ?? 0} merely unreachable). Clear the cool-down to retry them all now — do this if a source was simply down.`}>
            ↻ retry {cooling} failed</button>}
          {routableCount > 0 && <button className="primary" style={{ flex: "0 0 auto" }} onClick={harvestAll}
            title="Fetch every routable reference in the current filter and resolve — runs in the background, survives closing this tab">
            ⤓ Harvest {srcFilter ? "filtered" : "all routable"} ({routableCount})</button>}
        </div>
      </div>
      {!srcFilter && ready != null && ready < routableCount && (
        <p className="muted" style={{ fontSize: 12, marginTop: 4 }}>
          {ready.toLocaleString()} ready to harvest now · {cooling.toLocaleString()} cooling off after earlier failures <Info t="A harvest attempts only the 'ready' references. The rest failed recently and are skipped for a while so a dead URL doesn't stall every run — genuine 404s for 90 days, mere timeouts for ~6 hours. 'retry failed' clears that early." />
        </p>
      )}
      {bulk && <p className={bulk.startsWith("error") ? "err" : "ok"}>{bulk}</p>}
      {list.length === 0 && <p className="muted">Nothing hanging — every citation resolves. ✓</p>}
      <table className="grid">
        <thead><tr><th>cites</th><th>reference</th><th>looks like</th><th>route</th><th></th></tr></thead>
        <tbody>
          {list.map((r) => (
            <ResolveRow key={r.ref} r={r} open={open}
              active={active === r.ref} toggle={() => setActive(active === r.ref ? null : r.ref)}
              onDone={reloadAll} />
          ))}
        </tbody>
      </table>
    </div>
    <UnfetchablePanel />
    </div>
  );
}

// --- Cited but unfetchable: the pre-neutral-citation frontier ----------------
// Most-cited references the system CAN'T fetch — classic law reports ("[1982] AC 1"),
// cases by name, courts with no adapter. Each carries a BAILII link (direct RTF where a
// neutral citation exists, else a citation search) and an upload that resolves it in place.
function UnfetchablePanel() {
  const [data, err, reload] = useAsync(() => api.unfetchable(200), []);
  useEffect(() => {
    if (!data?._warming) return;
    const iv = setInterval(() => reload(), 2500);
    return () => clearInterval(iv);
  }, [data?._warming]);
  const [upRef, setUpRef] = useState<string | null>(null);
  const refs: any[] = data?.references || [];
  if (err) return null;
  return (
    <div className="panel">
      <div className="row" style={{ alignItems: "baseline" }}>
        <h3 style={{ marginTop: 0, flex: 1 }}>Cited but unfetchable
          <span className="muted"> — most-cited authorities the system can’t fetch (classic reporters, cases by name). Follow the link, then upload the file to resolve every citation to it at once.</span>
          {data?.total != null && <span className="tag" style={{ marginLeft: 8 }}>{data.total.toLocaleString()}</span>}
        </h3>
        <button className="mini" style={{ flex: "0 0 auto" }}
          title="Scrape the House of Lords archive (1996–2009) and match reporter-only citations ('[1998] AC 1') to the harvested cases by name + year. Runs in the background — see the Jobs panel."
          onClick={async () => { try { await api.harvestHoL(); } catch { /* surfaced in Jobs */ } }}>
          ⚖ scrape House of Lords + match</button>
      </div>
      {data?._warming && <p className="muted loading-pulse">⏳ ranking the unfetchable frontier…</p>}
      {!data?._warming && refs.length === 0 && <p className="muted">Nothing recognised as unfetchable. ✓</p>}
      {refs.length > 0 && (
        <table className="grid">
          <thead><tr><th>cites</th><th>reference</th><th>looks like</th><th>source</th><th></th></tr></thead>
          <tbody>
            {refs.map((r) => (
              <Fragment key={r.ref}>
                <tr>
                  <td className="num" style={{ whiteSpace: "nowrap" }}>{r.citing_count}</td>
                  <td style={{ fontFamily: "var(--mono, monospace)", fontSize: 12 }}>{r.raw || r.ref}</td>
                  <td className="muted">{r.form}</td>
                  <td style={{ whiteSpace: "nowrap" }}>
                    {r.link
                      ? <a href={r.link.url} target="_blank" rel="noopener noreferrer">{r.link.label}</a>
                      : <span className="muted">—</span>}
                  </td>
                  <td style={{ whiteSpace: "nowrap" }}>
                    {r.link?.can_upload && <a style={{ cursor: "pointer" }}
                      onClick={() => setUpRef(upRef === r.ref ? null : r.ref)}>{upRef === r.ref ? "cancel" : "⬆ upload"}</a>}
                  </td>
                </tr>
                {upRef === r.ref && (
                  <tr><td colSpan={5}>
                    <UnfetchableUpload r={r} onDone={() => { setUpRef(null); reload(); }} />
                  </td></tr>
                )}
              </Fragment>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

// Upload the file the user downloaded (from BAILII etc.) and resolve the reference to it.
// A neutral-citation slug imports under that stable_id (import_bailii for RTF); a
// candidate-less report resolves the pasted-citation edge to the uploaded document.
function UnfetchableUpload({ r, onDone }: { r: any; onDone: () => void }) {
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);
  const slug = r.link?.stable_id as string | undefined;
  async function upload(file: File) {
    setBusy(true); setMsg("importing…");
    try {
      const res = slug && file.name.toLowerCase().endsWith(".rtf")
        ? await api.importBailii(slug, file)
        : await api.resolveReferenceFile(r.ref, file, { title: r.raw || r.ref, doc_type: "judgment" });
      setMsg(`✓ imported · resolved ${res.resolved_edges ?? 0} citation(s)`);
      setTimeout(onDone, 900);
    } catch (e: any) { setMsg("error: " + e.message); } finally { setBusy(false); }
  }
  return (
    <div style={{ padding: "4px 0" }}>
      <p className="muted" style={{ margin: "0 0 4px", fontSize: 12 }}>
        Download the judgment from the source link{slug ? " (RTF for a direct import)" : ""}, then drop it here:
      </p>
      <input type="file" disabled={busy} accept=".rtf,.pdf,.html,.htm,.txt,.doc,.docx"
        onChange={(e) => { const f = e.target.files?.[0]; if (f) upload(f); }} />
      {msg && <p className={msg.startsWith("error") ? "err" : "ok"} style={{ fontSize: 12 }}>{msg}</p>}
    </div>
  );
}

// A quiet info glyph carrying a tooltip (Swiss restraint — explanation on demand, no chrome).
function Info({ t }: { t: string }) {
  return <span className="info" title={t} role="img" aria-label="info">ⓘ</span>;
}

// Fire a background job and report tersely; the global Jobs dock shows live progress.
async function fireJob(kind: any, body: Record<string, unknown>, setMsg: (s: string) => void) {
  try {
    const r = await api.startJob(kind, body);
    if (r.error) setMsg("✗ " + r.error);
    else if (r.already_running) setMsg("• already running");
    else setMsg("✓ queued — see the Jobs panel");
  } catch (e: any) { setMsg("✗ " + e); }
}

// What each category's docs cite, by target category (unique vs total) — lazy-loaded.
function CitesPanel({ category }: { category: string }) {
  const [data, err] = useAsync(() => api.corpusMapCites(category), [category]);
  if (err) return <p className="err" style={{ margin: "4px 0" }}>{String(err)}</p>;
  if (!data) return <p className="muted loading-pulse" style={{ margin: "4px 0" }}>⏳ tracing what this category cites…</p>;
  const targets: any[] = data.targets || [];
  if (targets.length === 0) return <p className="muted" style={{ margin: "4px 0" }}>cites nothing tracked.</p>;
  return (
    <div className="cites-panel">
      <div className="muted" style={{ marginBottom: 4 }}>
        cites <Info t="Across all held documents in this category: the distinct things they cite (unique — a document citing the same target three times counts once) and the total number of citation occurrences." />
      </div>
      <table className="grid cites-grid"><thead><tr><th>target category</th><th>unique</th><th>total</th></tr></thead>
        <tbody>{targets.map((t) => (
          <tr key={t.category}><td>{t.label}</td><td className="num">{t.unique.toLocaleString()}</td><td className="num">{t.total.toLocaleString()}</td></tr>
        ))}</tbody></table>
    </div>
  );
}

// The Corpus Map — held-vs-pending by legal category & sub-type, with per-row actions.
// Replaces the old prose coverage panel (IBM Carbon table, Swiss numeric hierarchy).
function CorpusMap({ cov, navigate }: { cov: any; navigate?: (f: Record<string, string>) => void }) {
  const [map, err, reload] = useAsync(() => api.corpusMap(), []);
  const [open, setOpen] = useState<Set<string>>(new Set());
  const [msg, setMsg] = useState("");
  // both aggregates scan the graph; serve {_warming} on a cold load → poll until ready
  const warming = map?._warming || cov?._warming;
  useEffect(() => {
    if (!warming) return;
    const iv = setInterval(() => reload(), 2500);
    return () => clearInterval(iv);
  }, [warming]);
  const s = cov?.stats || {};
  const res = s.resolution || s.citation_resolution || {};
  const pct = res.resolved != null && res.total ? Math.round((100 * res.resolved) / res.total) : null;
  const totals = map?.totals || {};
  const toggle = (k: string) => setOpen((o) => { const n = new Set(o); n.has(k) ? n.delete(k) : n.add(k); return n; });
  const see = (f: Record<string, string>) => navigate && navigate(f);

  return (
    <div className="panel corpus-map">
      <h3 style={{ marginTop: 0 }}>Corpus map <span className="muted">— what we hold, what we’re missing, and what each part cites</span></h3>
      <div className="row stat-strip" style={{ flexWrap: "wrap", gap: 20 }}>
        <div><b>{(totals.held ?? s.total ?? 0).toLocaleString()}</b><div className="muted">held <Info t="Documents currently in the corpus." /></div></div>
        <div><b>{(totals.pending ?? cov?.routable_references ?? 0).toLocaleString()}</b><div className="muted">pending <Info t="Distinct items we cite but don’t yet hold AND can fetch automatically (a known adapter, high confidence). These are the one-click ‘Harvest’ targets." /></div></div>
        <div><b>{(totals.name_only ?? cov?.needs_identifier ?? 0).toLocaleString()}</b><div className="muted">name-only <Info t="References recognised but not routable — recognised by name with no identifier, or a form we can’t fetch yet. Need a human (upload / identifier / link)." /></div></div>
        {pct != null && <div><b>{pct}%</b><div className="muted">citations resolved <Info t="Share of all citation edges whose target is held in the corpus." /></div></div>}
        <div><b>{(cov?.hanging_references ?? 0).toLocaleString()}</b><div className="muted">hanging total <Info t="Every distinct cited-but-not-held reference, routable or not." /></div></div>
      </div>
      {msg && <p className={msg.startsWith("✗") ? "err" : "ok"} style={{ marginBottom: 4 }}>{msg}</p>}
      {warming && <p className="muted loading-pulse">⏳ Computing the corpus map (scanning the citation graph)… one-off after a restart.</p>}
      {err && <p className="err">{String(err)}</p>}
      {map && !warming && (map.categories || []).length > 0 && (
        <table className="grid map-grid">
          <thead><tr>
            <th>category / sub-type</th>
            <th className="num">held <Info t="Documents of this kind in the corpus." /></th>
            <th className="num">pending <Info t="Routable cited-but-not-held items — one click to harvest." /></th>
            <th className="num">name-only <Info t="Recognised but not auto-fetchable; need a human." /></th>
            <th className="actions-h">actions</th>
          </tr></thead>
          <tbody>
            {(map.categories || []).map((c: any) => {
              const isOpen = open.has(c.key);
              const isEU = c.key === "eu-cellar";
              return (
                <Fragment key={c.key}>
                  <tr className="cat-row">
                    <td>
                      <a className="caret" onClick={() => toggle(c.key)}>{isOpen ? "▾" : "▸"}</a>
                      <b>{c.label}</b>
                    </td>
                    <td className="num">{c.held.toLocaleString()}</td>
                    <td className="num">{c.pending ? c.pending.toLocaleString() : <span className="muted">0</span>}</td>
                    <td className="num">{c.name_only ? c.name_only.toLocaleString() : <span className="muted">0</span>}</td>
                    <td className="actions">
                      {navigate && c.held > 0 && <button className="mini" title="Browse these held documents in the Corpus pane" onClick={() => see({ source: c.key })}>👁 list</button>}
                      {c.pending > 0 && c.key !== "other" && <button className="mini" title={`Harvest ALL ${c.pending} pending routable references in this category (runs in the background, cancellable, skips items that fail)`} onClick={() => fireJob("harvest-all", { adapter: c.key, limit: 1000000 }, setMsg)}>⤓ harvest ({c.pending.toLocaleString()})</button>}
                      {c.key !== "other" && <details className="refresh-menu">
                        <summary className="mini" title="Refresh actions">↻ refresh</summary>
                        <div className="refresh-pop">
                          <button onClick={() => fireJob("rescan-citations", {}, setMsg)} title="Re-extract citations from every document with the latest grammars (global)">re-scan citations</button>
                          {isEU && <button onClick={() => fireJob("expand-citing", { source: "eu-cellar" }, setMsg)} title="Find cases that cite our held EU cases (CELLAR citation graph)">find citing cases</button>}
                          {isEU && <button onClick={() => fireJob("pull-ag-opinions", {}, setMsg)} title="Pull the AG Opinion for every held CJEU judgment that lacks one">pull AG opinions</button>}
                          <button onClick={() => fireJob("backfill-metadata", {}, setMsg)} title="Repair court/title/ruling-only metadata from stored raw (global)">backfill metadata</button>
                        </div>
                      </details>}
                      {c.key !== "other" && <button className="mini" title="Total refresh: harvest this category’s pending references, then (EU) pull citing cases" onClick={() => fireJob("refresh-category", { category: c.key }, setMsg)}>⟳ total</button>}
                    </td>
                  </tr>
                  {isOpen && (c.subtypes || []).map((st: any) => (
                    <tr key={c.key + ":" + st.key} className="sub-row">
                      <td className="sub-label">{st.label}</td>
                      <td className="num">{st.held.toLocaleString()}</td>
                      <td className="num">{st.pending ? st.pending.toLocaleString() : <span className="muted">0</span>}</td>
                      <td className="num">{st.name_only ? st.name_only.toLocaleString() : <span className="muted">0</span>}</td>
                      <td className="actions">
                        {navigate && st.held > 0 && Object.keys(st.filter || {}).length > 0 &&
                          <button className="mini" title="Browse these held documents" onClick={() => see(st.filter)}>👁 list</button>}
                        {st.pending > 0 && c.key === "uk-legislation" &&
                          <button className="mini" title={`Harvest ALL ${st.pending} pending references of this type`} onClick={() => fireJob("harvest-all", { adapter: "uk-legislation", leg_kind: st.key.split(":")[0], limit: 1000000 }, setMsg)}>⤓ harvest ({st.pending.toLocaleString()})</button>}
                      </td>
                    </tr>
                  ))}
                  {isOpen && (
                    <tr className="cites-row"><td colSpan={5}><CitesPanel category={c.key} /></td></tr>
                  )}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      )}
    </div>
  );
}

function ResolveRow({ r, open, active, toggle, onDone }:
  { r: any; open: (id: string) => void; active: boolean; toggle: () => void; onDone: () => void }) {
  const [mode, setMode] = useState(r.needs_identifier ? "identifier" : "existing");
  const [identifier, setIdentifier] = useState("");
  const [jurisdiction, setJurisdiction] = useState(r.jurisdiction || "");
  const [existing, setExisting] = useState("");
  const [url, setUrl] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);

  async function harvest() {
    setBusy(true); setMsg("…");
    try {
      const res = await api.harvestReference(r.ref, r.candidate || undefined);
      if (res.error) setMsg("error: " + res.error);
      else if (res.resolved) { setMsg(`✓ fetched ${res.candidate} · resolved ${res.resolved_edges} edge(s)`); setTimeout(onDone, 700); }
      else if (res.stored) setMsg("fetched but didn't resolve — try Resolve citations");
      else setMsg("not found at the source (may not be published/digitised there) — use ‘other…’ to upload, scrape, or link");
    } catch (e: any) { setMsg("error: " + e); }
    finally { setBusy(false); }
  }

  async function go() {
    setMsg("…");
    try {
      let res: any;
      if (mode === "file" && file)
        res = await api.resolveReferenceFile(r.ref, file, { identifier, jurisdiction });
      else
        res = await api.resolveReference({
          ref: r.ref,
          identifier: mode === "identifier" ? identifier : undefined,
          jurisdiction: mode === "identifier" ? jurisdiction : undefined,
          existing_id: mode === "existing" ? existing : undefined,
          url: mode === "url" ? url : undefined,
        });
      setMsg(res.resolved ? `✓ resolved ${res.resolved_edges} edge(s)` : `re-keyed; still pending (${res.canonical || res.target || "?"})`);
      if (res.resolved) setTimeout(onDone, 600);
    } catch (e: any) { setMsg("error: " + e); }
  }

  return (
    <>
      <tr>
        <td>{r.citing_count}×</td>
        <td><code>{r.raw || r.ref}</code>{r.pinpoint && <span className="muted"> ◆ {r.pinpoint}</span>}</td>
        <td>{r.form}{r.jurisdiction ? ` [${r.jurisdiction}]` : ""}
          {r.confidence === "low" && <span className="err"> · low-confidence</span>}</td>
        <td>{r.suggested_adapter
          ? <><button title={`Fetch this exact item from ${r.suggested_adapter} and resolve`}
              disabled={busy} onClick={harvest}>⤓ {busy ? "harvesting…" : `harvest (${r.suggested_adapter})`}</button>
              {r.bailii_url && (
                <a href={r.bailii_url} target="_blank" rel="noopener noreferrer"
                   title="Right-click → Save As to download the RTF, then use 'other…' to upload it"
                   style={{ fontSize: 11, marginLeft: 8, whiteSpace: "nowrap" }}>↗ BAILII</a>
              )}
              {!active && msg && <span className={msg.startsWith("error") ? "err" : "ok"} style={{ marginLeft: 6 }}>{msg}</span>}</>
          : <span className="err">no adapter</span>}</td>
        <td><button onClick={toggle}>{active ? "close" : "other…"}</button></td>
      </tr>
      {active && (
        <tr><td colSpan={5}>
          <div className="row" style={{ flexWrap: "wrap" }}>
            <select value={mode} onChange={(e) => setMode(e.target.value)} style={{ flex: "0 0 auto" }}>
              <option value="identifier">Supply identifier (neutral citation / ECLI / CELEX)</option>
              <option value="existing">Link to an existing item</option>
              <option value="url">Scrape from a URL</option>
              <option value="file">Upload the source file</option>
              {r.bailii_url && <option value="bailii">Upload BAILII RTF</option>}
            </select>
            {mode === "identifier" && <>
              <input value={identifier} onChange={(e) => setIdentifier(e.target.value)} placeholder="e.g. [2016] EWHC 2768 / ECLI:EU:C:2020:559" />
              <input value={jurisdiction} onChange={(e) => setJurisdiction(e.target.value)} placeholder="jurisdiction" style={{ maxWidth: 130 }} />
            </>}
            {mode === "existing" && <input value={existing} onChange={(e) => setExisting(e.target.value)} placeholder="existing stable_id in the corpus" />}
            {mode === "url" && <input value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://…  (fetched via the scraping engine)" />}
            {mode === "file" && <input type="file" onChange={(e) => setFile(e.target.files?.[0] ?? null)} />}
            {mode === "bailii" && r.bailii_url && (
              <div
                style={{ flex: 1, border: "1px dashed #555", borderRadius: 4, padding: "8px 12px", background: "rgba(255,255,255,0.03)" }}
                onDragOver={(e) => e.preventDefault()}
                onDrop={async (e) => {
                  e.preventDefault();
                  const f = e.dataTransfer.files[0];
                  if (!f) return;
                  setBusy(true); setMsg("importing…");
                  try {
                    const res = await api.importBailii(r.candidate, f);
                    setMsg(`✓ imported ${res.chars} chars · resolved ${res.resolved_edges} edge(s)`);
                    setTimeout(onDone, 700);
                  } catch (err: any) { setMsg("error: " + err.message); }
                  finally { setBusy(false); }
                }}>
                <p className="muted" style={{ margin: "0 0 6px", fontSize: 11 }}>
                  <a href={r.bailii_url} target="_blank" rel="noopener noreferrer">Download the RTF from BAILII ↗</a>
                  {" "}then drag it here, or use the picker:
                </p>
                <input type="file" accept=".rtf"
                  onChange={async (e) => {
                    const f = e.target.files?.[0]; if (!f) return;
                    setBusy(true); setMsg("importing…");
                    try {
                      const res = await api.importBailii(r.candidate, f);
                      setMsg(`✓ imported ${res.chars} chars · resolved ${res.resolved_edges} edge(s)`);
                      setTimeout(onDone, 700);
                    } catch (err: any) { setMsg("error: " + err.message); }
                    finally { setBusy(false); }
                  }} />
              </div>
            )}
            {mode !== "bailii" && <button className="primary" style={{ flex: "0 0 auto" }} onClick={go}>Resolve</button>}
          </div>
          {r.citing_documents?.length > 0 && (
            <p className="muted" style={{ marginTop: 4 }}>cited by: {r.citing_documents.map((d: string) => (
              <a key={d} onClick={() => open(d)} style={{ cursor: "pointer", marginRight: 8 }}>{d}</a>
            ))}</p>
          )}
          {msg && <p className={msg.startsWith("error") ? "err" : "ok"}>{msg}</p>}
        </td></tr>
      )}
    </>
  );
}

// --- Shorthand rules: list / create / delete (propagate across the corpus) --
export function RulesView({ open }: { open: (id: string) => void }) {
  const [rules, _e, reload] = useAsync(() => api.aliases(), []);
  const [phrase, setPhrase] = useState("");
  const [target, setTarget] = useState<{ id: string; title: string } | null>(null);
  const [msg, setMsg] = useState("");
  const create = async () => {
    if (!phrase.trim() || !target) return;
    setMsg("…");
    try {
      await api.createAlias(phrase.trim(), target.id);
      setMsg(`✓ “${phrase.trim()}” → ${target.title}`);
      setPhrase(""); setTarget(null); reload();
    } catch (e: any) { setMsg("error: " + e.message); }
  };
  const apply = async () => {
    setMsg("applying rules across the corpus (re-extracting)…");
    try { const r = await api.applyRules(); setMsg(`✓ re-extracted ${r.documents} docs · ${r.resolved_edges} edges resolved`); }
    catch (e: any) { setMsg("error: " + e.message); }
  };
  return (
    <div>
      <div className="panel">
        <h2 style={{ marginTop: 0 }}>Shorthand rules</h2>
        <p className="muted">A rule links a phrase wherever it appears (e.g. <b>UK GDPR</b> → Assimilated Regulation 2016/679,
          <b> EU GDPR</b> → the original). Rules propagate across the whole corpus on the next extraction. You can also just
          highlight any text while reading a document to make one.</p>
        <div className="row" style={{ alignItems: "flex-end", flexWrap: "wrap" }}>
          <div style={{ flex: "0 0 220px" }}>
            <label className="muted" style={{ fontSize: 11 }}>phrase</label>
            <input value={phrase} onChange={(e) => setPhrase(e.target.value)} placeholder="e.g. UK GDPR" />
          </div>
          <div style={{ flex: 1, minWidth: 280 }}>
            <label className="muted" style={{ fontSize: 11 }}>links to</label>
            {target
              ? <div className="row"><span className="tag">{target.title}</span>
                  <a onClick={() => setTarget(null)} style={{ cursor: "pointer" }}>change</a></div>
              : <DocAutocomplete onPick={(id, title) => setTarget({ id, title })} />}
          </div>
          <button className="primary" style={{ flex: "0 0 auto" }} disabled={!phrase.trim() || !target} onClick={create}>Add rule</button>
        </div>
        {msg && <p className={msg.startsWith("error") ? "err" : "ok"} style={{ fontSize: 12 }}>{msg}</p>}
      </div>
      <div className="panel">
        <div className="row"><h3 style={{ flex: 1 }}>Rules ({(rules || []).length})</h3>
          <button onClick={apply} title="Re-extract the corpus so rules link everywhere">↻ apply to corpus</button></div>
        {(rules || []).length === 0 && <p className="muted">No rules yet.</p>}
        <table><tbody>
          {(rules || []).map((r: any) => (
            <tr key={r.phrase}>
              <td><b>{r.phrase}</b></td>
              <td>→ <a onClick={() => open(r.target_id)} style={{ cursor: "pointer" }}>{r.target_id}</a>
                {!r.target_present && <span className="err" title="target not yet in the corpus"> · not harvested</span>}</td>
              <td><a onClick={async () => { await api.deleteAlias(r.phrase); reload(); }} style={{ cursor: "pointer" }}>✗ delete</a></td>
            </tr>
          ))}
        </tbody></table>
      </div>
    </div>
  );
}

// --- Outstanding amendments (the legislation.gov.uk editorial lag) ----------
function EffectsBanner({ id, open }: { id: string; open: (id: string, a?: string) => void }) {
  const [all, _e, reload] = useAsync(() => api.outstandingEffects(800), [id]);
  const [busy, setBusy] = useState(false);
  if (!all) return null;
  const row = all.find((r: any) => r.stable_id === id);
  if (!row) return null;  // no known unapplied effects → nothing to warn about
  const held = new Set(row.affecting_held || []);
  const harvest = async (aff: string) => {
    setBusy(true);
    try { await api.harvestReference(aff); reload(); } catch { /* ignore */ }
    finally { setBusy(false); }
  };
  return (
    <div className="panel effects-warn">
      <h3 style={{ marginTop: 0 }}>⚠ {row.outstanding} unapplied amendment{row.outstanding === 1 ? "" : "s"}
        <span className="muted"> — legislation.gov.uk knows of changes not yet written into this text</span></h3>
      <p className="muted" style={{ fontSize: 12 }}>
        The published text may be out of date. Next auto re-check: {String(row.next_check_at).slice(0, 10)} (checked {row.checks}×).
        For the law as it stood at a past date, use the point-in-time versions below.
      </p>
      <div>amended by:{" "}
        {(row.affecting || []).map((aff: string) => (
          <span key={aff} className="tag" style={{ marginRight: 6 }}>
            {held.has(aff)
              ? <a onClick={() => open(aff)} style={{ cursor: "pointer" }}>{aff} ✓</a>
              : <>{aff} <a title="fetch this amending instrument" onClick={() => harvest(aff)}
                  style={{ cursor: "pointer" }}>{busy ? "…" : "⤓"}</a></>}
          </span>
        ))}
        {(row.affecting || []).length === 0 && <span className="muted">commencement/other effects (no single amending instrument named)</span>}
      </div>
    </div>
  );
}

// --- What this act changes (affecting side) --------------------------------
function ChangesPanel({ id, open }: { id: string; open: (id: string, a?: string) => void }) {
  const [changes, _e, reload] = useAsync(() => api.legislationChanges(id), [id]);
  const [msg, setMsg] = useState("");
  const scan = async () => {
    setMsg("scanning the Changes-to-Legislation feed…");
    try {
      const r = await api.propagateChanges(id);
      setMsg(`✓ ${r.effects} effect(s); ${r.edges} edge(s); flagged ${r.flagged_for_repull} held act(s) for re-pull`);
      reload();
    } catch (e: any) { setMsg("error: " + e.message); }
  };
  return (
    <div className="panel">
      <div className="row"><h3 style={{ flex: 1, marginTop: 0 }}>Changes this act makes
        <span className="muted"> — instruments it amends (pushed out so they reflect it)</span></h3>
        <button onClick={scan} title="Fetch the affecting-side feed and flag affected acts we hold for re-pull">↻ scan changes</button></div>
      {msg && <p className={msg.startsWith("error") ? "err" : "ok"} style={{ fontSize: 12 }}>{msg}</p>}
      {(changes || []).length === 0 && <p className="muted">none recorded yet — use “scan changes”.</p>}
      {(changes || []).map((c: any, i: number) => (
        <div key={i} style={{ fontSize: 13 }}>
          <a onClick={() => open(c.affected_id)} style={{ cursor: "pointer" }}>{c.affected_title || c.affected_id}</a>
          {c.affected_provision && <span className="muted"> · {c.affected_provision}</span>}
          {c.effect_type && <span className="tag" style={{ marginLeft: 6 }}>{c.effect_type}</span>}
        </div>
      ))}
    </div>
  );
}

// --- Point-in-time legislation versioning ----------------------------------
export function VersionPanel({ id, open }: { id: string; open: (id: string, a?: string) => void }) {
  const [data, _e, reload] = useAsync(() => api.legislationVersions(id), [id]);
  const [date, setDate] = useState("");
  const [msg, setMsg] = useState("");
  const versions = data?.versions || [];
  const fetchAt = async () => {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) { setMsg("error: use YYYY-MM-DD"); return; }
    setMsg(`fetching as at ${date}…`);
    try {
      const r = await api.legislationVersionAt(id, date);
      if (r.error) setMsg("error: " + r.error);
      else { setMsg(`✓ stored ${r.stable_id}`); reload(); }
    } catch (e: any) { setMsg("error: " + e.message); }
  };
  return (
    <div className="panel">
      <h3>Point-in-time versions <span className="muted">— a citing case read the text as it stood then, not today's (possibly repealed) version</span></h3>
      <div className="row" style={{ flexWrap: "wrap", alignItems: "center" }}>
        <input value={date} onChange={(e) => setDate(e.target.value)} placeholder="YYYY-MM-DD" style={{ maxWidth: 150 }} />
        <button onClick={fetchAt}>Show as at this date</button>
        {msg && <span className={msg.startsWith("error") ? "err" : "ok"} style={{ fontSize: 12 }}>{msg}</span>}
      </div>
      {versions.length > 0 && <p className="muted" style={{ marginTop: 6 }}>held versions: {versions.map((v: any) => (
        <a key={v.stable_id} onClick={() => open(v.stable_id)} style={{ cursor: "pointer", marginRight: 10 }}>{v.version_date || v.stable_id}</a>
      ))}</p>}
    </div>
  );
}
