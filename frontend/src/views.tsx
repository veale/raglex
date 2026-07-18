import { createContext, Fragment, lazy, Suspense, useContext, useEffect, useRef, useState } from "react";
import { api, Hit, Setting } from "./api";

// pdf.js is ~700 kB — split it out so it loads only when an original-PDF pane opens
const PdfPane = lazy(() => import("./pdfpane").then((m) => ({ default: m.PdfPane })));
const HtmlPane = lazy(() => import("./pdfpane").then((m) => ({ default: m.HtmlPane })));

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

// --- Tray stack (stacking side "organiser") --------------------------------
// A stack of side trays that offset like bookmarks: opening a link inside a tray pushes
// a new one on top (you still see the ones beneath), each with its own close cross.
type Tray =
  | { kind: "mentions"; target: string; anchor?: string; label: any }
  | { kind: "cites"; target: string; family: "cases" | "statute"; label: any }
  | { kind: "doc"; id: string; highlightTarget?: string; label: any };
const TrayCtx = createContext<{ stack: Tray[]; push: (t: Tray) => void; closeAt: (i: number) => void } | null>(null);
export function useTray() {
  return useContext(TrayCtx) ?? { stack: [] as Tray[], push: (_t: Tray) => {}, closeAt: (_i: number) => {} };
}
export function TrayProvider({ children }: { children: any }) {
  const [stack, setStack] = useState<Tray[]>([]);
  useEffect(() => { document.body.classList.toggle("has-tray", stack.length > 0); }, [stack.length]);
  const push = (t: Tray) => setStack((s) => [...s, t]);
  const closeAt = (i: number) => setStack((s) => s.filter((_, j) => j < i)); // close this + those above it
  return <TrayCtx.Provider value={{ stack, push, closeAt }}>{children}</TrayCtx.Provider>;
}

// The stacked trays themselves — each offset from the last so the ones beneath peek out.
// When the peek column is open the whole stack shifts left of it, so neither hides the other.
export function TrayStack({ open }: { open: (id: string, a?: string) => void }) {
  const { stack, closeAt } = useTray();
  const peek = usePeek();
  if (!stack.length) return null;
  const peekOffset = peek.current ? "400px + " : "";
  return <>{stack.map((t, i) => (
    <aside key={i} className="tray" role="dialog"
      style={{ top: `calc(var(--sp-5) + ${i * 16}px)`, right: `calc(${peekOffset}var(--sp-5) + ${i * 16}px)`,
        zIndex: Math.min(60 + i, 68) }}>
      <div className="tray-head">
        <span className="tray-title">{t.label}</span>
        <button className="tray-x" onClick={() => closeAt(i)} title="close">✕</button>
      </div>
      <div className="tray-body"><TrayContent t={t} open={open} /></div>
    </aside>
  ))}</>;
}

// Escape closes the topmost overlay: the peek first (it renders on top), then the top tray.
export function EscapeCloser() {
  const peek = usePeek();
  const { stack, closeAt } = useTray();
  const ref = useRef({ peek, stack, closeAt });
  ref.current = { peek, stack, closeAt };
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      const tgt = e.target as HTMLElement | null;
      // don't steal Escape from inputs (autocomplete lists close on it)
      if (tgt && /^(INPUT|TEXTAREA|SELECT)$/.test(tgt.tagName)) return;
      const { peek: p, stack: s, closeAt: c } = ref.current;
      if (p.current) p.close();
      else if (s.length) c(s.length - 1);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
  return null;
}

function TrayContent({ t, open }: { t: Tray; open: (id: string, a?: string) => void }) {
  if (t.kind === "doc") return <MentionReader id={t.id} highlightTarget={t.highlightTarget} open={open} />;
  if (t.kind === "cites") return <CitesTray target={t.target} family={t.family} open={open} />;
  return <MentionsTray target={t.target} anchor={t.anchor} open={open} />;
}

// Grouped-by-citer mentions of a document (or one of its paragraphs), most-authoritative
// first — with the passages where each cites it, and a jump to the full citing document.
function MentionsTray({ target, anchor, open }: { target: string; anchor?: string; open: (id: string, a?: string) => void }) {
  const { push } = useTray();
  const [data] = useAsync(() => api.mentions(target, anchor), [target, anchor]);
  if (!data) return <p className="muted loading-pulse">Loading mentions…</p>;
  const groups: any[] = data.groups || [];
  if (!groups.length) return <p className="muted">Nothing mentions this yet.</p>;
  return (
    <div>
      {data.total > groups.length && <p className="muted" style={{ fontSize: 12 }}>{data.total} citing documents · showing {groups.length}</p>}
      {groups.map((g, i) => (
        <div className="mgroup" key={i}>
          <div className="mgroup-head">
            <a className="mgroup-title" title="Open this citing document in a new tray, with its citing passages highlighted"
              onClick={() => push({ kind: "doc", id: g.src_id, highlightTarget: target, label: <Oscola c={g.src_oscola} fallback={g.src_id} /> })}>
              <Oscola c={g.src_oscola} fallback={g.src_id} /></a>
            <button className="mini" title="Open the full document in the main view" onClick={() => open(g.src_id)}>open ↗</button>
          </div>
          {g.snippets.map((s: any, j: number) => (
            <div className="msnip" key={j}>{s.anchor && <span className="msnip-anchor">{s.anchor}</span>}
              <span className="msnip-text">…{s.text}…</span></div>
          ))}
        </div>
      ))}
    </div>
  );
}

// The authorities a document cites (cases | statutory material), OSCOLA-formatted with
// their pinpoints — a resolved one opens in a new tray.
function CitesTray({ target, family, open }: { target: string; family: "cases" | "statute"; open: (id: string, a?: string) => void }) {
  const { push } = useTray();
  const [data] = useAsync(() => api.citationsOut(target, family), [target, family]);
  if (!data) return <p className="muted loading-pulse">Loading…</p>;
  const items: any[] = data.items || [];
  if (!items.length) return <p className="muted">Nothing cited here.</p>;
  return (
    <div>
      {items.map((it, i) => (
        <div className="crow" key={i}>
          <div className="crow-cite">
            {it.resolved_id
              ? <a onClick={() => push({ kind: "doc", id: it.resolved_id, label: <Oscola c={it.oscola} fallback={it.resolved_id} /> })}>
                  <Oscola c={it.oscola} fallback={it.raw || it.candidate} /></a>
              : <span><Oscola c={it.oscola} fallback={it.raw || it.candidate} /> <span className="muted">· not held</span></span>}
            {it.pinpoints?.length > 0 && <span className="crow-pins"> {it.pinpoints.join(", ")}</span>}
          </div>
          {it.resolved_id && <button className="mini" onClick={() => open(it.resolved_id)}>open ↗</button>}
        </div>
      ))}
    </div>
  );
}

// A read-only reader inside a tray, highlighting the paragraphs where the document cites
// the origin document (the "bit linked from"), scrolled to the first.
function MentionReader({ id, highlightTarget, open }: { id: string; highlightTarget?: string; open: (id: string, a?: string) => void }) {
  const [body] = useAsync(() => api.documentBody(id), [id]);
  const peek = usePeek();
  const onCite = (c: any) => peek.push(citePeek(c));
  const segs: any[] = body?.segments || [];
  const cites: any[] = body?.citations || [];
  const hi = new Set<number>();
  if (highlightTarget && body) {
    segs.forEach((s: any, i: number) => {
      if (cites.some((c: any) => c.char_start >= s.char_start && c.char_start < s.char_end && c.resolved_id === highlightTarget))
        hi.add(i);
    });
  }
  useEffect(() => {
    if (!body) return;
    const first = [...hi][0];
    if (first != null) {
      const el = document.getElementById(`tray-${id}-seg-${first}`);
      if (el) setTimeout(() => { el.scrollIntoView({ behavior: "smooth", block: "center" }); }, 80);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [body]);
  if (!body) return <p className="muted loading-pulse">Loading…</p>;
  return (
    <div>
      <div className="tray-doc-head">
        <b><Oscola c={body.oscola} fallback={body.title || id} /></b>
        <button className="mini" onClick={() => open(id)}>open full ↗</button>
      </div>
      {!body.text && <p className="muted">No text (metadata only).</p>}
      <div className="reader">
        {segs.map((s: any, i: number) => {
          const sb = segBody(body.text, s, cites, onCite);
          return (
            <div className={`seg lvl${Math.min(s.level, 2)} kind-${s.kind}${hi.has(i) ? " seg-hi" : ""}`} key={i} id={`tray-${id}-seg-${i}`}>
              {sb.showLabel && <span className="seg-label">{s.label}</span>}
              <span className="seg-body">{sb.body}</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// The inline "Mentioned by A, B, C and n more. See all mentions." line under a paragraph.
function MentionedBy({ list, target, anchor }: { list: any[]; target: string; anchor: string }) {
  const { push } = useTray();
  const top = list.slice(0, 3);
  const more = list.length - top.length;
  return (
    <div className="mentioned-by">
      <span className="mb-label">Mentioned by </span>
      {top.map((m, i) => (
        <Fragment key={i}>{i > 0 && ", "}
          <a title="Open this citing document, with the citing passages highlighted"
            onClick={() => push({ kind: "doc", id: m.src_id, highlightTarget: target, label: <Oscola c={m.src_oscola} fallback={m.src_id} /> })}>
            <Oscola c={m.src_oscola} fallback={m.src_id} /></a>
        </Fragment>
      ))}
      {more > 0 && <span> and {more} more</span>}.{" "}
      <a className="mb-all" onClick={() => push({ kind: "mentions", target, anchor, label: <>Mentions of {anchor}</> })}>See all mentions</a>
    </div>
  );
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

// --- Unified search --------------------------------------------------------
// One page: a fast metadata bar with clever (tokenised, order-free) autocomplete, an
// advanced structured mode whose fields autocomplete, and a faceted results view with
// sorting, grouping, refine tick-boxes and a year histogram.
type Filters = {
  query?: string; source?: string; doc_type?: string; court?: string; tag?: string;
  year_from?: string; year_to?: string; cites?: string; cites_pinpoint?: string; cited_by?: string;
  id_prefix?: string;
};
const PAGE = 50;
const FACET_LABEL: Record<string, string> = { source: "Source", doc_type: "Type", court: "Court" };
const SORTS: [string, string][] = [["date", "Newest"], ["date_asc", "Oldest"], ["title", "Title A–Z"], ["cited", "Most cited"]];
const GROUPS: [string, string][] = [["none", "No grouping"], ["source", "Source"], ["doc_type", "Type"], ["court", "Court"], ["decade", "Decade"]];

const activeFilters = (f: Filters): Record<string, string> => {
  const o: Record<string, string> = {};
  Object.entries(f).forEach(([k, v]) => v && !k.startsWith("_") && (o[k] = String(v)));
  return o;
};

export function SearchView({ open, initialFilter }: { open: (id: string, a?: string) => void; initialFilter?: Record<string, string> }) {
  const [mode, setMode] = useState<"simple" | "advanced">(
    initialFilter && Object.keys(initialFilter).length ? "advanced" : "simple");
  const [filters, setFilters] = useState<Filters>(initialFilter || {});
  const [sort, setSort] = useState("date");
  const [group, setGroup] = useState("none");
  const [page, setPage] = useState(0);
  const [run, setRun] = useState(0);        // bump to (re)run a search
  const [semantic, setSemantic] = useState(false);

  // a Corpus-Map deep-link adopts its filter and searches immediately
  useEffect(() => {
    if (initialFilter && Object.keys(initialFilter).length) {
      setFilters(initialFilter); setMode("advanced"); setPage(0); setRun((r) => r + 1);
    }
  }, [JSON.stringify(initialFilter)]);

  // NB: doesn't touch the semantic toggle — pressing Search re-runs whichever mode is on
  const doSearch = () => { setPage(0); setRun((r) => r + 1); };
  const patch = (p: Partial<Filters>) => { setFilters((f) => ({ ...f, ...p })); setPage(0); setRun((r) => r + 1); };
  const clearAll = () => { setFilters({}); setPage(0); setRun((r) => r + 1); };

  // metadata results + facets (skipped while in semantic mode)
  const [res, err, , loading] = useAsync(
    () => semantic ? Promise.resolve(null)
      : api.searchCorpus({ ...activeFilters(filters), sort, limit: String(PAGE), offset: String(page * PAGE) }),
    [run, sort, page, semantic]);

  // optional semantic (full-text) hits
  const [hits, setHits] = useState<Hit[] | null>(null);
  const [semErr, setSemErr] = useState("");
  useEffect(() => {
    if (!semantic) return;
    if (!(filters.query || "").trim()) { setHits([]); setSemErr(""); return; }
    let live = true;
    setSemErr("");
    const f = activeFilters(filters);
    api.search(filters.query || "", 12, { source: f.source, doc_type: f.doc_type, tag: f.tag, year_from: f.year_from })
      .then((h) => { if (live) setHits(h); })
      .catch((e) => { if (live) { setHits([]); setSemErr(String(e)); } });
    return () => { live = false; };
  }, [run, semantic]);

  const nActive = Object.keys(activeFilters(filters)).length;
  return (
    <div>
      <div className="panel">
        <div className="row" style={{ alignItems: "center", marginBottom: 8 }}>
          <div className="seg-toggle" style={{ flex: "0 0 auto" }}>
            <button className={mode === "simple" ? "on" : ""} onClick={() => setMode("simple")}>Simple</button>
            <button className={mode === "advanced" ? "on" : ""} onClick={() => setMode("advanced")}>Advanced</button>
          </div>
          <span style={{ flex: 1 }} />
          {nActive > 0 && <a className="muted" style={{ cursor: "pointer", fontSize: 12 }} onClick={clearAll}>clear all ✕</a>}
        </div>
        {mode === "simple"
          ? <SimpleBar filters={filters} setQuery={(q) => setFilters((f) => ({ ...f, query: q }))}
              onSearch={doSearch} open={open} semantic={semantic} setSemantic={(v) => { setSemantic(v); if (v) setRun((r) => r + 1); }} />
          : <AdvancedForm filters={filters} setFilters={setFilters} onSearch={doSearch} />}
        {err && <p className="err">{String(err)}</p>}
        {semantic && semErr && <p className="err">{semErr}</p>}
      </div>

      {semantic && hits !== null && <SemanticResults hits={hits} open={open} />}

      {!semantic && res && (
        <div className="search-layout">
          <FacetSidebar facets={res.facets} filters={filters} patch={patch} />
          <div className="search-main">
            <div className="panel">
              <div className="row" style={{ alignItems: "center", justifyContent: "space-between" }}>
                <p className="muted" style={{ margin: 0 }}>
                  {res.total.toLocaleString()} result{res.total === 1 ? "" : "s"}
                  {res.total > PAGE ? ` · ${page * PAGE + 1}–${Math.min((page + 1) * PAGE, res.total)}` : ""}
                  {loading ? " · …" : ""}
                </p>
                <div className="row" style={{ flex: "0 0 auto", gap: 8 }}>
                  <label className="mini-label">sort
                    <select value={sort} onChange={(e) => { setSort(e.target.value); setPage(0); }}>{SORTS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select></label>
                  <label className="mini-label">group
                    <select value={group} onChange={(e) => setGroup(e.target.value)}>{GROUPS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select></label>
                </div>
              </div>
              <ActiveChips filters={filters} patch={(p) => patch(p)} />
              <ResultsList items={res.items} group={group} open={open} />
              {res.total > PAGE && (
                <div className="row" style={{ justifyContent: "center", marginTop: 10 }}>
                  <button disabled={page === 0} onClick={() => setPage((p) => Math.max(0, p - 1))}>‹ prev</button>
                  <span className="muted" style={{ flex: "0 0 auto" }}>page {page + 1} / {Math.ceil(res.total / PAGE)}</span>
                  <button disabled={(page + 1) * PAGE >= res.total} onClick={() => setPage((p) => p + 1)}>next ›</button>
                </div>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// The simple bar: metadata search with an instant, tokenised (order-free) autocomplete of
// matching documents — pick one to open it, or press Enter to search the whole corpus.
function SimpleBar({ filters, setQuery, onSearch, open, semantic, setSemantic }:
  { filters: Filters; setQuery: (q: string) => void; onSearch: () => void; open: (id: string) => void;
    semantic: boolean; setSemantic: (v: boolean) => void }) {
  const q = filters.query || "";
  const [sugg, setSugg] = useState<any[]>([]);
  const [hi, setHi] = useState(-1);
  const [openList, setOpenList] = useState(false);
  useEffect(() => {
    let live = true;
    if (q.trim().length < 2 || semantic) { setSugg([]); return; }
    const t = setTimeout(async () => {
      try {
        const r = await api.searchCorpus({ query: q.trim(), limit: "8", facets: "false" });
        if (live) { setSugg(r.items || []); setHi(-1); setOpenList(true); }
      } catch { /* ignore */ }
    }, 110);
    return () => { live = false; clearTimeout(t); };
  }, [q, semantic]);
  const pick = (o: any) => { if (o) { open(o.stable_id); setOpenList(false); } };
  const search = () => { setOpenList(false); onSearch(); };   // running a search dismisses the dropdown
  return (
    <div>
      <div className="row ac" style={{ position: "relative" }}>
        <input autoFocus value={q} placeholder="Search cases, statutes… (any words, any order)"
          onChange={(e) => { setQuery(e.target.value); }}
          onFocus={() => sugg.length && setOpenList(true)}
          onBlur={() => setTimeout(() => setOpenList(false), 150)}
          onKeyDown={(e) => {
            if (e.key === "ArrowDown") { e.preventDefault(); setHi((h) => Math.min(h + 1, sugg.length - 1)); }
            else if (e.key === "ArrowUp") { e.preventDefault(); setHi((h) => Math.max(h - 1, -1)); }
            else if (e.key === "Enter") { if (hi >= 0 && openList) pick(sugg[hi]); else search(); }
            else if (e.key === "Escape") setOpenList(false);
          }} />
        <button className="primary" style={{ flex: "0 0 auto" }} onClick={search}>Search</button>
        {openList && sugg.length > 0 && (
          <div className="ac-list">
            {sugg.map((o, i) => (
              <div key={o.stable_id} className={`ac-opt${i === hi ? " hi" : ""}`}
                onMouseEnter={() => setHi(i)} onMouseDown={(e) => { e.preventDefault(); pick(o); }}>
                <b><Oscola c={o.oscola} fallback={o.title || o.stable_id} /></b>
                <span className="muted"> · {o.source}/{o.doc_type}{o.court ? " · " + o.court : ""}</span>
              </div>
            ))}
          </div>
        )}
      </div>
      <label className="muted" style={{ display: "inline-flex", alignItems: "center", gap: 6, marginTop: 8, fontSize: 12 }}>
        <input type="checkbox" style={{ width: "auto" }} checked={semantic} onChange={(e) => setSemantic(e.target.checked)} />
        search full text semantically (meaning, not just the words) — for concepts rather than names
      </label>
    </div>
  );
}

// Advanced mode: structured fields, each autocompleting (free text does not). Makes full
// use of the metadata — source/type/court/year plus the graph (cites / cited by, with a
// pinpoint autocomplete for the cited provision).
function AdvancedForm({ filters, setFilters, onSearch }:
  { filters: Filters; setFilters: (f: (p: Filters) => Filters) => void; onSearch: () => void }) {
  const [fv] = useAsync(() => api.facetValues(), []);
  const set = (k: keyof Filters, v: string) => setFilters((f) => ({ ...f, [k]: v || undefined }));
  const opts = (rows: any[]) => (rows || []).map((r) => <option key={r.key} value={r.key}>{r.key} ({r.n.toLocaleString()})</option>);
  return (
    <div className="adv-form">
      <div className="adv-row">
        <label>Title / id contains <span className="muted">(free text — words in any order)</span></label>
        <input value={filters.query || ""} placeholder="e.g. data protection erasure"
          onChange={(e) => set("query", e.target.value)} onKeyDown={(e) => e.key === "Enter" && onSearch()} />
      </div>
      <div className="adv-grid">
        <div><label>Source</label>
          <select value={filters.source || ""} onChange={(e) => set("source", e.target.value)}>
            <option value="">any</option>{opts(fv?.sources)}</select></div>
        <div><label>Type</label>
          <select value={filters.doc_type || ""} onChange={(e) => set("doc_type", e.target.value)}>
            <option value="">any</option>{opts(fv?.doc_types)}</select></div>
        <div><label>Court</label>
          <ComboBox value={filters.court || ""} onChange={(v) => set("court", v)}
            options={(fv?.courts || []).map((c: any) => c.key)} placeholder="any court" /></div>
        <div><label>Tag / collection</label>
          <select value={filters.tag || ""} onChange={(e) => set("tag", e.target.value)}>
            <option value="">any</option>{opts(fv?.tags)}</select></div>
        <div><label>Year from</label>
          <input type="number" min={1200} max={2100} value={filters.year_from || ""} placeholder="e.g. 2016"
            onChange={(e) => set("year_from", e.target.value)} /></div>
        <div><label>Year to</label>
          <input type="number" min={1200} max={2100} value={filters.year_to || ""} placeholder="e.g. 2024"
            onChange={(e) => set("year_to", e.target.value)} /></div>
      </div>
      <div className="adv-grid">
        <div className="adv-cites"><label>Cites <span className="muted">— documents that cite…</span></label>
          <CiteTargetField value={filters.cites} pinpoint={filters.cites_pinpoint}
            onChange={(id, pin) => setFilters((f) => ({ ...f, cites: id, cites_pinpoint: pin }))} /></div>
        <div className="adv-cites"><label>Cited by <span className="muted">— documents cited by…</span></label>
          <CiteTargetField value={filters.cited_by} onChange={(id) => setFilters((f) => ({ ...f, cited_by: id }))} /></div>
      </div>
      <div className="row" style={{ marginTop: 10 }}>
        <button className="primary" style={{ flex: "0 0 auto" }} onClick={onSearch}>Search</button>
      </div>
    </div>
  );
}

// A pick-a-document field (name autocomplete) with an optional pinpoint (section/article of
// the target) — reuses the reader's LinkTargetPicker autocomplete pattern.
function CiteTargetField({ value, pinpoint, onChange }:
  { value?: string; pinpoint?: string; onChange: (id: string | undefined, pin?: string) => void }) {
  const [picked, setPicked] = useState<{ id: string; title: string } | null>(value ? { id: value, title: value } : null);
  const [labels, setLabels] = useState<string[]>([]);
  useEffect(() => {
    if (!picked) return;
    let live = true;
    api.documentBody(picked.id).then((b) => { if (live) setLabels([...new Set((b.segments || []).map((s: any) => s.label).filter(Boolean))] as string[]); }).catch(() => {});
    return () => { live = false; };
  }, [picked?.id]);
  if (!picked) return <DocAutocomplete onPick={(id, title) => { setPicked({ id, title }); onChange(id); }} placeholder="find a case or act…" />;
  return (
    <div>
      <div className="row" style={{ gap: 6 }}>
        <span className="tag" style={{ flex: 1 }}>{picked.title}</span>
        <a className="muted" style={{ cursor: "pointer", flex: "0 0 auto" }} onClick={() => { setPicked(null); onChange(undefined, undefined); }}>change</a>
      </div>
      {pinpoint !== undefined && (
        <div style={{ marginTop: 4 }}>
          <input list={`pin-${picked.id}`} defaultValue={pinpoint || ""} placeholder="pinpoint — section / article (optional)"
            onChange={(e) => onChange(picked.id, e.target.value || undefined)} />
          <datalist id={`pin-${picked.id}`}>{labels.map((l, i) => <option key={i} value={l} />)}</datalist>
        </div>
      )}
    </div>
  );
}

// A lightweight combobox: type to filter a fixed option list, choose one (for Court).
function ComboBox({ value, onChange, options, placeholder }:
  { value: string; onChange: (v: string) => void; options: string[]; placeholder?: string }) {
  const [q, setQ] = useState(value);
  const [openL, setOpenL] = useState(false);
  useEffect(() => { setQ(value); }, [value]);
  const ql = q.toLowerCase();
  const matches = q ? options.filter((o) => o.toLowerCase().includes(ql)).slice(0, 12) : options.slice(0, 12);
  return (
    <div className="ac" style={{ position: "relative" }}>
      <input value={q} placeholder={placeholder}
        onChange={(e) => { setQ(e.target.value); setOpenL(true); if (!e.target.value) onChange(""); }}
        onFocus={() => setOpenL(true)} onBlur={() => setTimeout(() => setOpenL(false), 150)} />
      {openL && matches.length > 0 && (
        <div className="ac-list">
          {matches.map((o) => (
            <div key={o} className="ac-opt" onMouseDown={(e) => { e.preventDefault(); onChange(o); setQ(o); setOpenL(false); }}>{o}</div>
          ))}
        </div>
      )}
    </div>
  );
}

// The chips summarising active filters (each removable) above the results.
function ActiveChips({ filters, patch }: { filters: Filters; patch: (p: Partial<Filters>) => void }) {
  const entries = Object.entries(activeFilters(filters)).filter(([k]) => k !== "query");
  if (!entries.length) return null;
  const label: Record<string, string> = { source: "source", doc_type: "type", court: "court", tag: "tag",
    year_from: "from", year_to: "to", cites: "cites", cites_pinpoint: "cites ¶", cited_by: "cited by", id_prefix: "id" };
  return (
    <div className="active-chips">
      {entries.map(([k, v]) => (
        <span className="filter-chip" key={k}>{label[k] || k}: {v}
          <a onClick={() => patch({ [k]: undefined } as any)} title="remove"> ✕</a></span>
      ))}
    </div>
  );
}

// Left refine sidebar: a year histogram + tick-box facet groups, each value with its count.
function FacetSidebar({ facets, filters, patch }:
  { facets: any; filters: Filters; patch: (p: Partial<Filters>) => void }) {
  if (!facets) return null;
  return (
    <aside className="facets panel">
      <YearHistogram year={facets.year || {}} from={filters.year_from} to={filters.year_to}
        onPick={(y) => patch({ year_from: y, year_to: y })} onClear={() => patch({ year_from: undefined, year_to: undefined })} />
      {(["source", "doc_type", "court"] as const).map((dim) => (
        <FacetGroup key={dim} title={FACET_LABEL[dim]} values={facets[dim] || []}
          active={(filters as any)[dim]} onPick={(k) => patch({ [dim]: (filters as any)[dim] === k ? undefined : k } as any)} />
      ))}
    </aside>
  );
}

function FacetGroup({ title, values, active, onPick }:
  { title: string; values: any[]; active?: string; onPick: (k: string) => void }) {
  const [all, setAll] = useState(false);
  if (!values.length) return null;
  const shown = all ? values : values.slice(0, 8);
  return (
    <div className="facet-group">
      <div className="facet-title">{title}</div>
      {shown.map((v) => (
        <label key={v.key} className={`facet-row${active === v.key ? " on" : ""}`}>
          <input type="checkbox" checked={active === v.key} onChange={() => onPick(v.key)} />
          <span className="facet-name" title={v.key}>{v.key}</span>
          <span className="facet-count">{v.n.toLocaleString()}</span>
        </label>
      ))}
      {values.length > 8 && <a className="facet-more" onClick={() => setAll((a) => !a)}>{all ? "less" : `+${values.length - 8} more`}</a>}
    </div>
  );
}

// A compact year-distribution histogram; click a bar to filter to that year.
function YearHistogram({ year, from, to, onPick, onClear }:
  { year: Record<string, number>; from?: string; to?: string; onPick: (y: string) => void; onClear: () => void }) {
  const years = Object.keys(year).filter((y) => /^\d{4}$/.test(y)).sort();
  if (years.length < 2) return null;
  const max = Math.max(...years.map((y) => year[y]));
  const lo = years[0], hi = years[years.length - 1];
  return (
    <div className="facet-group">
      <div className="facet-title">Year {(from || to) && <a className="facet-more" onClick={onClear}>clear</a>}</div>
      <div className="histo" title="click a bar to filter to that year">
        {years.map((y) => {
          const on = from === to && from === y;
          return <div key={y} className={`histo-bar${on ? " on" : ""}`} style={{ height: `${Math.max(3, (year[y] / max) * 40)}px` }}
            title={`${y}: ${year[y].toLocaleString()}`} onClick={() => onPick(y)} />;
        })}
      </div>
      <div className="histo-axis"><span>{lo}</span><span>{hi}</span></div>
    </div>
  );
}

// One results list, optionally grouped, each row an OSCOLA citation + metadata.
function ResultsList({ items, group, open }: { items: any[]; group: string; open: (id: string, a?: string) => void }) {
  if (!items.length) return <p className="muted" style={{ marginTop: 8 }}>No matches. Loosen a filter, or try the semantic toggle for concepts.</p>;
  const keyFor = (d: any): string => {
    if (group === "source") return d.source || "—";
    if (group === "doc_type") return d.doc_type || "—";
    if (group === "court") return d.court || "—";
    if (group === "decade") { const y = (d.decision_date || "").slice(0, 4); return y ? y.slice(0, 3) + "0s" : "undated"; }
    return "";
  };
  const row = (d: any) => (
    <div className="result-row" key={d.stable_id}>
      <a className="result-cite" onClick={() => open(d.stable_id)}><Oscola c={d.oscola} fallback={d.title || d.stable_id} /></a>
      <div className="result-meta muted">
        <span className="tag">{d.doc_type}</span>
        {d.court && <span> · {d.court}</span>}
        {d.decision_date && <span> · {String(d.decision_date).slice(0, 10)}</span>}
        {d.cited_by > 0 && <span> · cited by {d.cited_by.toLocaleString()}</span>}
        {d.source && <span> · {d.source}</span>}
      </div>
    </div>
  );
  if (group === "none") return <div className="results">{items.map(row)}</div>;
  const groups: Record<string, any[]> = {};
  for (const d of items) (groups[keyFor(d)] ||= []).push(d);
  return (
    <div className="results">
      {Object.entries(groups).map(([g, rows]) => (
        <div key={g} className="result-group">
          <div className="result-group-head">{g} <span className="muted">({rows.length})</span></div>
          {rows.map(row)}
        </div>
      ))}
    </div>
  );
}

// Semantic (full-text) hits — meaning-based; kept from the old hybrid search.
function SemanticResults({ hits, open }: { hits: Hit[]; open: (id: string) => void }) {
  return (
    <div className="panel">
      <p className="muted">{hits.length} result{hits.length === 1 ? "" : "s"} · keyword + semantic, fused (RRF), with graph neighbours</p>
      {hits.length === 0 && <p className="muted">No matches. Try fewer filters, or embed first (Dashboard → Embed pending).</p>}
      {hits.map((h, i) => (
        <div className="hit" key={i}>
          <div><a onClick={() => open(h.doc_id)}>{h.ecli || h.title || h.doc_id}</a>{" "}
            <span className="muted">· {h.source}/{h.court} · {h.structural_unit} · score {h.score.toFixed(4)}</span></div>
          <div className="snippet">{h.chunk_text.slice(0, 300)}</div>
          {h.neighbours.length > 0 && (
            <div className="nbr">graph: {h.neighbours.slice(0, 3).map((n, j) =>
              <span key={j}>{n.direction === "out" ? "→" : "←"} {n.relationship_type} <a onClick={() => open(n.id)}>{n.id}</a>; </span>)}</div>
          )}
        </div>
      ))}
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

// The leading paragraph number of a segment label ("43." / "[43]" → "43"), when the label
// is a bare number (not a named header like "Article 17" or "ruling").
function labelNum(label: string): string | null {
  const m = /^\[?(\d{1,4})[.\]\)]?$/.exec((label || "").trim());
  return m ? m[1] : null;
}

// Canonical key for a pinpoint/segment so a citation anchor ("Article 4") matches the
// segment that carries it even when the segment label also has the heading text ("Article 4
// Definitions"). Typed (art/rec/s/…) so "Recital 5" and "Article 5" never collide; a bare
// number ("1." / "[12]") stays number-only so judgment paragraphs still match.
const _ANCHOR_TYPE: Record<string, string> = {
  article: "art", art: "art", recital: "rec", rec: "rec", section: "s", sec: "s", s: "s",
  schedule: "sch", sch: "sch", paragraph: "para", para: "para", regulation: "reg", reg: "reg",
  rule: "rule", point: "pt", pt: "pt", annex: "annex",
};
function anchorKey(text: string): string | null {
  const t = (text || "").trim().toLowerCase().replace(/^[[(]/, "");
  const m = /^([a-z]+)?\.?\s*(\d+[a-z]?)/.exec(t);
  if (!m || !m[2]) return null;
  const typ = m[1] ? _ANCHOR_TYPE[m[1]] : "";
  return typ ? `${typ}:${m[2]}` : m[2];
}

// Render one segment's body, de-duplicating the paragraph number: judgments store the
// number both as a label AND at the head of the prose ("1. This is an appeal…"). When the
// prose already carries it, we drop the separate label and style the inline number instead
// (greeny-blue, bold, in flow) so the text reads without a repeated, orphaned number.
function segBody(text: string, s: { label: string; char_start: number; char_end: number },
                 cites: any[], onCite: (c: any) => void, paraSet?: Set<string>, onPara?: (n: string) => void) {
  const num = labelNum(s.label);
  const raw = text.slice(s.char_start, s.char_end);
  const m = num ? new RegExp(`^(\\s*)(${num})([.)\\]]?)(\\s+)`).exec(raw) : null;
  if (!m) return { showLabel: true, body: renderCited(text, s.char_start, s.char_end, cites, onCite, paraSet, onPara) };
  const numEnd = s.char_start + m[0].length;
  return {
    showLabel: false,
    body: <>
      <b className="seg-num">{m[2]}{m[3]}</b>{" "}
      {renderCited(text, numEnd, s.char_end, cites, onCite, paraSet, onPara)}
    </>,
  };
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
        <b><Oscola c={(doc as any)?.oscola} fallback={d?.title || id} /></b>
        <div className="muted" style={{ fontSize: 12 }}>{d?.court}{d?.decision_date ? " · " + String(d.decision_date).slice(0, 10) : ""}
          {doc?.cited_by_count ? ` · cited by ${doc.cited_by_count}` : ""}{anchor ? ` · ${anchor}` : ""}</div>
        <button style={{ marginTop: 4 }} onClick={() => openFull(id, anchor)}>open full ↗</button>
      </div>
      {!body?.text && doc && <p className="muted">No text yet (metadata only).</p>}
      {body?.text && segs.length > 0 && (
        <div className="reader">
          {segs.map((s, i) => {
            const sb = segBody(body.text, s, cites, onCite);
            return (
            <div className={`seg lvl${Math.min(s.level, 2)} kind-${s.kind}`} key={i} id={"peek-seg-" + i}>
              {sb.showLabel && <span className="seg-label">{s.label}</span>}
              <span className="seg-body">{sb.body}</span>
            </div>
            );
          })}
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

// Structural segment kinds that read as headings — the spine of the left-rail index.
const _HEADING_KINDS = new Set(["section", "article", "chapter", "part", "title", "heading",
  "subheading", "crossheading", "division", "schedule"]);
function isHeading(s: { kind: string; level: number; label: string }): boolean {
  if (_HEADING_KINDS.has(s.kind)) return true;
  // a level-0 segment whose label isn't a bare paragraph number is a heading
  return s.level === 0 && s.kind !== "paragraph" && !/^\[?\d/.test((s.label || "").trim());
}

// The left rail: the document's OSCOLA title (sticky), a link to the original, a
// case-insensitive "find in document" box, and a heading index for navigation.
function DocNav({ segs, text, oscola, title, landingUrl, id }:
  { segs: any[]; text: string; oscola?: OscolaCite | null; title?: string; landingUrl?: string; id: string }) {
  const [q, setQ] = useState("");
  const [at, setAt] = useState(0);
  const headings = segs.map((s: any, i: number) => ({ s, i })).filter(({ s }) => isHeading(s));
  const query = q.trim().toLowerCase();
  const matches = query
    ? segs.map((_s: any, i: number) => i).filter((i: number) =>
        text.slice(segs[i].char_start, segs[i].char_end).toLowerCase().includes(query))
    : [];
  const jump = (i: number) => scrollToSeg(segId(segs[i].label));
  const step = (dir: number) => {
    if (!matches.length) return;
    const n = (at + dir + matches.length) % matches.length;
    setAt(n); jump(matches[n]);
  };
  useEffect(() => { setAt(0); if (matches.length) jump(matches[0]); /* eslint-disable-next-line */ }, [query]);
  return (
    <nav className="doc-nav">
      <div className="doc-nav-title" title={title}><Oscola c={oscola} fallback={title || id} /></div>
      {landingUrl && <a className="doc-nav-orig" href={landingUrl} target="_blank" rel="noreferrer">link to original ↗</a>}
      <div className="doc-nav-find">
        <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Find in document"
          onKeyDown={(e) => { if (e.key === "Enter") step(e.shiftKey ? -1 : 1); }} />
        {query && <div className="doc-nav-find-n">
          {matches.length ? <>{at + 1}/{matches.length}
            <a onClick={() => step(-1)} title="previous"> ‹</a><a onClick={() => step(1)} title="next"> ›</a></>
            : "no matches"}</div>}
      </div>
      {headings.length > 0 && (
        <ol className="doc-nav-index">
          {headings.map(({ s, i }) => (
            <li key={i} className={`nav-lvl${Math.min(s.level, 2)}`}>
              <a onClick={() => jump(i)} title={s.label}>{s.label}</a>
            </li>
          ))}
        </ol>
      )}
    </nav>
  );
}

// --- Structured reader (legislation hierarchy / judgment paragraphs) -------
function Reader({ id, incoming, pinpoint, oscola, landingUrl, title }:
  { id: string; incoming: any[]; pinpoint?: string | null; oscola?: OscolaCite | null; landingUrl?: string; title?: string }) {
  const [body] = useAsync(() => api.documentBody(id), [id]);
  // "original" pane: the stored source file (guidance PDF via the linkified pdf.js
  // viewer, styled BAILII HTML in a sandboxed frame) alongside the extracted text
  const rawKind = body?.raw_ext === "pdf" ? "pdf"
    : body?.raw_ext === "html" || body?.raw_ext === "htm" ? "html" : null;
  const [view, setView] = useState<"text" | "orig">("text");
  useEffect(() => { setView(body && !body.text && rawKind ? "orig" : "text"); }, [id, !body]);
  // per-paragraph "mentioned by" roll-up (who cites each paragraph, most-authoritative first).
  // Index it by a canonical anchor key so a citation to "Article 4" matches the segment whose
  // label is "Article 4 Definitions"; keep the real citation anchor for the "see all" filter.
  const [mentions] = useAsync(() => api.mentions(id), [id]);
  const byAnchor: Record<string, { anchor: string; list: any[] }> = {};
  for (const [k, list] of Object.entries((mentions?.by_anchor || {}) as Record<string, any[]>)) {
    const ck = anchorKey(k);
    if (!ck) continue;
    const cur = byAnchor[ck] || (byAnchor[ck] = { anchor: k, list: [] });
    const seen = new Set(cur.list.map((m: any) => m.src_id));
    cur.list.push(...list.filter((m: any) => !seen.has(m.src_id)));
  }
  const mentionsFor = (label: string) => { const ck = anchorKey(label); return ck ? byAnchor[ck] : undefined; };
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
  if (!body.text && !rawKind) return (
    <div>
      {body.external_pdf && (
        <div className="pdf-stub-banner">
          📄 No text transcript — the original judgment is a PDF on BAILII.{" "}
          <a href={body.external_pdf} target="_blank" rel="noopener noreferrer">Open the PDF on BAILII ↗</a>
        </div>
      )}
      <p className="muted">No extracted text (metadata-only, or not yet extracted).</p>
    </div>
  );
  const segs = body.segments as { label: string; kind: string; level: number; char_start: number; char_end: number }[];
  const cites = body.citations || [];
  const pinned = (label: string) => (incoming || []).filter((r) => r.dst_anchor === label);
  const content = !body.text ? null : (!segs || segs.length === 0)
    ? <div className="reader"><div className="seg"><div className="seg-body">{renderCited(body.text, 0, body.text.length, cites, onCite, paraSet, onPara)}</div></div></div>
    : (
      <div className="reader">
        {segs.map((s, i) => {
          const sb = segBody(body.text, s, cites, onCite, paraSet, onPara);
          return (
          <div className={`seg lvl${Math.min(s.level, 2)} kind-${s.kind}`} key={i} id={segId(s.label)}>
            <a className="seg-plus" title="Link commentary or an authority to this paragraph"
              onClick={() => peek.push({ kind: "augment", docId: id, anchor: s.label })}>＋</a>
            {sb.showLabel && <span className="seg-label">{s.label}</span>}
            <span className="seg-body">{sb.body}</span>
            {pinned(s.label).map((r, j) => (
              <div className="pinned" key={j}>💬 {r.relationship_type}: <a onClick={() => peek.push({ kind: "doc", id: r.src_id })}>{r.src_title || r.src_id}</a>
                {r.src_anchor && <span className="muted"> ({r.src_anchor})</span>}</div>
            ))}
            {(() => { const mb = mentionsFor(s.label); return mb && mb.list.length > 0
              ? <MentionedBy list={mb.list} target={id} anchor={mb.anchor} /> : null; })()}
          </div>
          );
        })}
      </div>
    );
  const chips = body.doc_type === "guidance" && <GuidanceChips id={id} />;
  // BAILII PDF-only stub: no transcript here, but the original PDF lives on bailii.org.
  // Surface it as a real clickable link (the sandboxed original pane can't open links).
  const pdfBanner = body.external_pdf && (
    <div className="pdf-stub-banner">
      📄 This judgment has no text transcript on BAILII — only the original PDF.{" "}
      <a href={body.external_pdf} target="_blank" rel="noopener noreferrer">Open the PDF on BAILII ↗</a>
      {body.source_url && <> · <a href={body.source_url} target="_blank" rel="noopener noreferrer" className="muted">source page</a></>}
    </div>
  );
  const tabs = rawKind && (
    <div className="viewtabs">
      <button className={`mini${view === "text" ? " on" : ""}`} disabled={!body.text}
        onClick={() => setView("text")}>text</button>
      <button className={`mini${view === "orig" ? " on" : ""}`}
        title={rawKind === "pdf" ? "the original PDF, with citations linked on the page" : "the original page as saved"}
        onClick={() => setView("orig")}>original ({rawKind})</button>
    </div>
  );
  const main = view === "orig" && rawKind
    ? <Suspense fallback={<p className="muted loading-pulse">loading viewer…</p>}>
        {rawKind === "pdf" ? <PdfPane id={id} onCite={onCite} /> : <HtmlPane id={id} />}
      </Suspense>
    : content;
  return (
    <SelectionShorthand docId={id}>
      <div className="doc-layout">
        <DocNav segs={segs || []} text={body.text || ""} oscola={oscola} title={title} landingUrl={landingUrl} id={id} />
        <div className="doc-main">{chips}{pdfBanner}{tabs}{main}</div>
      </div>
    </SelectionShorthand>
  );
}

// Classification chips on a guidance document: each field shows its value with the
// rule that set it (hover = the matched text); click to correct — corrections are
// `manual` and survive every re-classify. The inspectable face of guidance sorting.
function GuidanceChips({ id }: { id: string }) {
  const [g, setG] = useState<any>(null);
  const [edit, setEdit] = useState<string | null>(null);
  const [val, setVal] = useState("");
  useEffect(() => {
    let live = true;
    api.document(id).then((d) => { if (live) setG((d.meta || {}).guidance || {}); }).catch(() => {});
    return () => { live = false; };
  }, [id]);
  if (!g) return null;
  const FIELDS = ["issuer", "number", "version", "status", "adopted_date", "regime"];
  const save = async (field: string) => {
    try {
      const r = await api.setGuidanceField(id, field, val.trim() || null);
      setG(r.guidance); setEdit(null);
    } catch { /* leave the editor open */ }
  };
  return (
    <div className="gchips">
      {FIELDS.map((f) => {
        const v = g[f];
        if (edit === f) {
          return (
            <span className="gchip editing" key={f}>
              {f}: <input autoFocus value={val} onChange={(e) => setVal(e.target.value)}
                onKeyDown={(e) => { if (e.key === "Enter") save(f); if (e.key === "Escape") setEdit(null); }}
                style={{ width: 130 }} />
              <a onClick={() => save(f)} title="save">✓</a>
            </span>
          );
        }
        return (
          <span key={f} className={`gchip${v ? ` m-${v.method}` : " empty"}`}
            title={v ? `${v.method === "manual" ? "set by you" : `rule: ${v.rule}`}${v.evidence ? `\nmatched: ${v.evidence}` : ""}\nclick to edit` : `${f} not classified — click to set`}
            onClick={() => { setEdit(f); setVal(v?.value || ""); }}>
            <span className="muted">{f}</span> {v?.value || "—"}
            {v?.method === "manual" && <span title="set manually — re-classify never overwrites"> ✎</span>}
          </span>
        );
      })}
    </div>
  );
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
// Pick a target case/act (name autocomplete), then optionally a pinpoint WITHIN it — a
// paragraph, article, section, schedule or recital — autocompleted from the target's own
// structure (its segment labels). Used by the highlight-to-link popover.
function LinkTargetPicker({ initial, onCreate }:
  { initial: string; onCreate: (id: string, title: string, pinpoint?: string) => void }) {
  const [target, setTarget] = useState<{ id: string; title: string } | null>(null);
  const [pin, setPin] = useState("");
  const [labels, setLabels] = useState<string[]>([]);
  useEffect(() => {
    if (!target) return;
    let live = true;
    api.documentBody(target.id)
      .then((b) => { if (live) setLabels([...new Set((b.segments || []).map((s: any) => s.label).filter(Boolean))] as string[]); })
      .catch(() => {});
    return () => { live = false; };
  }, [target?.id]);
  if (!target) return <DocAutocomplete initial={initial} onPick={(id, title) => setTarget({ id, title })} />;
  return (
    <div style={{ minWidth: 300 }}>
      <div className="muted" style={{ fontSize: 11, marginBottom: 4 }}>→ <b>{target.title}</b>{" "}
        <a onClick={() => { setTarget(null); setPin(""); }} style={{ cursor: "pointer" }}>change</a></div>
      <div className="row">
        <input list="pinpoint-list" value={pin} onChange={(e) => setPin(e.target.value)} autoFocus
          placeholder="pinpoint — paragraph / article / section (optional)" />
        <datalist id="pinpoint-list">{labels.map((l, i) => <option key={i} value={l} />)}</datalist>
        <button className="primary" style={{ flex: "0 0 auto" }}
          onClick={() => onCreate(target.id, target.title, pin.trim() || undefined)}>Link</button>
      </div>
    </div>
  );
}

type SelInfo = {
  text: string; x: number; y: number;
  anchor: string | null;           // enclosing segment's label, when the selection is in one
  context: string;                 // the enclosing segment's text (truncated)
  links: { text: string; state: string; title: string | null }[];  // citations linked in that segment NOW
};

function SelectionShorthand({ children, docId }: { children: any; docId?: string }) {
  const ref = useRef<HTMLDivElement>(null);
  const [sel, setSel] = useState<SelInfo | null>(null);
  const [mode, setMode] = useState<"menu" | "link" | "flag">("menu");
  const [note, setNote] = useState("");
  const [msg, setMsg] = useState("");
  useEffect(() => {
    function onUp(e: MouseEvent) {
      if ((e.target as HTMLElement)?.closest?.(".sel-pop")) return;  // clicking inside our popover
      const s = window.getSelection();
      const text = s?.toString().trim() || "";
      if (!text || text.length > 140 || !ref.current || !s?.anchorNode || !ref.current.contains(s.anchorNode)) {
        setSel(null); setMode("menu"); return;
      }
      const rect = s.getRangeAt(0).getBoundingClientRect();
      // capture where the selection sits and what its segment links to right now —
      // the evidence a "flag for improved refinement" needs to be reviewable later.
      const node = s.anchorNode instanceof Element ? s.anchorNode : s.anchorNode.parentElement;
      const seg = node?.closest?.(".seg") as HTMLElement | null;
      const links = seg ? Array.from(seg.querySelectorAll("a.cite")).map((a) => ({
        text: a.textContent || "",
        state: (a.className.match(/cite-(\w+)/) || [])[1] || "",
        title: a.getAttribute("title"),
      })) : [];
      const anchor = seg?.querySelector(".seg-label")?.textContent
        || seg?.querySelector(".seg-num")?.textContent || null;
      setSel({ text, x: rect.left + rect.width / 2, y: rect.bottom, anchor,
               context: (seg?.textContent || "").slice(0, 600), links });
      setMode("menu"); setMsg(""); setNote("");
    }
    document.addEventListener("mouseup", onUp);
    return () => document.removeEventListener("mouseup", onUp);
  }, []);
  const dismiss = (delay = 2400) =>
    setTimeout(() => { setSel(null); setMsg(""); setMode("menu"); window.getSelection()?.removeAllRanges(); }, delay);
  const create = async (id: string, title: string, pinpoint?: string) => {
    if (!sel) return;
    try {
      // the phrase → target shorthand propagates across the corpus…
      await api.createAlias(sel.text, id);
      // …and when a pinpoint is chosen, record a fragment link from THIS passage to the
      // target's paragraph/article/section as well.
      if (pinpoint && docId) {
        try { await api.link(docId, id, "mentions", sel.text.slice(0, 120), pinpoint); } catch { /* non-fatal */ }
      }
      setMsg(`✓ linked “${sel.text}” → ${title}${pinpoint ? " · " + pinpoint : ""}`);
    } catch (e: any) { setMsg("error: " + e.message); }
    setMode("menu");
    dismiss();
  };
  const flag = async () => {
    if (!sel || !docId) return;
    try {
      await api.flagRefinement({
        doc_id: docId, selected_text: sel.text, anchor: sel.anchor, context: sel.context,
        current_links: JSON.stringify(sel.links), note: note.trim() || undefined,
      });
      setMsg("✓ flagged for refinement — see Maintain");
    } catch (e: any) { setMsg("error: " + e.message); }
    setMode("menu");
    dismiss();
  };
  return (
    <div ref={ref} style={{ position: "relative" }}>
      {children}
      {sel && <div className="sel-pop" style={{ position: "fixed",
        left: Math.min(Math.max(sel.x, 180), window.innerWidth - 180),
        top: Math.min(sel.y + 6, window.innerHeight - 170),
        transform: "translateX(-50%)" }}>
        {msg ? <span className={msg.startsWith("error") ? "err" : "ok"} style={{ fontSize: 12 }}>{msg}</span>
          : mode === "menu" ? (
            <div className="row" style={{ gap: 6, flexWrap: "nowrap" }}>
              <button style={{ flex: "0 0 auto" }} onClick={() => setMode("link")}>
                🔖 Link “{sel.text.length > 24 ? sel.text.slice(0, 24) + "…" : sel.text}” to…</button>
              <button style={{ flex: "0 0 auto" }} title="Record this passage as badly linked/refined — with its location and what it links to now — for a later pass over the linking logic"
                onClick={() => setMode("flag")}>⚑ Flag for improved refinement</button>
            </div>
          ) : mode === "link" ? (
            <div style={{ minWidth: 320 }}>
              <div className="muted" style={{ fontSize: 11, marginBottom: 4 }}>“{sel.text}” links to a case / act (and, optionally, a part of it):</div>
              <LinkTargetPicker initial={sel.text} onCreate={create} />
            </div>
          ) : (
            <div style={{ minWidth: 340 }}>
              <div className="muted" style={{ fontSize: 11, marginBottom: 4 }}>
                Flag “{sel.text.length > 40 ? sel.text.slice(0, 40) + "…" : sel.text}”
                {sel.anchor ? ` (at ${sel.anchor})` : ""} · {sel.links.length} link(s) in this passage recorded
              </div>
              <div className="row" style={{ gap: 6 }}>
                <input autoFocus value={note} onChange={(e) => setNote(e.target.value)}
                  placeholder="what should it do instead? (optional)"
                  onKeyDown={(e) => e.key === "Enter" && flag()} />
                <button className="primary" style={{ flex: "0 0 auto" }} onClick={flag}>⚑ Flag</button>
              </div>
            </div>
          )}
      </div>}
    </div>
  );
}

// Render a structured OSCOLA citation from the backend: runs flagged `i` are italic
// (case names), the rest plain. Falls back to a plain string when no citation is supplied.
type OscolaCite = { parts: { t: string; i: boolean }[]; text: string };
export function Oscola({ c, fallback }: { c?: OscolaCite | null; fallback?: string }) {
  if (!c || !c.parts || c.parts.length === 0) return <>{fallback ?? ""}</>;
  return <>{c.parts.map((p, i) => p.i ? <i key={i}>{p.t}</i> : <Fragment key={i}>{p.t}</Fragment>)}</>;
}

// --- Document reader + augment ---------------------------------------------
export function DocumentView({ id, open, openGraph, pinpoint }: { id: string; open: (id: string, a?: string) => void; openGraph: (id: string) => void; pinpoint?: string | null }) {
  const [doc, err, reload] = useAsync(() => api.document(id), [id]);
  const [pinAnchor, setPinAnchor] = useState("");
  const [editing, setEditing] = useState(false);
  // Options (snowball, graph, fix-metadata) and provenance metadata are hidden by default
  // behind a subtle toggle so the reading surface stays uncluttered.
  const [showOpts, setShowOpts] = useState(false);
  const tray = useTray();
  if (err) return <p className="err">{err}</p>;
  if (!doc) return <p className="muted loading-pulse">Loading…</p>;
  if (doc.error) return <p className="err">{doc.error}: {id}</p>;
  const d = doc.document;
  const versions = doc.versions || [];
  return (
    <div>
      <div className="panel">
        <h2 className="doc-title" style={{ marginTop: 0 }}><Oscola c={doc.oscola} fallback={d.title || d.stable_id} /></h2>
        <div className="doc-summary">
          <a className="summary-stat" title="Later documents that cite this one"
            onClick={() => tray.push({ kind: "mentions", target: d.stable_id, label: "Citations to this decision" })}>Citations to this decision <b>{doc.cited_by_count ?? 0}</b></a>
          <span className="summary-sep">|</span>
          <a className="summary-stat" title="Distinct cases this document cites"
            onClick={() => tray.push({ kind: "cites", target: d.stable_id, family: "cases", label: "Cases cited" })}>Cases cited <b>{doc.cases_cited_count ?? 0}</b></a>
          <span className="summary-sep">|</span>
          <a className="summary-stat" title="Distinct statutory material this document cites"
            onClick={() => tray.push({ kind: "cites", target: d.stable_id, family: "statute", label: "Statutory material cited" })}>Statutory material cited <b>{doc.statute_cited_count ?? 0}</b></a>
        </div>
        {(doc.also_cited_as || []).length > 0 && (
          <p className="also-cited muted" title="Alternative citation forms linked to this document (parallel-citation mining, report matching, your confirmations)">
            Also cited as {doc.also_cited_as.map((a: string, i: number) =>
              <Fragment key={i}>{i > 0 && <span className="summary-sep"> · </span>}<b>{a}</b></Fragment>)}
          </p>
        )}
        <a className="opts-toggle muted" onClick={() => setShowOpts((v) => !v)}>
          {showOpts ? "▾ Hide options and metadata" : "▸ Expand options and metadata"}</a>
        {showOpts && (
          <div className="opts-tray">
            <div className="row" style={{ alignItems: "flex-start" }}>
              <Snowball seed={d.stable_id} onDone={reload} />
              <button onClick={() => setEditing((e) => !e)} style={{ flex: "0 0 auto" }}>✎ {editing ? "cancel" : "fix metadata"}</button>
              <button onClick={() => openGraph(d.stable_id)} style={{ flex: "0 0 auto" }}>◴ View citation graph</button>
            </div>
            <p className="muted" style={{ marginTop: 8 }}>{d.ecli || d.stable_id} · {d.source}/{d.court} · {d.doc_type}
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
        )}
      </div>
      {(doc.incoming || []).length > 0 && <CitedByPanel incoming={doc.incoming} count={doc.cited_by_count} inferred={doc.inferred_by_count} />}
      <div className="panel">
        <Reader id={d.stable_id} incoming={doc.incoming || []} pinpoint={pinpoint}
          oscola={doc.oscola} title={d.title || d.stable_id} landingUrl={d.landing_url} />
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
  const colour: Record<string, string> = { overrules: "var(--bad)", distinguishes: "var(--warn)", applies: "var(--ok)", follows: "var(--ok)" };
  // "mentions" is confusing from the cited-authority's side — read it as "mentioned by".
  const treat = (t: string) => (t === "mentions" ? "mentioned by" : t);
  return (
    <div className="panel">
      <h3>Cited by <span className="muted">({count ?? incoming.length}) — later documents that cite this one, and how</span>
        {inferred ? <span className="muted" style={{ fontWeight: 400 }}> {" "}
          <Info t={`Plus ${inferred} inferred link${inferred === 1 ? "" : "s"} — heuristic carry-forwards (a bare "Section 12" pinned to the last-named Act), not citations anyone made. Excluded from the count above so they don't inflate it.`} />
          {" +"}{inferred} inferred</span> : null}</h3>
      <div className="active-chips" style={{ marginBottom: 6 }}>
        {order.filter((t) => byType[t]).map((t) => (
          <span key={t} className="tag" style={{ borderColor: colour[t] || "var(--line)", color: colour[t] || "inherit" }}>
            {byType[t]} {treat(t)}</span>
        ))}
      </div>
      <table><tbody>
        {incoming.slice(0, 50).map((r, i) => (
          <tr key={i}>
            <td style={{ whiteSpace: "nowrap", color: colour[r.relationship_type] || "var(--subtext)" }}>{treat(r.relationship_type)}</td>
            <td><a onClick={() => open(r.src_id, r.dst_anchor)}><Oscola c={r.src_oscola} fallback={r.src_title || r.src_id} /></a>
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
export function Dashboard({ open: _open, navigate }: { open: (id: string) => void; navigate?: (f: Record<string, string>) => void }) {
  const [sources, , reloadSources] = useAsync(() => api.sources(), []);
  const [queues, , reloadQueues] = useAsync(() => api.queues(), []);
  const [alerts, , reloadAlerts] = useAsync(() => api.alerts(), []);
  const [stats, , reloadStats] = useAsync(() => api.stats(), []);
  const [worklist, , reloadWork] = useAsync(() => api.worklist(20), []);
  const [srcList] = useAsync(() => api.sourceList(), []);
  const [health] = useAsync(() => api.embeddingHealth(), []);
  const [backlog, , reloadBacklog] = useAsync(() => api.embedBacklog(), []);
  const [msg, setMsg] = useState("");
  const [harvestSrc, setHarvestSrc] = useState("");
  const [backfill, setBackfill] = useState(false);
  const [pages, setPages] = useState(1);

  const refresh = () => { reloadSources(); reloadQueues(); reloadAlerts(); reloadStats(); reloadWork(); reloadBacklog(); };
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
          <button onClick={() => act(api.embed(), "embed")} style={{ flex: "0 0 auto" }}
            title={backlog ? `${backlog.indexed.toLocaleString()} indexed · ${backlog.pending.toLocaleString()} pending (${backlog.provider}/${backlog.model})` : "index documents for search"}>
            Embed / index{backlog ? ` (${backlog.pending.toLocaleString()} pending)` : ""}
          </button>
          <button onClick={() => act(api.resolve(), "resolve")} style={{ flex: "0 0 auto" }}>Resolve citations</button>
          <span className="muted" style={{ flex: 1, textAlign: "right", fontSize: 12 }}>
            Re-scans, full relinks, EU-name / ECtHR backfills &amp; corpus-growth jobs live in <b>Maintain</b>.
          </span>
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
          <div>{Object.entries(stats.by_doc_type || {}).map(([k, v]: any) =>
            <span className="tag" key={k}>{navigate ? <a onClick={() => navigate({ doc_type: k })} title="browse in Search">{k}: {v}</a> : <>{k}: {v}</>}</span>)}</div>
          <div>{Object.entries(stats.by_source || {}).map(([k, v]: any) =>
            <span className="tag" key={k}>{navigate ? <a onClick={() => navigate({ source: k })} title="browse in Search">{k}: {v}</a> : <>{k}: {v}</>}</span>)}</div>
          <div>{Object.entries(stats.by_tag || {}).map(([k, v]: any) =>
            <span className="tag" key={k}>{navigate ? <a onClick={() => navigate({ tag: k })} title="browse in Search">#{k}: {v}</a> : <>#{k}: {v}</>}</span>)}</div>
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
      <CaseLawImportPanel />
      <ZoteroPanel show={show} />
      <GuidanceRulesPanel />
      {msg && <div className="panel"><pre style={{ whiteSpace: "pre-wrap", wordBreak: "break-all" }}>{msg}</pre></div>}
    </div>
  );
}

// Zotero import — also the guidance-intake channel: clip an EDPB/Ofcom page (with its
// PDF) into a dedicated collection using the Zotero browser connector (your real
// browser session, so no bot-blocking), then pull that collection in as `guidance`.
// Connection is ONE field: the API key — the library id is derived from the key.
function ZoteroPanel({ show }: { show: (r: any) => void }) {
  const [status, setStatus] = useState<any>(null);
  const [rules, setRules] = useState<any>(null);
  const [key, setKey] = useState("");
  const [collection, setCollection] = useState("");
  const [docType, setDocType] = useState("");
  const [fetchPdfs, setFetchPdfs] = useState(true);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");
  const refresh = () => {
    api.zoteroStatus().then(setStatus).catch(() => setStatus({ connected: false, reason: "unreachable" }));
    api.guidanceRules().then(setRules).catch(() => {});
  };
  useEffect(refresh, []);
  // picking a collection with a saved intake mapping pre-fills the type
  useEffect(() => {
    const m = rules?.collections?.[collection];
    if (m?.doc_type) setDocType(m.doc_type);
  }, [collection, rules]);

  // parents first, children indented beneath them
  const cols: any[] = status?.collections || [];
  const roots = cols.filter((c) => !c.parent).sort((a, b) => (a.name || "").localeCompare(b.name || ""));
  const ordered: { key: string; label: string }[] = [];
  for (const r of roots) {
    ordered.push({ key: r.key, label: r.name });
    for (const ch of cols.filter((c) => c.parent === r.key).sort((a, b) => (a.name || "").localeCompare(b.name || "")))
      ordered.push({ key: ch.key, label: "· " + ch.name });
  }

  return (
    <div className="panel">
      <h3>Zotero library</h3>
      {!status && <p className="muted loading-pulse">checking connection…</p>}
      {status && !status.connected && (
        <div>
          <p className="muted" style={{ fontSize: 13 }}>
            Not connected. Create a key at{" "}
            <a href="https://www.zotero.org/settings/keys/new" target="_blank" rel="noopener noreferrer">
              zotero.org/settings/keys/new</a> (read access is enough), paste it here — that's the
            whole setup; your library id is derived from the key.
            {status.reason === "bad_key" && <span className="err"> The saved key was rejected — paste a fresh one.</span>}
          </p>
          <div className="row" style={{ alignItems: "center" }}>
            <input type="password" value={key} onChange={(e) => setKey(e.target.value)}
              placeholder="Zotero API key" style={{ maxWidth: 320 }} />
            <button className="primary" disabled={!key.trim() || busy} onClick={async () => {
              setBusy(true); setMsg("connecting…");
              try {
                await api.saveSettings({ ZOTERO_API_KEY: key.trim() });
                setKey(""); setMsg(""); refresh();
              } catch (e: any) { setMsg("error: " + e); } finally { setBusy(false); }
            }}>Connect</button>
          </div>
        </div>
      )}
      {status?.connected && (
        <div>
          <p className="ok" style={{ fontSize: 13, marginTop: 0 }}>
            ✓ connected{status.username ? <> as <b>{status.username}</b></> : null} · library {status.library_id}
            {" "}<a style={{ cursor: "pointer" }} className="muted" title="re-check" onClick={refresh}>↻</a>
          </p>
          <p className="muted" style={{ fontSize: 13 }}>
            Clip pages with the Zotero connector into an intake collection, pick it below, and import.
            Guidance PDFs are text-extracted with numbered-paragraph pinpoints and auto-classified
            (issuer · number · version · regime) — see the classification panel below for the rules.
          </p>
          <div className="row" style={{ flexWrap: "wrap", alignItems: "center" }}>
            <select value={collection} onChange={(e) => setCollection(e.target.value)} style={{ maxWidth: 280 }}>
              <option value="">whole library</option>
              {ordered.map((c) => <option key={c.key} value={c.key}>
                {c.label}{rules?.collections?.[c.key] ? " ✓" : ""}</option>)}
            </select>
            <select value={docType} onChange={(e) => setDocType(e.target.value)} style={{ flex: "0 0 auto" }}>
              <option value="">type: from Zotero itemType</option>
              {DOC_TYPES.map((t) => <option key={t} value={t}>type: {t}</option>)}
            </select>
            <label style={{ display: "flex", alignItems: "center", gap: 4, fontSize: 13 }}>
              <input type="checkbox" checked={fetchPdfs} onChange={(e) => setFetchPdfs(e.target.checked)} />
              fetch PDFs
            </label>
            <button className="primary" disabled={busy} onClick={async () => {
              setBusy(true); setMsg("importing…");
              try {
                const r = await api.importZotero({
                  limit: 50, fetch_pdfs: fetchPdfs,
                  ...(collection ? { collection } : {}),
                  ...(docType ? { doc_type: docType } : {}),
                });
                setMsg(""); show(r);
              } catch (e: any) { setMsg("error: " + e); } finally { setBusy(false); }
            }}>Import</button>
            {collection && docType && (
              <button className="mini" title="Remember this collection → type mapping, so future imports (and anyone clipping into it) need no re-selection"
                onClick={async () => {
                  try {
                    const next = { issuers: rules?.issuers || [], collections: { ...(rules?.collections || {}), [collection]: { doc_type: docType } } };
                    setRules(await api.saveGuidanceRules(next)); setMsg("✓ mapping saved");
                  } catch (e: any) { setMsg("error: " + e); }
                }}>save as intake mapping</button>
            )}
          </div>
          {msg && <p className={msg.startsWith("error") ? "err" : "ok"} style={{ fontSize: 12 }}>{msg}</p>}
        </div>
      )}
    </div>
  );
}

// How guidance classification works, laid open: the rules (data, editable here), a
// test-bench that shows per-field WHICH rule fired and WHAT it matched, and the
// re-classify job that applies rule edits to everything already imported.
function GuidanceRulesPanel() {
  const [rules, setRules] = useState<any>(null);
  const [rows, setRows] = useState<any[]>([]);
  const [maps, setMaps] = useState<[string, any][]>([]);
  const [msg, setMsg] = useState("");
  const [tryIn, setTryIn] = useState({ title: "", url: "", text: "" });
  const [preview, setPreview] = useState<any>(null);
  useEffect(() => {
    api.guidanceRules().then((r) => {
      setRules(r);
      setRows((r.issuers || []).map((i: any) => ({
        ...i, domains_text: (i.domains || []).join(", "),
        boilerplate_text: (i.boilerplate || []).join(", "),
      })));
      setMaps(Object.entries(r.collections || {}));
    }).catch(() => {});
  }, []);
  if (!rules) return null;
  const upd = (i: number, k: string, v: string) =>
    setRows((rs) => rs.map((r, j) => (j === i ? { ...r, [k]: v } : r)));
  const save = async () => {
    try {
      const payload = {
        issuers: rows.filter((r) => r.code?.trim()).map((r) => ({
          code: r.code.trim().toLowerCase(), label: r.label || r.code,
          domains: (r.domains_text || "").split(",").map((s: string) => s.trim()).filter(Boolean),
          boilerplate: (r.boilerplate_text || "").split(",").map((s: string) => s.trim()).filter(Boolean),
          default_regime: (r.default_regime || "").trim() || null,
        })),
        collections: Object.fromEntries(maps.filter(([k]) => k.trim())),
      };
      const r = await api.saveGuidanceRules(payload);
      setMsg(`✓ saved (${r.issuers.length} issuer rules) — run re-classify to apply to held guidance`);
    } catch (e: any) { setMsg("error: " + e); }
  };
  return (
    <div className="panel">
      <h3>Guidance classification <span className="muted">— how sorting works, and the rules that drive it</span></h3>
      <p className="muted" style={{ fontSize: 13 }}>
        Four deterministic stages, no LLMs: <b>1</b> the intake collection's saved mapping sets the
        document type (and default issuer); <b>2</b> issuer rules below match the source domain and
        first-page boilerplate (two independent witnesses — disagreement is flagged, not guessed);{" "}
        <b>3</b> identity grammars read the series number ("Guidelines 05/2020", "WP248 rev.01"),
        version and adopted/consultation status, minting the citation aliases; <b>4</b> the regime
        (what it's guidance <i>under</i>) comes from the document's own dominant legislation citation,
        falling back to the issuer default only when unrivalled. Every field stores the rule that fired
        and the matched text — visible as chips on the document and in the test-bench below. Human
        edits are marked <span className="kbd">manual</span> and never overwritten by a re-classify.
      </p>
      <h4 style={{ marginBottom: 4 }}>Issuer rules</h4>
      <table className="grid">
        <thead><tr><th>code</th><th>label</th><th>domains (comma-sep)</th><th>first-page boilerplate</th><th>default regime</th><th /></tr></thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i}>
              <td><input value={r.code || ""} onChange={(e) => upd(i, "code", e.target.value)} style={{ width: 70 }} /></td>
              <td><input value={r.label || ""} onChange={(e) => upd(i, "label", e.target.value)} /></td>
              <td><input value={r.domains_text || ""} onChange={(e) => upd(i, "domains_text", e.target.value)} /></td>
              <td><input value={r.boilerplate_text || ""} onChange={(e) => upd(i, "boilerplate_text", e.target.value)} /></td>
              <td><input value={r.default_regime || ""} onChange={(e) => upd(i, "default_regime", e.target.value)}
                placeholder="e.g. 32016R0679" style={{ width: 120 }} /></td>
              <td><button className="mini" title="remove" onClick={() => setRows((rs) => rs.filter((_, j) => j !== i))}>✕</button></td>
            </tr>
          ))}
        </tbody>
      </table>
      <p>
        <button className="mini" onClick={() => setRows((rs) => [...rs, { code: "", label: "", domains_text: "", boilerplate_text: "", default_regime: "" }])}>+ add issuer</button>{" "}
        <button className="primary" onClick={save}>Save rules</button>{" "}
        <button className="mini" title="Apply the current rules to every held guidance document (manual fields untouched) — runs as a job"
          onClick={async () => { try { const j = await api.classifyGuidanceJob(); setMsg(j.error || `✓ re-classify started (job ${j.job_id}) — watch the Jobs panel`); } catch (e: any) { setMsg("error: " + e); } }}>
          ↻ re-classify all guidance</button>
        {msg && <span className={msg.startsWith("error") ? "err" : "ok"} style={{ fontSize: 12 }}> {msg}</span>}
      </p>
      {maps.length > 0 && <>
        <h4 style={{ marginBottom: 4 }}>Intake collection mappings</h4>
        <table className="grid"><tbody>
          {maps.map(([k, v], i) => (
            <tr key={k}>
              <td style={{ fontFamily: "var(--mono, monospace)", fontSize: 12 }}>{k}</td>
              <td>{v.doc_type || "—"}{v.issuer ? ` · issuer: ${v.issuer}` : ""}</td>
              <td><button className="mini" onClick={() => setMaps((m) => m.filter((_, j) => j !== i))}>✕</button></td>
            </tr>
          ))}
        </tbody></table>
      </>}
      <h4 style={{ marginBottom: 4 }}>Test-bench <span className="muted">— paste a cover page, see which rules fire</span></h4>
      <div className="row" style={{ flexWrap: "wrap" }}>
        <input value={tryIn.title} onChange={(e) => setTryIn({ ...tryIn, title: e.target.value })} placeholder="title" />
        <input value={tryIn.url} onChange={(e) => setTryIn({ ...tryIn, url: e.target.value })} placeholder="source URL" />
      </div>
      <textarea value={tryIn.text} onChange={(e) => setTryIn({ ...tryIn, text: e.target.value })}
        placeholder="first-page text (optional)" rows={3} style={{ width: "100%" }} />
      <p><button className="mini" onClick={async () => {
        try { setPreview(await api.classifyGuidance(tryIn)); } catch (e: any) { setMsg("error: " + e); }
      }}>classify (dry run)</button></p>
      {preview?.fields && (
        <table className="grid">
          <thead><tr><th>field</th><th>value</th><th>rule that fired</th><th>matched</th></tr></thead>
          <tbody>
            {Object.entries(preview.fields).map(([k, v]: [string, any]) => (
              <tr key={k}><td>{k}</td><td><b>{v.value}</b></td>
                <td className="muted" style={{ fontSize: 12 }}>{v.rule}</td>
                <td className="muted" style={{ fontSize: 12 }}>{v.evidence}</td></tr>
            ))}
            {(preview.aliases || []).length > 0 && (
              <tr><td>aliases</td><td colSpan={3}>{preview.aliases.join(" · ")}</td></tr>
            )}
          </tbody>
        </table>
      )}
      {preview && !Object.keys(preview.fields || {}).length && <p className="muted">no rule matched — add a domain/boilerplate rule above and retry</p>}
    </div>
  );
}

// Upload saved case law — a folder or zip mixing BAILII judgment .html pages and Westlaw
// .rtf exports. Each file is routed to its own parser by extension in one background job:
// a BAILII page keys by its neutral-citation slug and "Cite as:" list; a Westlaw RTF keys
// by its strongest identity (neutral slug → ECLI → Westlaw id) with every parallel report
// citation aliased. New cases are imported, lower-fidelity copies superseded.
function CaseLawImportPanel() {
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);
  const [prog, setProg] = useState<{ done: number; total: number } | null>(null);
  const folderRef = useRef<HTMLInputElement>(null);
  const CASE_RE = /\.(html?|rtf)$/i;

  // Folder / multi-file upload — no zip needed. The browser hands us every file in the
  // picked folder; we keep the .html/.rtf, stage them server-side in batches (so no single
  // request is huge), then start one background import job that routes each by extension.
  async function uploadFiles(fileList: FileList) {
    const files = Array.from(fileList).filter((f) => CASE_RE.test(f.name));
    if (!files.length) { setMsg("no .html or .rtf files in that selection"); return; }
    const html = files.filter((f) => /\.html?$/i.test(f.name)).length;
    const rtf = files.length - html;
    setBusy(true); setProg({ done: 0, total: files.length });
    const uploadId = (crypto.randomUUID?.() || Math.random().toString(36).slice(2)).replace(/-/g, "").slice(0, 24);
    const BATCH = 200;
    try {
      for (let i = 0; i < files.length; i += BATCH) {
        const r = await api.importCaselawFilesBatch(uploadId, files.slice(i, i + BATCH));
        if (r.error) throw new Error(r.error);
        setProg({ done: Math.min(i + BATCH, files.length), total: files.length });
      }
      const kinds = [html && `${html} BAILII`, rtf && `${rtf} Westlaw`].filter(Boolean).join(" + ");
      setMsg(`staged ${files.length} files (${kinds}) — starting import…`);
      const j = await api.importCaselawFilesStart(uploadId);
      setMsg(j.error ? "error: " + j.error : `✓ queued as job ${j.job_id} (${files.length} files) — watch the Jobs panel`);
    } catch (err: any) { setMsg("error: " + (err.message || err)); }
    finally { setBusy(false); setProg(null); }
  }

  return (
    <div className="panel">
      <h3>Case law (folder or zip of BAILII .html + Westlaw .rtf)</h3>
      <p className="muted" style={{ fontSize: 13 }}>
        Pick a whole folder — no zipping needed — or drop a zip. Saved BAILII case pages
        (<code>.html</code>) and Westlaw case exports (<code>.rtf</code>) can be mixed freely; each file is
        routed to its own parser. BAILII pages key by neutral citation and the “Cite as:” list; Westlaw
        RTFs key by neutral citation → ECLI → Westlaw id, with parties, court, judges, counsel and every
        parallel report citation extracted and aliased (EU cases reported in UK series key by their
        ECLI). Runs in the background — watch the Jobs panel.
      </p>
      <div className="row" style={{ flexWrap: "wrap", alignItems: "center", gap: 10 }}>
        <button className="primary" disabled={busy} onClick={() => folderRef.current?.click()}>
          Choose folder
        </button>
        {/* webkitdirectory: whole-folder picker (recursive). Not in the TS DOM types → cast. */}
        <input ref={folderRef} type="file" multiple hidden
          // @ts-expect-error non-standard folder-picker attributes
          webkitdirectory="" directory=""
          onChange={(e) => { if (e.target.files?.length) uploadFiles(e.target.files); e.currentTarget.value = ""; }} />
        <span className="muted" style={{ fontSize: 12 }}>or select files:</span>
        <input type="file" multiple accept=".html,.htm,.rtf" disabled={busy}
          onChange={(e) => { if (e.target.files?.length) uploadFiles(e.target.files); e.currentTarget.value = ""; }} />
        <span className="muted" style={{ fontSize: 12 }}>or a zip:</span>
        <input type="file" accept=".zip" disabled={busy} onChange={async (e) => {
          const f = e.target.files?.[0];
          if (!f) return;
          setBusy(true); setMsg("uploading zip…");
          try {
            const r = await api.importCaselawZip(f);
            setMsg(r.error ? "error: " + r.error : `✓ queued as job ${r.job_id} — watch the Jobs panel`);
          } catch (err: any) { setMsg("error: " + (err.message || err)); }
          finally { setBusy(false); e.target.value = ""; }
        }} />
      </div>
      {prog && <p className="muted" style={{ fontSize: 12 }}>uploading {prog.done}/{prog.total} files…</p>}
      {msg && <p className={msg.startsWith("error") ? "err" : "ok"} style={{ fontSize: 12 }}>{msg}</p>}
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
// Plain-language capability chips for a source — so it's obvious what a watch on it can and
// can't do (search at the API vs post-filter, incremental "new since last run", forward-
// citation discovery, neutral-citation gap-scanning).
function SourceCaps({ info }: { info: any }) {
  const chip = (on: boolean, yes: string, no: string, title: string) => (
    <span className="cap-chip" data-on={on ? "1" : "0"} title={title}>{on ? "✓ " + yes : "✗ " + no}</span>
  );
  return (
    <div className="cap-chips">
      {chip(!!info.can_keyword_search, "keyword search at source", "keywords post-filter only",
        info.can_keyword_search ? "Keywords are searched in the source's own API — precise." : "The source API has no free-text search; keywords filter what's harvested (any-term match).")}
      {chip(!!info.can_incremental, "checks for new automatically", "fetched by naming items",
        info.can_incremental ? "A feed-like source: a watch can pull only what's new since the last run." : "This source is fetched by naming the acts/instruments; there's no moving feed to poll.")}
      {chip(!!info.can_discover_citing, "forward-citation discovery", "no citing-case discovery",
        info.can_discover_citing ? "Can find NEW documents that cite a target as they appear (the renewing watch)." : "This source can't search for documents citing a target.")}
      {info.can_gap_scan && <span className="cap-chip" data-on="1" title="Neutral-citation numbering can be gap-scanned per court/year (see Backfill gaps).">✓ gap-scannable</span>}
    </div>
  );
}

// "Keep current" — surfaces what the background scheduler already does on its own, and gives
// each a Run-now that fires a visible Job. So upkeep is legible, not folklore.
function KeepCurrentPanel() {
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);
  const runNow = (kind: any, label: string) => fireJob(kind, {}, (m) => setMsg(`${label}: ${m}`));
  const auto = [
    ["Pull EU case names / subjects (EUR-Lex)", "daily", "Fills missing CJEU case names so their OSCOLA citations read properly."],
    ["Re-check outstanding legislation amendments", "hourly", "Re-pulls acts whose legislation.gov.uk effects re-check is due (bounded)."],
    ["Propagate changes an act makes", "hourly", "Flags held acts affected by a change for re-pull."],
    ["Rebuild citation-frequency roll-up", "hourly", "Keeps the worklist ranking + snowball fresh."],
    ["Top up the statute gazetteer", "weekly", "Pulls newly passed acts from legislation.gov.uk so name citations keep confirming."],
    ["Drain the harvest worklist", "per tick", "Fetches a bounded batch of routable references each tick (set auto-drain on the Unresolved tab)."],
    ["Run due watches", "per tick", "Every enabled watch whose cadence is due starts as a Job."],
  ];
  return (
    <div className="panel">
      <h3 style={{ marginTop: 0 }}>Keep current <span className="muted">— automatic upkeep the scheduler already runs</span></h3>
      <p className="muted" style={{ fontSize: 13 }}>
        You don't have to trigger any of this by hand — the background scheduler runs it on a cadence, and its work now
        shows in the <b>Jobs</b> panel. The buttons just let you force one immediately.
      </p>
      <table className="grid"><thead><tr><th>task</th><th>runs</th></tr></thead>
        <tbody>
          {auto.map(([label, when, hint], i) => (
            <tr key={i}><td>{label}<div className="muted" style={{ fontSize: 11 }}>{hint}</div></td>
              <td className="muted" style={{ whiteSpace: "nowrap" }}>{when}</td></tr>
          ))}
        </tbody>
      </table>
      <div className="row" style={{ marginTop: 8, flexWrap: "wrap" }}>
        <button onClick={() => runNow("rebuild-citation-counts", "rebuild counts")}>↻ Rebuild citation counts</button>
        <button onClick={() => runNow("backfill-metadata", "backfill metadata")}>✎ Repair metadata</button>
        <button disabled={busy} onClick={async () => {
          setBusy(true); setMsg("EU case names: running…");
          try { const r = await api.backfillTitles(); setMsg("EU case names: " + JSON.stringify(r)); }
          catch (e: any) { setMsg("✗ " + e); } finally { setBusy(false); }
        }} title="Pull CJEU case names + subjects from the EUR-Lex webservice (needs credentials in Settings). Runs the daily auto-task now.">⇊ EU case names</button>
        <button onClick={() => runNow("rescan-citations", "re-scan citations")}>↻ Re-scan all citations</button>
        <button onClick={() => fireJob("rescan", { doc_types: ["judgment"] }, (m) => setMsg(`full relink — judgments: ${m}`))} title="Re-extract every JUDGMENT (skips the 122k legislation docs, ~2× faster), then run the whole resolution chain">⟳ Full relink (judgments)</button>
        <button onClick={() => fireJob("rescan", {}, (m) => setMsg(`full relink — all: ${m}`))} title="Re-extract EVERY document (incl. legislation), then run the whole resolution chain">⟳ Full relink (all)</button>
      </div>
      {msg && <p className={msg.includes("✗") ? "err" : "ok"} style={{ fontSize: 12, marginTop: 6 }}>{msg}</p>}
    </div>
  );
}

// UK neutral-citation courts a gap-scan can enumerate (slug heads used in stable_ids).
const GAP_COURTS = ["uksc", "ukpc", "ewca/civ", "ewca/crim", "ewhc/admin", "ewhc/ch", "ewhc/comm",
  "ewhc/kb", "ewhc/qb", "ewhc/fam", "ewhc/tcc", "ewhc/pat", "ewhc/ipec", "eat",
  "ukut/aac", "ukut/iac", "ukut/lc", "ukut/tcc", "ukftt/grc", "ukftt/tc"];

// "Backfill gaps" — the completeness engine. Enumerate a court's neutral-citation numbering
// for a year, pull the missing judgments, and account for the gaps (historic = permanent).
function GapFillPanel() {
  const thisYear = new Date().getFullYear();
  const [court, setCourt] = useState("ewca/civ");
  const [year, setYear] = useState(thisYear - 1);
  const [status, setStatus] = useState<any>(null);
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);
  const loadStatus = async () => { try { setStatus(await api.gapStatus(court, year)); } catch { setStatus(null); } };
  useEffect(() => { loadStatus(); /* eslint-disable-next-line */ }, [court, year]);
  const scan = async () => {
    setBusy(true); setMsg("");
    try { const r = await api.gapScan({ court, year }); setMsg(r.error ? "error: " + r.error : "✓ gap-scan queued — watch it in the Jobs panel, then Refresh status."); }
    catch (e: any) { setMsg("error: " + e); } finally { setBusy(false); }
  };
  const clear = async () => { await api.gapClear(court, year); setMsg("gaps cleared — re-scan to re-probe"); loadStatus(); };
  const s = status;
  return (
    <div className="panel">
      <h3 style={{ marginTop: 0 }}>Backfill gaps <span className="muted">— fill a court's neutral-citation numbering toward 100%</span></h3>
      <p className="muted" style={{ fontSize: 13 }}>
        Enumerates <span className="kbd">[year] COURT 1, 2, 3…</span>, fetches every judgment that isn't held, and records the gaps.
        A <b>past year is contiguous</b>, so a missing number is marked <b>permanently unavailable</b> (never issued, or not digitised).
        The <b>current year</b> is still filling, so its misses are re-probed later. Each pulled judgment is extracted + resolved, so its
        own citations join the graph and feed onward pulling.
      </p>
      <div className="row" style={{ flexWrap: "wrap", alignItems: "center" }}>
        <label style={{ flex: "0 0 auto" }}>court
          <select value={court} onChange={(e) => setCourt(e.target.value)} style={{ marginLeft: 6, width: "auto" }}>
            {GAP_COURTS.map((c) => <option key={c} value={c}>{c}</option>)}
          </select></label>
        <label style={{ flex: "0 0 auto" }}>year
          <input type="number" min={1990} max={thisYear} value={year} onChange={(e) => setYear(+e.target.value || thisYear)} style={{ width: 90, marginLeft: 6 }} /></label>
        <button className="primary" disabled={busy} style={{ flex: "0 0 auto" }} onClick={scan}>⤓ {busy ? "queuing…" : "Scan & fill"}</button>
        <button style={{ flex: "0 0 auto" }} onClick={loadStatus}>↻ Refresh status</button>
      </div>
      {s && <div className="gap-status">
        <div className="row stat-strip" style={{ gap: 20, flexWrap: "wrap", marginTop: 10 }}>
          <div><b>{s.held}</b><div className="muted">held</div></div>
          <div><b>{s.highest || "—"}</b><div className="muted">highest no.</div></div>
          <div><b>{s.permanent_gaps}</b><div className="muted">permanent gaps</div></div>
          <div><b>{s.pending_reprobe}</b><div className="muted">pending re-probe</div></div>
          <div><b>{s.complete ? "✓" : "—"}</b><div className="muted">accounted for</div></div>
        </div>
        {s.gap_numbers?.length > 0 && <p className="muted" style={{ fontSize: 12, marginTop: 6 }}>
          permanent gaps (never issued / not digitised): {s.gap_numbers.slice(0, 60).join(", ")}{s.gap_numbers.length > 60 ? "…" : ""}
          {" "}<a onClick={clear} style={{ cursor: "pointer" }}>clear &amp; re-probe</a></p>}
      </div>}
      {msg && <p className={msg.startsWith("error") ? "err" : "ok"} style={{ fontSize: 12, marginTop: 6 }}>{msg}</p>}
    </div>
  );
}

// "Expand coverage" — one-off pulls that grow the corpus outward from what it already
// holds (as opposed to Keep-current's automatic upkeep). Moved here from the Dashboard.
function ExpandCoveragePanel() {
  const [msg, setMsg] = useState("");
  return (
    <div className="panel">
      <h3 style={{ marginTop: 0 }}>Expand coverage <span className="muted">— one-off pulls that grow the corpus outward from what it holds</span></h3>
      <p className="muted" style={{ fontSize: 13 }}>
        Background jobs — watch the <b>Jobs</b> panel for progress. <b>Queue missing ECtHR</b> fetches the Strasbourg cases
        your corpus cites by name / EHRR but doesn't hold; <b>Pull cases citing EU cases</b> walks CELLAR's citation graph to
        pull every judgment that cites an EU case already held.
      </p>
      <div className="row" style={{ flexWrap: "wrap" }}>
        <button onClick={() => fireJob("harvest-echr", {}, setMsg)}
          title="Queue the ECtHR cases the corpus cites by name/EHRR but doesn't hold, and fetch them from HUDOC by case-name search (most-cited first). Then links their EHRR citations.">⇊ Queue missing ECtHR (HUDOC)</button>
        <button onClick={() => fireJob("expand-citing", {}, setMsg)}
          title="Find and pull every case that CITES an EU case already in the corpus (via CELLAR's citation graph). Backward citation expansion.">⇊ Pull cases citing EU cases</button>
      </div>
      {msg && <p className={msg.startsWith("✗") ? "err" : "ok"} style={{ fontSize: 12, marginTop: 6 }}>{msg}</p>}
    </div>
  );
}

// The consolidated "Maintain" page: keep-current upkeep, gap backfill, watches, and rules —
// the whole "grow + keep the corpus complete" surface in one place.
export function MaintainView({ open }: { open: (id: string) => void }) {
  return (
    <div>
      <div className="panel" style={{ background: "transparent", border: "none", padding: 0, marginBottom: 8 }}>
        <h2 style={{ margin: 0 }}>Maintain</h2>
        <p className="muted" style={{ marginTop: 4 }}>
          Grow the corpus and keep it current. <b>Keep current</b> is automatic upkeep; <b>Backfill gaps</b> chases 100%
          completeness court-by-court; <b>Expand coverage</b> pulls in cited-but-missing authorities; <b>Watches</b> pull
          new material on a schedule; <b>Rules</b> are optional shorthands.
        </p>
      </div>
      <KeepCurrentPanel />
      <GapFillPanel />
      <ExpandCoveragePanel />
      <RefinementFlagsPanel open={open} />
      <WatchesView />
      <RulesView open={open} />
    </div>
  );
}

// Reader passages flagged "for improved refinement" — the queue of linking mistakes a
// human noticed, with everything an LLM/engineer needs to reproduce each one: the doc,
// the passage, what it links to now, and what the user says it should do.
function RefinementFlagsPanel({ open }: { open: (id: string, a?: string) => void }) {
  const [flags, , reload] = useAsync(() => api.refinementFlags("open"), []);
  if (!flags || flags.length === 0) return null;
  return (
    <div className="panel">
      <h3 style={{ marginTop: 0 }}>Flagged for refinement <span className="muted">
        — passages you marked as badly linked, for the next pass over the linking logic</span>
        <span className="tag" style={{ marginLeft: 8 }}>{flags.length}</span></h3>
      <table className="grid"><thead><tr><th>where</th><th>passage</th><th>links now</th><th>should</th><th /></tr></thead>
        <tbody>{flags.map((f: any) => {
          let links: any[] = [];
          try { links = JSON.parse(f.current_links || "[]"); } catch { /* legacy */ }
          return (
            <tr key={f.flag_id}>
              <td style={{ whiteSpace: "nowrap" }}>
                <a onClick={() => open(f.doc_id, f.anchor || undefined)} style={{ cursor: "pointer" }}>{f.doc_id}</a>
                {f.anchor && <span className="muted"> · {f.anchor}</span>}</td>
              <td style={{ maxWidth: 320 }}><b>“{f.selected_text}”</b></td>
              <td className="muted" style={{ fontSize: 12 }}>
                {links.length === 0 ? "nothing" : links.slice(0, 4).map((l: any, i: number) => (
                  <span key={i} title={l.title || ""}>{i > 0 && ", "}{l.text} <span className="muted">({l.state})</span></span>
                ))}{links.length > 4 && ` +${links.length - 4}`}</td>
              <td className="muted" style={{ fontSize: 12 }}>{f.note || "—"}</td>
              <td style={{ whiteSpace: "nowrap" }}>
                <button className="mini" title="mark handled"
                  onClick={async () => { await api.setRefinementFlag(f.flag_id); reload(); }}>✓ resolve</button></td>
            </tr>
          );
        })}</tbody>
      </table>
    </div>
  );
}

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
  const [srcOpts, setSrcOpts] = useState<Record<string, string>>({});
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState<number | "new" | null>(null);

  const info = (cat ?? []).find((s) => s.key === source);

  async function create() {
    if (!name || (!source && !cites && !citing)) { setMsg("give a name and a source, a ‘discover citing’ target, or a ‘cites’ rule"); return; }
    setBusy("new"); setMsg("");
    const spec: any = { degrees, max_pages: maxPages };
    if (source) spec.source = source;
    const opts = Object.fromEntries(Object.entries(srcOpts).filter(([, v]) => v.trim()));
    if (source && Object.keys(opts).length) spec.source_options = opts;
    if (keywords.trim()) spec.keywords = keywords.split(",").map((k) => k.trim()).filter(Boolean);
    if (citing.trim()) spec.discover = { citing: citing.trim(), via: "auto" };
    if (cites.trim()) spec.seed_rule = { cites: cites.trim() };
    if (tag.trim()) spec.tag = tag.trim();
    try {
      await api.createWatch({ name, spec, cadence_minutes: cadence });
      setName(""); setKeywords(""); setCites(""); setCiting(""); setTag(""); setSrcOpts({}); reload();
    } catch (e: any) { setMsg("error: " + e); } finally { setBusy(null); }
  }
  async function run(id: number) {
    setBusy(id); setMsg("");
    // runs as a background job now — it shows in the Jobs panel with per-stage progress
    try {
      const r = await api.runWatch(id);
      setMsg(r.error ? "error: " + r.error : `✓ watch #${id} queued — watch it run in the Jobs panel (bottom-left)`);
      reload();
    } catch (e: any) { setMsg("error: " + e); } finally { setBusy(null); }
  }
  return (
    <div>
      <div className="panel">
        <h3>New watch <span className="muted">— a saved harvest plan: keyword-limit a source, then enrich each new case with its citations.</span></h3>
        <p className="muted" style={{ fontSize: 12, marginTop: 0 }}>
          Scheduling pays off when new material keeps arriving. The two <b style={{ color: "var(--ok)" }}>growing</b> watch
          types: a <b>source/keyword</b> harvest (new decisions are handed down), and <b>🔎 discover cases citing X</b> —
          forward-citation discovery via Find Case Law / CELLAR, which finds <i>new</i> judgments that cite a landmark
          as they appear. The snowball then back-fills each new case’s authorities. A pure <b>graph rule</b> (no source/
          discovery) is largely <i>one-shot</i> — a backward snowball converges; for a one-off radiate from a single
          document, use the <b>❅ Snowball</b> button there instead.
        </p>
        <div className="row" style={{ flexWrap: "wrap" }}>
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder="watch name, e.g. ‘UK DP cases’" style={{ minWidth: 180 }} />
          <select value={source} onChange={(e) => { setSource(e.target.value); setSrcOpts({}); }} style={{ flex: "0 0 auto" }}>
            <option value="">— source (optional) —</option>
            {(cat ?? []).map((s) => <option key={s.key} value={s.key}>{s.label}</option>)}
          </select>
        </div>
        {/* morph: explain what THIS source supports, in plain-language capability chips */}
        {info && <div style={{ marginTop: 4 }}>
          <p className="muted" style={{ fontSize: 12, marginBottom: 4 }}>{info.description}</p>
          <SourceCaps info={info} /></div>}
        {/* per-source options (court filter, feed=new, legislation types, …) — the same
            knobs the CLI's -o takes, so a watch can be scoped without leaving the UI */}
        {info && (info.options ?? []).length > 0 && <div className="row" style={{ flexWrap: "wrap", marginTop: 4 }}>
          {(info.options ?? []).filter((o: any) => o.name !== "query").map((o: any) => (
            <input key={o.name} value={srcOpts[o.name] ?? ""} title={o.label}
              onChange={(e) => setSrcOpts({ ...srcOpts, [o.name]: e.target.value })}
              placeholder={`${o.label}${o.placeholder ? ` — ${o.placeholder}` : ""}`} style={{ minWidth: 200 }} />
          ))}
        </div>}
        <div className="row" style={{ flexWrap: "wrap", marginTop: 4 }}>
          {source && <input value={keywords} onChange={(e) => setKeywords(e.target.value)}
            placeholder={info?.keyword_search ? "keywords (searched at source), comma-sep" : "keywords (post-filter), comma-sep"} style={{ minWidth: 220 }} />}
          <input value={citing} onChange={(e) => setCiting(e.target.value)}
            title="Find NEW cases that cite this, via Find Case Law search (UK) or CELLAR (EU CELEX). This grows over time."
            placeholder="🔎 discover NEW cases citing… e.g. 32016R0679 (GDPR) or [2014] UKSC 38" style={{ minWidth: 280, color: "var(--ok)" }} />
          <input value={cites} onChange={(e) => setCites(e.target.value)}
            placeholder="…or graph rule: corpus docs citing id" style={{ minWidth: 200 }} />
        </div>
        <div className="row" style={{ flexWrap: "wrap", marginTop: 4, alignItems: "center" }}>
          <label style={{ flex: "0 0 auto" }} title="Enrich each newly-found case by fetching what it cites, N hops out">enrich each case <select value={degrees} onChange={(e) => setDegrees(+e.target.value)}>{[0, 1, 2, 3].map((n) => <option key={n} value={n}>{n} degree{n !== 1 ? "s" : ""}</option>)}</select></label>
          {source && <label style={{ flex: "0 0 auto" }}>pages <input type="number" min={1} max={20} value={maxPages} onChange={(e) => setMaxPages(+e.target.value || 1)} style={{ width: 50 }} /></label>}
          <input value={tag} onChange={(e) => setTag(e.target.value)} placeholder="tag results into collection (optional)" style={{ maxWidth: 220 }} />
          <label style={{ flex: "0 0 auto" }}>every <input type="number" min={5} value={cadence} onChange={(e) => setCadence(+e.target.value || 1440)} style={{ width: 84 }} /> min</label>
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
                {w.spec.discover ? <span style={{ color: "var(--ok)" }}>🔎 cases citing <b>{w.spec.discover.citing}</b></span> : null}
                {w.spec.seed_rule ? <> seed: cites <b>{w.spec.seed_rule.cites}</b></> : null}
                {` · ❅ ${w.spec.degrees ?? 1}°`}{w.spec.tag ? ` · →#${w.spec.tag}` : ""}
                {!(w.spec.source || w.spec.discover) && <span title="No renewing source — a backward snowball converges, so scheduling adds little" style={{ color: "var(--warn)" }}> · one-shot</span>}
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
      <select value={val || "0"} onChange={(e) => set(e.target.value)} style={{ width: 88 }}>
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
          <button className="mini" style={{ flex: "0 0 auto" }}
            title="Scan the hanging references for near-misses — truncated act names ('Harassment Act 1997'), year slips, party-name matches against held judgments — and surface each as a 'Possibly: …?' you confirm with one click. Runs in the background."
            onClick={() => fireJob("suggest-matches", {}, setBulk)}>💡 suggest matches</button>
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
    <RetrievalExportPanel />
    <AllSuggestionsPanel />
    </div>
  );
}

// Export the unfetchable frontier as mention-ranked, ≤100-per-batch citation lists to
// paste into Westlaw UK "Find & Print" or Lexis+ UK "Get & Print" — the report-only
// authorities BAILII + Find Case Law don't hold, which those subscriptions usually do.
function RetrievalExportPanel() {
  const [minCiting, setMinCiting] = useState(3);
  const [batchSize, setBatchSize] = useState(100);
  const [sep, setSep] = useState("newline");
  const [names, setNames] = useState(false);
  // Westlaw UK / Lexis+ UK are UK subscriptions: an Irish or Commonwealth report in the
  // batch can't retrieve and just burns one of the 100 slots — so default to UK only.
  const JURS: [string, string][] = [["uk", "UK"], ["ie", "Ireland"], ["eu", "EU (CMLR…)"], ["commonwealth", "Commonwealth"]];
  const [jurs, setJurs] = useState<Record<string, boolean>>({ uk: true, ie: false, eu: false, commonwealth: false });
  const jurCsv = JURS.filter(([k]) => jurs[k]).map(([k]) => k).join(",");
  const [data, setData] = useState<any>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");
  const [copied, setCopied] = useState<number | null>(null);
  const run = async () => {
    setBusy(true); setMsg("");
    try {
      setData(await api.exportRetrievalCitations({
        min_citing: minCiting, batch_size: batchSize, separator: sep, include_names: names,
        jurisdictions: jurCsv }));
    } catch (e: any) { setMsg("error: " + e); } finally { setBusy(false); }
  };
  const qs = new URLSearchParams({ min_citing: String(minCiting), batch_size: String(batchSize),
    separator: sep, include_names: String(names), ...(jurCsv ? { jurisdictions: jurCsv } : {}) });
  return (
    <div className="panel">
      <h3 style={{ marginTop: 0 }}>Export for Westlaw / Lexis batch retrieval
        <span className="muted"> — the report-only authorities BAILII &amp; Find Case Law don't hold, ranked by how often your corpus cites them</span>
      </h3>
      <p className="muted" style={{ fontSize: 13 }}>
        Paste each block into Westlaw UK <b>Find &amp; Print</b> or Lexis+ UK <b>Get &amp; Print</b> (both take
        newline- or semicolon-separated lists and cap a run at 100 documents). Coverage caveats: the
        official ICLR Law Reports (AC/QB/Ch) may fail on Westlaw; Lexis rejects a citation that maps to
        more than one document. ECR &amp; EHRR are excluded (harvested from CELLAR / HUDOC already). Run
        the same list through both and merge if a batch retrieves poorly on one.
      </p>
      <div className="row" style={{ flexWrap: "wrap", alignItems: "center", gap: 10 }}>
        <label style={{ fontSize: 13 }}>min mentions <input type="number" min={1} value={minCiting}
          onChange={(e) => setMinCiting(+e.target.value || 1)} style={{ width: 60 }} /></label>
        <label style={{ fontSize: 13 }}>per batch <input type="number" min={1} max={100} value={batchSize}
          onChange={(e) => setBatchSize(Math.min(100, +e.target.value || 100))} style={{ width: 60 }} /></label>
        <select value={sep} onChange={(e) => setSep(e.target.value)}>
          <option value="newline">newline-separated</option>
          <option value="semicolon">semicolon-separated</option>
        </select>
        <label style={{ display: "flex", alignItems: "center", gap: 4, fontSize: 13 }}>
          <input type="checkbox" checked={names} onChange={(e) => setNames(e.target.checked)} />
          include cases cited by name (won't retrieve without a citation)
        </label>
        <span style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 13 }}
          title="Which report-series jurisdictions to include. A UK subscription can't retrieve Irish or Commonwealth reports — they'd burn slots in the 100-citation batch.">
          <span className="muted">jurisdictions:</span>
          {JURS.map(([k, label]) => (
            <label key={k} style={{ display: "flex", alignItems: "center", gap: 3 }}>
              <input type="checkbox" checked={!!jurs[k]} onChange={(e) => setJurs({ ...jurs, [k]: e.target.checked })} />
              {label}
            </label>
          ))}
        </span>
        <button className="primary" disabled={busy} onClick={run}>{busy ? "building…" : "Build citation batches"}</button>
        {data && <a className="mini" href={`/api/export/retrieval-citations.txt?${qs}`} target="_blank" rel="noopener noreferrer">⬇ download all as .txt</a>}
      </div>
      {msg && <p className="err" style={{ fontSize: 12 }}>{msg}</p>}
      {data && (
        <div style={{ marginTop: 10 }}>
          <p className="ok" style={{ fontSize: 13 }}>
            {data.total_citations.toLocaleString()} citations · {data.total_mentions.toLocaleString()} mentions ·
            {" "}{data.batch_count} batch{data.batch_count === 1 ? "" : "es"} of ≤{data.batch_size}
          </p>
          {data.batches.map((b: any) => (
            <div key={b.index} style={{ marginBottom: 12 }}>
              <div className="row" style={{ alignItems: "baseline" }}>
                <b style={{ flex: 1 }}>Batch {b.index} <span className="muted">— {b.count} citations, {b.mentions.toLocaleString()} mentions</span></b>
                <button className="mini" onClick={() => { navigator.clipboard?.writeText(b.text); setCopied(b.index); setTimeout(() => setCopied(null), 1200); }}>
                  {copied === b.index ? "✓ copied" : "copy"}</button>
              </div>
              <textarea readOnly value={b.text} rows={Math.min(12, b.count + 1)}
                style={{ width: "100%", fontFamily: "var(--mono, monospace)", fontSize: 12 }}
                onFocus={(e) => e.currentTarget.select()} />
            </div>
          ))}
        </div>
      )}
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
  const [linkRef, setLinkRef] = useState<string | null>(null);
  const [holMsg, setHolMsg] = useState("");
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
          onClick={async () => {
            try { await api.harvestHoL(); setHolMsg("✓ queued — watch the Jobs panel"); }
            catch (e: any) { setHolMsg("✗ " + e); }
          }}>
          ⚖ scrape House of Lords + match</button>
      </div>
      {holMsg && <p className={holMsg.startsWith("✗") ? "err" : "ok"} style={{ fontSize: 12 }}>{holMsg}</p>}
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
                    {" "}
                    <a style={{ cursor: "pointer" }} title="Link this reference to a document already in the corpus (name autocomplete)"
                      onClick={() => setLinkRef(linkRef === r.ref ? null : r.ref)}>{linkRef === r.ref ? "cancel" : "⚲ link"}</a>
                  </td>
                </tr>
                {(r.suggestions || []).length > 0 && (
                  <tr><td /><td colSpan={4} style={{ borderBottom: "none", paddingTop: 0 }}>
                    {r.suggestions.slice(0, 2).map((s: any, i: number) => <SuggestionRow key={i} s={s} onDone={reload} />)}
                  </td></tr>
                )}
                {upRef === r.ref && (
                  <tr><td colSpan={5}>
                    <UnfetchableUpload r={r} onDone={() => { setUpRef(null); reload(); }} />
                  </td></tr>
                )}
                {linkRef === r.ref && (
                  <tr><td colSpan={5}>
                    <LinkExisting refKey={r.ref} onDone={() => { setLinkRef(null); reload(); }} />
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
  async function upload(file: File) {
    setBusy(true); setMsg("importing…");
    try {
      // import_case extracts clean text (RTF de-RTF'd, PDF via pypdf), detects the case's
      // OWN neutral citation from the header, keys it there, and aliases the report
      // citation the user uploaded against — so every form of the citation resolves.
      const res = await api.importCase(file, { ref: r.raw || r.ref });
      const cite = res.detected_citation ? ` as ${res.detected_citation}` : "";
      setMsg(`✓ imported${cite} · ${res.aliases} alias(es) · resolved ${res.resolved_edges} citation(s)`);
      setTimeout(onDone, 1400);
    } catch (e: any) { setMsg("error: " + e.message); } finally { setBusy(false); }
  }
  return (
    <div style={{ padding: "4px 0" }}>
      <p className="muted" style={{ margin: "0 0 4px", fontSize: 12 }}>
        Download the judgment (PDF preferred; RTF works), then drop it here — it's keyed by the
        case's own neutral citation and every citation form is linked to it:
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

// One "Possibly: …?" match suggestion with tick/cross. Accepting links every citation of
// the reference to the suggested document (and fetches it first if it isn't held yet);
// rejecting records the decision so it's never suggested again. Decisions apply IN PLACE —
// no list reload, so you can sweep down the page confirming one after another without the
// rows re-ranking under your cursor.
function SuggestionRow({ s }: { s: any; onDone?: () => void }) {
  const [busy, setBusy] = useState(false);
  const [decided, setDecided] = useState<null | "accepted" | "rejected">(null);
  const [msg, setMsg] = useState("");
  const decide = async (accept: boolean) => {
    setBusy(true); setMsg(accept ? "linking…" : "");
    try {
      const r = await api.decideSuggestion(s.ref, s.suggested_id, accept);
      setDecided(accept ? "accepted" : "rejected");
      if (accept) {
        setMsg(`✓ linked${r.resolved_edges ? ` · resolved ${r.resolved_edges} edge(s)` : ""}` +
          (r.harvest ? (r.harvest.stored ? " · fetched" : r.harvest.error ? ` · fetch failed: ${r.harvest.error}` : "") : ""));
      } else {
        setMsg("✗ dismissed");
      }
    } catch (e: any) { setMsg("error: " + e); }
    setBusy(false);
  };
  return (
    <div className="suggestion" style={decided === "rejected" ? { opacity: 0.55 } : undefined}>
      <span className="sug-label">Possibly:</span>{" "}
      <b>{s.context || s.suggested_id}</b>
      {!s.held && <span className="muted"> · not held yet — accepting fetches it</span>}
      <span className="muted"> — {s.reason}</span>
      {s.extracted_parties && <Info t={`auto-extracted parties: ${s.extracted_parties}`} />}
      {" "}
      {!decided && <>
        <button className="mini sug-yes" disabled={busy} title="yes — link every citation of this reference to it"
          onClick={() => decide(true)}>✓</button>{" "}
        <button className="mini sug-no" disabled={busy} title="no — never suggest this again"
          onClick={() => decide(false)}>✗</button>
      </>}
      {msg && <span className={msg.startsWith("error") ? "err" : "ok"} style={{ fontSize: 12 }}> {msg}</span>}
    </div>
  );
}

// The full sweep-through list of every pending naming candidate, at the bottom of the
// page: tick/cross applies in place (no reload, no re-ranking), and "accept all" walks
// the whole list — deferring the resolver to ONE pass at the end.
function AllSuggestionsPanel() {
  const [data, err] = useAsync(() => api.pendingSuggestions(500), []);
  // decision state lives HERE, keyed per suggestion, so deciding never re-fetches the list
  const [state, setState] = useState<Record<string, { s: string; note?: string }>>({});
  const [sweeping, setSweeping] = useState(false);
  const stopRef = useRef(false);
  const rows: any[] = data?.suggestions || [];
  const key = (s: any) => `${s.ref} ${s.suggested_id}`;
  const pendingRows = rows.filter((s) => !state[key(s)] || state[key(s)].s === "pending");

  async function decideOne(s: any, accept: boolean, resolve = true) {
    const k = key(s);
    setState((st) => ({ ...st, [k]: { s: "busy" } }));
    try {
      const r = await api.decideSuggestion(s.ref, s.suggested_id, accept, resolve);
      const note = accept
        ? `✓${r.resolved_edges ? ` resolved ${r.resolved_edges}` : " linked"}` +
          (r.harvest ? (r.harvest.stored ? " · fetched" : r.harvest.error ? ` · fetch failed` : "") : "")
        : "✗ dismissed";
      setState((st) => ({ ...st, [k]: { s: accept ? "accepted" : "rejected", note } }));
    } catch (e: any) {
      setState((st) => ({ ...st, [k]: { s: "pending", note: "error: " + (e.message || e) } }));
    }
  }

  async function acceptAll() {
    setSweeping(true); stopRef.current = false;
    for (const s of rows) {
      if (stopRef.current) break;
      const k = key(s);
      // eslint-disable-next-line no-await-in-loop
      if (!stateRef.current[k] || stateRef.current[k].s === "pending") await decideOne(s, true, false);
    }
    try { await api.resolve(); } catch { /* the sweep already linked; resolve is a top-up */ }
    setSweeping(false);
  }
  // acceptAll reads decision state across awaits — a ref tracks the latest without re-renders
  const stateRef = useRef(state);
  useEffect(() => { stateRef.current = state; }, [state]);

  if (err || !rows.length) return null;
  return (
    <div className="panel">
      <div className="row" style={{ alignItems: "baseline" }}>
        <h3 style={{ marginTop: 0, flex: 1 }}>Naming candidates
          <span className="muted"> — every pending “Possibly: …?” suggestion. Ticks apply in place (nothing reloads); sweep the list, then the graph resolves.</span>
          {data?.total != null && <span className="tag" style={{ marginLeft: 8 }}>{data.total.toLocaleString()}</span>}
        </h3>
        {!sweeping
          ? <button className="mini" style={{ flex: "0 0 auto" }} disabled={pendingRows.length === 0}
              title="Accept every remaining suggestion below, then run one resolve pass"
              onClick={acceptAll}>✓ accept all ({pendingRows.length})</button>
          : <button className="mini" style={{ flex: "0 0 auto" }} onClick={() => { stopRef.current = true; }}>■ stop</button>}
      </div>
      <table className="grid">
        <thead><tr><th>reference</th><th>suggested match</th><th>why</th><th style={{ whiteSpace: "nowrap" }}>decide</th></tr></thead>
        <tbody>
          {rows.map((s) => {
            const st = state[key(s)];
            const done = st && (st.s === "accepted" || st.s === "rejected");
            return (
              <tr key={key(s)} style={st?.s === "rejected" ? { opacity: 0.5 } : undefined}>
                <td style={{ fontFamily: "var(--mono, monospace)", fontSize: 12 }}>{s.ref}</td>
                <td><b>{s.context || s.suggested_id}</b>
                  {!s.held && <span className="muted"> · not held — accepting fetches it</span>}
                  {s.extracted_parties && <Info t={`auto-extracted parties: ${s.extracted_parties}`} />}</td>
                <td className="muted" style={{ fontSize: 12 }}>{s.reason}{s.score != null && ` · ${Number(s.score).toFixed(2)}`}</td>
                <td style={{ whiteSpace: "nowrap" }}>
                  {!done && <>
                    <button className="mini sug-yes" disabled={st?.s === "busy" || sweeping}
                      title="yes — link every citation of this reference to it"
                      onClick={() => decideOne(s, true)}>✓</button>{" "}
                    <button className="mini sug-no" disabled={st?.s === "busy" || sweeping}
                      title="no — never suggest this again"
                      onClick={() => decideOne(s, false)}>✗</button>
                  </>}
                  {st?.s === "busy" && <span className="muted" style={{ fontSize: 12 }}> …</span>}
                  {st?.note && <span className={st.note.startsWith("error") ? "err" : "ok"} style={{ fontSize: 12 }}> {st.note}</span>}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

// Link a hanging reference to a held document by name (the human override beside the
// automatic suggestions) — an autocomplete over the corpus, then one click to resolve.
function LinkExisting({ refKey, onDone }: { refKey: string; onDone: () => void }) {
  const [msg, setMsg] = useState("");
  return (
    <div className="row" style={{ alignItems: "center", marginTop: 4 }}>
      <span className="muted" style={{ flex: "0 0 auto", fontSize: 12 }}>link to:</span>
      <DocAutocomplete placeholder="find the real case or act by name…"
        onPick={async (id, title) => {
          setMsg("linking…");
          try {
            const r = await api.resolveReference({ ref: refKey, existing_id: id });
            setMsg(r.resolved ? `✓ linked to ${title} · resolved ${r.resolved_edges} edge(s)` : `✓ linked to ${title}`);
            setTimeout(onDone, 1100);
          } catch (e: any) { setMsg("error: " + e); }
        }} />
      {msg && <span className={msg.startsWith("error") ? "err" : "ok"} style={{ flex: "0 0 auto", fontSize: 12 }}>{msg}</span>}
    </div>
  );
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
      {(r.suggestions || []).length > 0 && (
        <tr><td /><td colSpan={4} style={{ borderBottom: "none", paddingTop: 0 }}>
          {r.suggestions.slice(0, 2).map((s: any, i: number) => <SuggestionRow key={i} s={s} onDone={onDone} />)}
        </td></tr>
      )}
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
            {mode === "existing" && (existing
              ? <span className="tag" style={{ flex: 1 }}>{existing}{" "}
                  <a style={{ cursor: "pointer" }} onClick={() => setExisting("")}>change</a></span>
              : <div style={{ flex: 1, minWidth: 240 }}>
                  <DocAutocomplete placeholder="find the case or act by name…"
                    onPick={(id) => setExisting(id)} /></div>)}
            {mode === "url" && <input value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://…  (fetched via the scraping engine)" />}
            {mode === "file" && <input type="file" onChange={(e) => setFile(e.target.files?.[0] ?? null)} />}
            {mode === "bailii" && r.bailii_url && (
              <div
                style={{ flex: 1, border: "1px dashed var(--line)", padding: "8px 12px", background: "var(--inset)" }}
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
          <button onClick={apply} style={{ flex: "0 0 auto" }} title="Re-extract the corpus so rules link everywhere">↻ apply to corpus</button></div>
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
