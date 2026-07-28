// Explore — the homepage. One screen that puts the corpus's SHAPE in your head:
// a search bar, then a jurisdiction table (counts by kind as a labelled
// proportional bar, a year sparkline with its span, citation density) where every
// element drills DOWN IN PLACE. A row expands to a brushable timeline, a courts
// rail, and a document panel whose every part is itself a facet control: click a
// year → the timeline focuses; click a court → the rail scopes; click "cited by
// N" → the panel flips to what cites that document. A natural-language line
// always states exactly what the panel is showing. PageRank ranks throughout.
import { Fragment, useEffect, useRef, useState } from "react";
import { api } from "./api";
import { DocLink } from "./links";
import { FlagIcon, Oscola } from "./views";

const FMT = (n: number) => n >= 1_000_000 ? (n / 1_000_000).toFixed(1) + "M"
  : n >= 10_000 ? Math.round(n / 1000) + "k"
  : n >= 1000 ? (n / 1000).toFixed(1) + "k" : String(n);

const KIND_COLOURS: [string, string, string][] = [
  ["cases", "var(--exp-cases)", "case law"],
  ["legislation", "var(--exp-leg)", "legislation"],
  ["guidance", "var(--exp-guid)", "guidance & reports"],
  ["administrative", "var(--exp-admin)", "admin decisions"],
  ["other", "var(--exp-other)", "other"],
];

type ShapeRow = {
  jurisdiction: string; total: number; cases: number; legislation: number;
  guidance: number; other: number; with_text: number; embedded: number;
  density: number; years: Record<string, number>;
  courts: { court: string; label?: string; n: number }[];
  sources: { source: string; label: string; n: number }[];
};

// wiki-style external-link glyph (little square with an arrow leaving it)
function ExtIcon() {
  return (
    <svg className="ext-icon" viewBox="0 0 12 12" width="10" height="10" aria-hidden="true">
      <path d="M3.5 1.5H1.5v9h9V8.5" fill="none" stroke="currentColor" strokeWidth="1.2" />
      <path d="M6 1.5h4.5V6M10.2 1.8 5.5 6.5" fill="none" stroke="currentColor" strokeWidth="1.2" />
    </svg>
  );
}

// --- SVG year sparkline with its span labelled at the ends -------------------
function Spark({ years, height = 26, width = 132, brush, onBrush, active }:
  { years: Record<string, number>; height?: number; width?: number;
    brush?: boolean; onBrush?: (a: string, b: string) => void; active?: [string, string] | null }) {
  const ys = Object.keys(years).filter((y) => /^\d{4}$/.test(y)).sort();
  const [drag, setDrag] = useState<[number, number] | null>(null);
  const ref = useRef<SVGSVGElement>(null);
  if (ys.length < 2) return <span className="muted">—</span>;
  const lo = +ys[0], hi = +ys[ys.length - 1];
  const span = Math.max(1, hi - lo);
  const max = Math.max(...ys.map((y) => years[y]));
  const x = (yr: number) => ((yr - lo) / span) * width;
  const idxAt = (clientX: number) => {
    const r = ref.current!.getBoundingClientRect();
    return Math.max(lo, Math.min(hi, Math.round(lo + ((clientX - r.left) / r.width) * span)));
  };
  const commit = () => {
    if (drag && onBrush) {
      const [a, b] = [Math.min(...drag), Math.max(...drag)];
      onBrush(String(a), String(b));
    }
    setDrag(null);
  };
  const sel: [number, number] | null = drag ? [Math.min(...drag), Math.max(...drag)]
    : active ? [+active[0], +active[1]] : null;
  return (
    <span className="sparkwrap">
      <span className="spark-year">{lo}</span>
      <svg ref={ref} className={`spark${brush ? " brushable" : ""}`} width={width} height={height}
        viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none"
        onMouseDown={brush ? (e) => { e.preventDefault(); const i = idxAt(e.clientX); setDrag([i, i]); } : undefined}
        onMouseMove={brush ? (e) => drag && setDrag((d) => (d ? [d[0], idxAt(e.clientX)] : d)) : undefined}
        onMouseUp={brush ? commit : undefined} onMouseLeave={brush ? () => drag && commit() : undefined}>
        {sel && <rect x={x(sel[0])} y={0} width={Math.max(2, x(sel[1]) - x(sel[0]))} height={height}
          className="spark-sel" />}
        {ys.map((y) => (
          <rect key={y} x={x(+y)} y={height - Math.max(1.5, (years[y] / max) * (height - 2))}
            width={Math.max(1, width / span - 0.5)}
            height={Math.max(1.5, (years[y] / max) * (height - 2))}
            className="spark-bar">
            <title>{y}: {years[y].toLocaleString()}</title>
          </rect>
        ))}
      </svg>
      <span className="spark-year">{hi}</span>
    </span>
  );
}

// Proportional kind bar, colour-only — the numbers live in a small caption
// beneath it ("case law (281k) · legislation (99k)"), each with its colour dot,
// so nothing ever has to fit inside a segment.
function KindBar({ r }: { r: ShapeRow }) {
  const parts = KIND_COLOURS.filter(([k]) => ((r as any)[k] as number) > 0);
  return (
    <div className="kindwrap">
      <div className="kindbar" title={parts.map(([k, , label]) =>
        `${label}: ${((r as any)[k] as number).toLocaleString()}`).join(" · ")}>
        {parts.map(([k, colour]) => {
          const frac = ((r as any)[k] as number) / (r.total || 1);
          return frac > 0.004 &&
            <span key={k} className="kindseg" style={{ width: `${frac * 100}%`, background: colour }} />;
        })}
      </div>
      <div className="kind-caption">
        {parts.map(([k, colour, label]) => (
          <span key={k}><i className="kind-dot" style={{ background: colour }} />
            {label} ({FMT((r as any)[k] as number)})</span>
        ))}
      </div>
    </div>
  );
}

// Mobile composition: the horizontal bar has no room on a phone, so show the same
// breakdown as a compact stacked list — one "type  count" per line, small text — which
// stands a chance of fitting the narrow column. Hidden on desktop (CSS).
function KindList({ r }: { r: ShapeRow }) {
  const parts = KIND_COLOURS.filter(([k]) => ((r as any)[k] as number) > 0);
  return (
    <div className="kindlist" aria-hidden="true">
      {parts.map(([k, colour, label]) => (
        <div key={k} className="kindlist-row">
          <i className="kind-dot" style={{ background: colour }} />
          <span className="kindlist-label">{label}</span>
          <span className="kindlist-n">{FMT((r as any)[k] as number)}</span>
        </div>
      ))}
    </div>
  );
}

// availability + provenance chips for one document row
function Availability({ it }: { it: any }) {
  return (
    <>
      {it.has_text ? <span className="avail avail-text">text</span>
        : it.pdf ? <span className="avail avail-pdf">pdf</span>
        : <span className="avail avail-none">no full text</span>}
      {it.url && (
        <a className="src-link" href={it.url} target="_blank" rel="noopener noreferrer"
          onClick={(e) => e.stopPropagation()} title={`open at ${it.source_label}`}>
          {it.source_label} <ExtIcon /></a>
      )}
    </>
  );
}

type LegType = { label: string; n: number; years: Record<string, number>; filters: any[] };
type Facets = {
  kind: string; sort: string; court: string | null;
  years: [string, string] | null;
  cites: { id: string; label: any } | null;
  leg: LegType | null;              // a legislation type from the taxonomy rail
};

const SORT_LABEL: Record<string, string> = {
  authority: "most authoritative", cited: "most cited",
  newest: "newest first", oldest: "oldest first",
};
const KIND_LABEL: Record<string, string> = {
  "": "documents", cases: "case law", legislation: "legislation", guidance: "guidance & reports",
  administrative: "administrative decisions",
};

// The always-true sentence describing what the panel currently shows.
function describe(j: string, f: Facets, courtLabel?: string): string {
  const what = f.kind === "legislation" && f.leg
    ? `${f.leg.label} legislation` : (KIND_LABEL[f.kind] ?? f.kind);
  const bits = [`The ${SORT_LABEL[f.sort]} ${what}`];
  if (f.cites) bits.push("citing the document below");
  if (f.court) bits.push(`in the ${courtLabel || f.court}`);
  if (f.years) bits.push(f.years[0] === f.years[1] ? `from ${f.years[0]}`
    : `from ${f.years[0]}–${f.years[1]}`);
  bits.push(f.cites ? "" : `— ${j}`);
  return bits.filter(Boolean).join(" ");
}

// --- the drill panel: documents of the current facet slice -------------------
function DrillPanel({ jurisdiction, f, setF, open, courtLabel }:
  { jurisdiction: string; f: Facets; setF: (p: Partial<Facets>) => void;
    open: (id: string, a?: string) => void; courtLabel?: string }) {
  const [data, setData] = useState<any | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    let live = true;
    let timer: ReturnType<typeof setTimeout> | null = null;
    setBusy(true);
    const p: Record<string, string> = { jurisdiction, sort: f.sort };
    if (f.court) p.court = f.court;
    if (f.kind) p.kind = f.kind;
    if (f.kind === "legislation" && f.leg) p.leg = JSON.stringify(f.leg.filters);
    if (f.years) { p.year_from = f.years[0]; p.year_to = f.years[1]; }
    if (f.cites) p.cites = f.cites.id;
    const load = () => api.drill(p).then((d) => {
      if (!live) return;
      setData(d);
      // a cold cached slice answers instantly with _warming while the server
      // computes it in the background — poll until the real rows arrive
      if (d._warming) { timer = setTimeout(load, 1200); } else setBusy(false);
    }).catch(() => { if (live) { setData({ items: [] }); setBusy(false); } });
    load();
    return () => { live = false; if (timer) clearTimeout(timer); };
  }, [jurisdiction, f.court, f.kind, f.sort, f.years?.[0], f.years?.[1], f.cites?.id, f.leg?.label]);

  const HANG: [string, string][] = [["judgment", "cases"], ["decision", "decisions"],
    ["opinion", "opinions"], ["guidance", "guidance & reports"], ["legislation", "legislation"]];
  return (
    <div className="drill">
      <div className="drill-desc">
        <span className="drill-desc-text">{describe(jurisdiction, f, courtLabel)}</span>
        {busy && <span className="loading-chip">loading…</span>}
        <select className="sort-select" value={f.sort} onChange={(e) => setF({ sort: e.target.value })}
          title="ordering" aria-label="ordering">
          <option value="authority">most authoritative</option>
          <option value="cited">most cited</option>
          <option value="newest">newest first</option>
          <option value="oldest">oldest first</option>
        </select>
      </div>
      {f.cites && (
        <div className="cites-crumb">
          <a className="mini-link" onClick={() => setF({ cites: null })}>← back to {jurisdiction}</a>
          <span className="cites-target">citing <b><Oscola c={f.cites.label?.oscola} fallback={f.cites.label?.title || f.cites.id} /></b></span>
        </div>
      )}
      <div className="drill-head">
        <div className="seg-toggle mini-toggle">
          {[["", "All"], ["cases", "Cases"], ["legislation", "Legislation"],
            ["guidance", "Guidance/Reports"], ["administrative", "Admin decisions"]].map(([v, l]) => (
            // switching kind re-scopes the rail, so a court or legislation type
            // picked under another kind may no longer exist — reset for a
            // predictable view
            <button key={v} className={f.kind === v ? "on" : ""}
              onClick={() => setF({ kind: v, court: null, leg: null })}>{l}</button>
          ))}
        </div>
      </div>
      {busy && !data?.items?.length && <p className="muted drill-loading">Loading the slice…</p>}
      <ol className={`drill-list${busy ? " stale" : ""}`}>
        {(data?.items || []).map((it: any, i: number) => (
          <li key={it.id}>
            <span className="drill-rank">{i + 1}</span>
            <div className="drill-doc">
              <DocLink id={it.id} onOpen={() => open(it.id)}><Oscola c={it.oscola} fallback={it.title || it.id} /></DocLink>
              <div className="drill-meta muted">
                <span className="tag">{it.doc_type}</span>
                {it.court && <a className="facet-link" title={`focus on ${it.court_label || it.court}`}
                  onClick={() => setF({ court: it.court, cites: null })}>{it.court_label || it.court}</a>}
                {it.date && <a className="facet-link" title={`focus on ${it.date.slice(0, 4)}`}
                  onClick={() => setF({ years: [it.date.slice(0, 4), it.date.slice(0, 4)], cites: null })}>{it.date.slice(0, 4)}</a>}
                {it.cited_by > 0 && <a className="facet-link" title="see what cites this"
                  onClick={() => setF({ cites: { id: it.id, label: it }, kind: "", court: null, years: null })}>
                  cited by {it.cited_by.toLocaleString()}</a>}
                <Availability it={it} />
              </div>
              {it.hanging && Object.keys(it.hanging).length > 0 && (
                <div className="hanging">
                  {HANG.filter(([k]) => it.hanging[k]).map(([k, label]) => (
                    <a key={k} className="hang-chip" title={`${label} citing this — click to list them`}
                      onClick={() => setF({ cites: { id: it.id, label: it },
                        kind: k === "judgment" || k === "decision" || k === "opinion" ? "cases" : k,
                        court: null, years: null })}>
                      {FMT(it.hanging[k])} {label}</a>
                  ))}
                </div>
              )}
            </div>
          </li>
        ))}
      </ol>
      {data && !data.items.length && !busy && <p className="muted">Nothing in this slice.</p>}
    </div>
  );
}

// --- one expanded jurisdiction: rail (timeline, courts, sources) + drill -----
function Expanded({ r, open }: { r: ShapeRow; open: (id: string, a?: string) => void }) {
  const [f, setFacets] = useState<Facets>({ kind: "", sort: "authority", court: null,
    years: null, cites: null, leg: null });
  const setF = (p: Partial<Facets>) => setFacets((old) => ({ ...old, ...p }));
  // the rail follows the kind filter: choose Legislation and the timeline,
  // types and sources re-scope to legislation only
  const slice = (f.kind && (r as any).kinds?.[f.kind]) || r;
  const isLeg = f.kind === "legislation";
  const legTypes: LegType[] = isLeg ? (slice.types || []) : [];
  // the timeline narrows again when a legislation type is selected
  const timelineYears = (isLeg && f.leg?.years) || slice.years;
  return (
    <div className="exp-detail">
      <div className="exp-rail">
        <div className="exp-rail-title">Timeline
          {isLeg && f.leg ? ` — ${f.leg.label}` : f.kind ? ` — ${KIND_LABEL[f.kind]}` : ""}{" "}
          <span className="muted">— drag to focus</span></div>
        <Spark years={timelineYears} width={240} height={44} brush active={f.years}
          onBrush={(a, b) => setF({ years: [a, b], cites: null })} />
        {f.years && <a className="mini-link" onClick={() => setF({ years: null })}>clear {f.years[0]}–{f.years[1]} ✕</a>}
        {isLeg && legTypes.length > 0 && <>
          <div className="exp-rail-title">Types</div>
          <ul className="court-list">
            <li><a className={!f.leg ? "on" : ""} onClick={() => setF({ leg: null })}>all</a></li>
            {legTypes.map((t) => (
              <li key={t.label}>
                <a className={f.leg?.label === t.label ? "on" : ""}
                  onClick={() => setF({ leg: f.leg?.label === t.label ? null : t, cites: null })}>
                  <span className="court-name">{t.label}</span>
                  <span className="court-n">{FMT(t.n)}</span>
                </a>
              </li>
            ))}
          </ul>
        </>}
        {!isLeg && slice.courts.length > 0 && <>
          <div className="exp-rail-title">Courts and bodies</div>
          <ul className="court-list">
            <li><a className={!f.court ? "on" : ""} onClick={() => setF({ court: null })}>all</a></li>
            {slice.courts.map((c: { court: string; label?: string; n: number }) => (
              <li key={c.court}>
                <a className={f.court === c.court ? "on" : ""} title={c.court}
                  onClick={() => setF({ court: f.court === c.court ? null : c.court, cites: null })}>
                  <span className="court-name">{c.label || c.court}</span>
                  <span className="court-n">{FMT(c.n)}</span>
                </a>
              </li>
            ))}
          </ul>
        </>}
        <div className="exp-rail-title">Sources{f.kind ? ` — ${KIND_LABEL[f.kind]}` : ""}</div>
        <div className="src-chips">
          {slice.sources.map((s: { source: string; label: string; n: number }) =>
            <span key={s.source} className="tag"
              title={`${s.n.toLocaleString()} documents (${s.source})`}>{s.label}</span>)}
        </div>
      </div>
      <DrillPanel jurisdiction={r.jurisdiction} f={f} setF={setF} open={open}
        courtLabel={f.court
          ? slice.courts.find((c: any) => c.court === f.court)?.label
          : undefined} />
    </div>
  );
}

// A subtle "updated X ago · Refresh" for the homepage figures. They come from roll-ups
// that now refresh weekly (not hourly) to spare the box, so a manual refresh is offered.
function _ago(iso?: string | null): string {
  if (!iso) return "not yet";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 90) return "just now";
  if (s < 5400) return `${Math.round(s / 60)}m ago`;
  if (s < 172800) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}
// A snippet with the matched words marked. The backend returns offsets rather than
// HTML so nothing user-supplied is ever interpreted as markup.
function Marked({ text, spans }: { text: string; spans?: number[][] }) {
  if (!spans || !spans.length) return <>{text}</>;
  const out: any[] = [];
  let at = 0;
  spans.forEach(([s, e], i) => {
    if (s > at) out.push(text.slice(at, s));
    out.push(<mark key={i}>{text.slice(s, e)}</mark>);
    at = e;
  });
  if (at < text.length) out.push(text.slice(at));
  return <>{out}</>;
}

// The second search box: the full text of the corpus, rather than its titles and
// citations. Kept separate from the hero search because it answers a different
// question and has a different scope — the index only covers the sources chosen in
// Maintain, and a search box that doesn't say what it covers is worse than none.
function FreeTextSearch({ open }: { open: (id: string, a?: string) => void }) {
  const [q, setQ] = useState("");
  const [exact, setExact] = useState(true);
  const [res, setRes] = useState<any>(null);
  const [busy, setBusy] = useState(false);
  const [scope, setScope] = useState<any>(null);
  const [showScope, setShowScope] = useState(false);
  const [picked, setPicked] = useState<string[]>([]);

  useEffect(() => {
    api.freetextScope().then((s) => {
      setScope(s);
      setPicked(s.selected || []);
    }).catch(() => {});
  }, []);

  async function run() {
    if (!q.trim()) return;
    setBusy(true);
    try {
      const r = await api.freetext({ q, exact, limit: 20, source: picked.join(",") });
      window.dispatchEvent(new CustomEvent("raglex-freetext", { detail: { q, exact, res: r } }));
      setRes(r);
    } catch (e: any) {
      setRes({ items: [], notes: [String(e?.message || e)] });
    } finally { setBusy(false); }
  }

  const indexed = scope?.indexed_total || 0;
  const sources: any[] = scope?.sources || [];
  const chosen = sources.filter((s) => picked.includes(s.source));
  const coverage = chosen.reduce((a, s) => a + s.indexed, 0);

  return (
    <div className="ft">
      <div className="hero-search">
        <input value={q} placeholder='Free text search — "quoted phrases" match literally'
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") run(); }} />
        <button className="primary" onClick={run} disabled={busy}>
          {busy ? "…" : "Search text"}</button>
      </div>
      <label className="ft-exact-top muted" title={exact
        ? 'a quoted phrase matches those characters — "duty of care" will not return "duties of care"'
        : 'a quoted phrase also matches stemmed forms — "duty of care" also returns "duties of care"'}>
        <input type="checkbox" checked={exact}
          onChange={(e) => { setExact(e.target.checked); if (res) setRes(null); }} />
        quotation marks match literally
      </label>
      <div className="ft-note muted">
        {scope?.note}
        {" "}
        <button className="linkish" onClick={() => setShowScope(!showScope)}>
          {chosen.length ? `${chosen.length} source${chosen.length > 1 ? "s" : ""}` : "no sources"}
          {coverage ? ` · ${FMT(coverage)} indexed` : ""} {showScope ? "▾" : "▸"}
        </button>
        {indexed === 0 && <span className="tag" style={{ marginLeft: 6 }}>index not built</span>}
      </div>

      {showScope && (
        <div className="ft-scope panel">
          <p className="muted" style={{ margin: "0 0 6px", fontSize: 12 }}>
            Tick the sources this box should search. Only ticked sources are indexed —
            narrowing the scope here does not delete an index, so re-ticking is free.
          </p>
          <div className="ft-sources">
            {sources.slice(0, 40).map((s) => (
              <label key={s.source} className="ft-src" title={
                `${s.indexed.toLocaleString()} of ${s.with_text.toLocaleString()} indexed`}>
                <input type="checkbox" checked={picked.includes(s.source)}
                  onChange={(e) => setPicked(e.target.checked
                    ? [...picked, s.source]
                    : picked.filter((x) => x !== s.source))} />
                <span>{s.source}</span>
                <span className="muted"> {FMT(s.with_text)}</span>
                {s.indexed > 0 && <span className="ok" title="indexed">●</span>}
              </label>
            ))}
          </div>
          <div style={{ display: "flex", gap: 8, marginTop: 8, alignItems: "center" }}>
            <button className="mini" onClick={async () => {
              await api.setFreetextScope({ sources: picked });
              setScope(await api.freetextScope());
            }}>save scope</button>
            <button className="mini" onClick={async () => {
              await api.setFreetextScope({ sources: picked });
              await api.buildFts({ sources: picked });
              setScope(await api.freetextScope());
            }}>save &amp; build index</button>
            <span className="muted" style={{ fontSize: 11 }}>
              building reads every document once — about an hour for UK+EU
            </span>
          </div>
        </div>
      )}

      {res && <FreeTextResults res={res} query={q} open={open} />}
    </div>
  );
}

function StatsRefresh({ at }: { at?: string | null }) {
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  return (
    <div style={{ textAlign: "right", fontSize: 11, marginBottom: -8 }}
         title="These corpus figures come from roll-ups refreshed weekly. Refresh to recompute now.">
      <span className="muted">figures updated {_ago(at)}</span>{" · "}
      <a style={{ cursor: busy ? "default" : "pointer", opacity: busy ? 0.5 : 0.8 }}
         onClick={async () => {
           if (busy) return;
           setBusy(true);
           try { const r = await api.rebuildCounts(); setMsg(r.error ? "error" : "refreshing…"); }
           catch { setMsg("error"); } finally { setBusy(false); }
         }}>↻ refresh</a>
      {msg && <span className="muted">{" "}{msg}</span>}
    </div>
  );
}

export function ExploreView({ open, goSearch }:
  { open: (id: string, a?: string) => void; goSearch: (q?: string) => void }) {
  const [shape, setShape] = useState<any | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const [sugg, setSugg] = useState<any[]>([]);
  const [hi, setHi] = useState(-1);
  useEffect(() => {
    let live = true;
    const load = () => api.corpusShape().then((s) => {
      if (!live) return;
      setShape(s);
      if (s._warming) setTimeout(load, 2500);  // stale-while-revalidate warm-up
    }).catch(() => {});
    load();
    return () => { live = false; };
  }, []);
  // instant find-a-document autocomplete on the hero search
  useEffect(() => {
    let live = true;
    if (q.trim().length < 2) { setSugg([]); return; }
    const t = setTimeout(async () => {
      try {
        const r = await api.searchCorpus({ query: q.trim(), limit: "6", facets: "false" });
        if (live) { setSugg(r.items || []); setHi(-1); }
      } catch { /* ignore */ }
    }, 120);
    return () => { live = false; clearTimeout(t); };
  }, [q]);

  const rows: ShapeRow[] = shape?.jurisdictions || [];
  return (
    <div className="explore">
      <StatsRefresh at={shape?.stats_refreshed_at} />
      <div className="hero">
        <h2 className="hero-title">RagLex
          <span className="muted hero-sub"> — find an authority by name or citation</span></h2>
        <div className="hero-search ac">
          <input value={q} autoFocus placeholder="Find a case, act or concept…  (⌘K jumps straight to a citation)"
            onChange={(e) => setQ(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "ArrowDown") { e.preventDefault(); setHi((h) => Math.min(h + 1, sugg.length - 1)); }
              else if (e.key === "ArrowUp") { e.preventDefault(); setHi((h) => Math.max(h - 1, -1)); }
              else if (e.key === "Enter") {
                if (hi >= 0 && sugg[hi]) open(sugg[hi].stable_id); else goSearch(q);
              } else if (e.key === "Escape") setSugg([]);
            }} />
          <button className="primary" onClick={() => goSearch(q)}>Search</button>
          {sugg.length > 0 && (
            <div className="ac-list">
              {sugg.map((o, i) => (
                <div key={o.stable_id} className={`ac-opt${i === hi ? " hi" : ""}`}
                  onMouseEnter={() => setHi(i)} onMouseDown={(e) => { e.preventDefault(); open(o.stable_id); }}>
                  <FlagIcon jurisdiction={o.jurisdiction} opacity={0.85} />
                  <span className="ac-opt-text">
                    <b><Oscola c={o.oscola} fallback={o.title || o.stable_id} /></b>
                    <span className="muted"> · {o.doc_type}{o.court ? ` · ${o.court}` : ""}</span>
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>
        <FreeTextSearch open={open} />
      </div>

      <div className="shape panel">
        <table className="shape-table">
          <thead>
            <tr><th /><th>Jurisdiction</th><th className="num">Documents</th><th>Composition</th>
              <th>Timeline</th></tr>
          </thead>
          <tbody>
            {rows.map((r) => {
              const on = expanded === r.jurisdiction;
              return (
                <Fragment key={r.jurisdiction}>
                  <tr className={`shape-row${on ? " on" : ""}`}
                    onClick={() => setExpanded(on ? null : r.jurisdiction)}>
                    <td className="chev">{on ? "▾" : "▸"}</td>
                    <td className="jname"><FlagIcon jurisdiction={r.jurisdiction} opacity={0.85} /> {r.jurisdiction}
                      <div className="muted jsub">{FMT(r.with_text)} with text · {FMT(r.embedded)} embedded</div></td>
                    <td className="num jtotal">{r.total.toLocaleString()}</td>
                    <td className="jbar"><KindBar r={r} /><KindList r={r} /></td>
                    <td className="jspark"><Spark years={r.years} /></td>
                  </tr>
                  {/* mobile only: the timeline as a full-width row beneath the jurisdiction,
                      as if the three trailing cells merged into one strip */}
                  <tr className="jspark-row-mobile" onClick={() => setExpanded(on ? null : r.jurisdiction)}>
                    <td colSpan={5}><Spark years={r.years} /></td>
                  </tr>
                  {on && <tr className="exp-row"><td colSpan={5}><Expanded r={r} open={open} /></td></tr>}
                </Fragment>
              );
            })}
          </tbody>
        </table>
        <div className="shape-foot muted">Click a row to drill in — every court, year and citation
          count is itself a filter. Ranking uses citation-network authority.</div>
      </div>
    </div>
  );
}

// --- Admin ▸ Search ---------------------------------------------------------
// One screen for both retrieval paths. They are independent — a source can be fully
// free-text indexed and carry no vectors at all — and nothing said so before, which
// is how the corpus ended up with a free-text feature that silently depended on an
// embedding pass that had never run. Everything here is per SOURCE, because a source
// is what an index is actually built over; jurisdiction is the grouping the reader
// thinks in, so the table is sorted by it.
export function SearchAdminView() {
  const [st, setSt] = useState<any>(null);
  const [picked, setPicked] = useState<string[]>([]);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [openSrc, setOpenSrc] = useState<string | null>(null);

  const load = () => api.searchStatus().then((s) => {
    setSt(s); setPicked(s.fts_selected || []); setNote(s.note || "");
  }).catch(() => {});
  useEffect(() => { load(); }, []);
  if (!st) return <div className="panel"><p className="muted">Loading…</p></div>;

  const rows: any[] = st.sources || [];
  const tot = st.totals || {};
  const chosen = rows.filter((r) => picked.includes(r.source));
  const inScopeText = chosen.reduce((a, r) => a + r.with_text, 0);
  const inScopeIndexed = chosen.reduce((a, r) => a + r.fts_indexed, 0);
  const pct = (a: number, b: number) => b ? Math.round(100 * a / b) : 0;

  async function act(key: string, fn: () => Promise<any>, done: string) {
    setBusy(key); setMsg(null);
    try { await fn(); setMsg(done); await load(); }
    catch (e: any) { setMsg(String(e?.message || e)); }
    finally { setBusy(null); }
  }

  const byJurisdiction: Record<string, any[]> = {};
  rows.forEach((r) => (byJurisdiction[r.jurisdiction] ||= []).push(r));

  return (
    <div className="search-admin">
      {/* ---- what each path covers ---- */}
      <div className="panel">
        <h3 style={{ marginTop: 0 }}>Coverage</h3>
        <div className="cov-grid">
          <div className="cov-card">
            <div className="cov-n">{FMT(tot.fts_indexed || 0)}</div>
            <div className="cov-l">documents in the free-text index</div>
            <div className="cov-bar"><span style={{ width: `${pct(tot.fts_indexed, tot.with_text)}%` }} /></div>
            <div className="muted cov-sub">{pct(tot.fts_indexed, tot.with_text)}% of {FMT(tot.with_text || 0)} with text</div>
          </div>
          <div className="cov-card">
            <div className="cov-n">{FMT(tot.embedded || 0)}</div>
            <div className="cov-l">documents with embeddings</div>
            <div className="cov-bar emb"><span style={{ width: `${pct(tot.embedded, tot.with_text)}%` }} /></div>
            <div className="muted cov-sub">
              {st.embedding.paused
                ? "embedding scope is set to none — nothing will be embedded"
                : `${st.embedding.provider}${st.embedding.model ? ` · ${st.embedding.model}` : ""}`}
            </div>
          </div>
          <div className="cov-card">
            <div className="cov-n">{FMT(inScopeText)}</div>
            <div className="cov-l">in the current free-text scope</div>
            <div className="muted cov-sub">
              {chosen.length} source{chosen.length === 1 ? "" : "s"} ticked ·
              {" "}{FMT(inScopeText - inScopeIndexed)} still to index
            </div>
          </div>
        </div>
        {msg && <p className="muted" style={{ fontSize: 12, marginBottom: 0 }}>{msg}</p>}
      </div>

      {/* ---- the note readers see ---- */}
      <div className="panel">
        <h3 style={{ marginTop: 0 }}>The note under the search box <span className="muted">
          — shown to every reader, so it should say what is actually covered</span></h3>
        <textarea className="ft-note-edit" rows={3} value={note}
          onChange={(e) => setNote(e.target.value)} />
        <div style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 6 }}>
          <button className="mini" disabled={busy === "note"}
            onClick={() => act("note", () => api.setFreetextScope({ note }), "note saved")}>
            save note</button>
          <button className="mini" disabled={busy === "note"}
            onClick={() => setNote(
              "Searches the full text of the sources selected below. " +
              'Put a phrase in "quotation marks" to match it literally.')}>reset</button>
          <span className="muted" style={{ fontSize: 11 }}>
            appears beneath the second box on the home page
          </span>
        </div>
      </div>

      {/* ---- scope + per-source status ---- */}
      <div className="panel">
        <h3 style={{ marginTop: 0 }}>Scope <span className="muted">
          — tick what the free-text box searches. Un-ticking does not delete an index.</span></h3>
        <div style={{ display: "flex", gap: 8, marginBottom: 8, flexWrap: "wrap" }}>
          <button className="mini" onClick={() => setPicked(rows.map((r) => r.source))}>all</button>
          <button className="mini" onClick={() => setPicked([])}>none</button>
          {Object.keys(byJurisdiction).sort().map((j) => (
            <button key={j} className="mini" onClick={() => setPicked([...new Set([
              ...picked, ...byJurisdiction[j].map((r) => r.source)])])}>+ {j}</button>
          ))}
          <span style={{ flex: 1 }} />
          <button className="mini" disabled={busy === "scope"}
            onClick={() => act("scope", () => api.setFreetextScope({ sources: picked }), "scope saved")}>
            save scope</button>
          <button className="primary mini" disabled={busy === "build"}
            title="reads every document in scope once; no model, no GPU"
            onClick={() => act("build", async () => {
              await api.setFreetextScope({ sources: picked });
              await api.buildFts({ sources: picked });
            }, "index build queued — watch it in Jobs")}>
            save &amp; build index</button>
        </div>
        <table className="grid search-scope">
          <thead><tr>
            <th style={{ width: 28 }} /><th>source</th><th className="num">with text</th>
            <th className="num">free text</th><th className="num">embedded</th><th />
          </tr></thead>
          <tbody>
            {Object.keys(byJurisdiction).sort().map((j) => (
              <Fragment key={j}>
                <tr className="jur-row"><td colSpan={6}>{j}</td></tr>
                {byJurisdiction[j].map((r) => (
                  <Fragment key={r.source}>
                    <tr>
                      <td><input type="checkbox" checked={picked.includes(r.source)}
                        onChange={(e) => setPicked(e.target.checked
                          ? [...picked, r.source]
                          : picked.filter((x) => x !== r.source))} /></td>
                      <td>
                        <button className="linkish" onClick={() =>
                          setOpenSrc(openSrc === r.source ? null : r.source)}>
                          {r.source} {openSrc === r.source ? "▾" : "▸"}</button>
                      </td>
                      <td className="num">{r.with_text.toLocaleString()}</td>
                      <td className="num">{r.fts_indexed
                        ? <span className="ok">{pct(r.fts_indexed, r.with_text)}%</span>
                        : <span className="muted">—</span>}</td>
                      <td className="num">{r.embedded
                        ? <span className="ok">{pct(r.embedded, r.with_text)}%</span>
                        : <span className="muted">—</span>}</td>
                      <td className="num">
                        <button className="mini" disabled={busy === r.source}
                          title="index just this source"
                          onClick={() => act(r.source,
                            () => api.buildFts({ sources: [r.source] }),
                            `queued ${r.source}`)}>index</button>
                      </td>
                    </tr>
                    {openSrc === r.source && (
                      <tr className="exp-row"><td colSpan={6}>
                        <div className="src-facets">
                          <div><b>types</b>{" "}
                            {r.doc_types.map(([t, n]: [string, number]) =>
                              <span key={t} className="tag">{t} {FMT(n)}</span>)}</div>
                          {r.courts.length > 0 && <div><b>courts / bodies</b>{" "}
                            {r.courts.map(([c, n]: [string, number]) =>
                              <span key={c} className="tag">{c} {FMT(n)}</span>)}</div>}
                        </div>
                      </td></tr>
                    )}
                  </Fragment>
                ))}
              </Fragment>
            ))}
          </tbody>
        </table>
      </div>

      {/* ---- embeddings + HPC ---- */}
      <div className="panel">
        <h3 style={{ marginTop: 0 }}>Semantic index <span className="muted">
          — separate from the above, and much more expensive</span></h3>
        <table className="grid kv">
          <tbody>
            <tr><td>provider</td><td>{st.embedding.provider}</td></tr>
            <tr><td>model</td><td>{st.embedding.model || <span className="muted">—</span>}</td></tr>
            <tr><td>dimensions</td><td>{st.embedding.dimensions || <span className="muted">—</span>}</td></tr>
            <tr><td>jurisdiction scope</td><td>
              {st.embedding.paused
                ? <span className="tag">none — embedding is switched off</span>
                : (st.embedding.jurisdictions || <span className="muted">all</span>)}</td></tr>
            <tr><td>HPC relay</td><td>
              {st.hpc.configured
                ? <>{st.hpc.host}{st.hpc.model ? ` · ${st.hpc.model}` : ""}
                    {st.hpc.tasks ? ` · ${st.hpc.tasks} tasks` : ""}</>
                : <span className="muted">not configured</span>}</td></tr>
          </tbody>
        </table>
        <p className="muted" style={{ fontSize: 12, marginBottom: 0 }}>
          Free-text search does not depend on any of this. A tsvector needs no model,
          no GPU and no HPC queue — which is the point of keeping them apart, so the
          cheap half is never blocked behind the expensive one. Change these in
          Settings ▸ Embeddings and HPC embed.
        </p>
      </div>
    </div>
  );
}

// --- free-text results, with a facet rail ------------------------------------
// The counts describe EVERY match, not the page — the server sends the whole
// matching id set with its metadata, which is also what lets a facet click narrow
// instantly rather than re-running the query. The one thing that does go back to the
// server is picking an authority outside the pre-computed "most cited" list.
type FacetState = {
  source: string[]; jurisdiction: string[]; doc_type: string[]; court: string[];
  years: [number, number] | null; cites: string | null;
};
const EMPTY_FACETS: FacetState = {
  source: [], jurisdiction: [], doc_type: [], court: [], years: null, cites: null,
};

function FacetGroup({ title, rows, picked, onToggle, max = 8 }:
  { title: string; rows: any[]; picked: string[]; onToggle: (v: string) => void; max?: number }) {
  const [all, setAll] = useState(false);
  if (!rows?.length) return null;
  const shown = all ? rows : rows.slice(0, max);
  const top = rows[0]?.n || 1;
  return (
    <div className="facet-grp">
      <h4>{title}</h4>
      {shown.map((r) => (
        <button key={r.key || r.value}
          className={`facet-row${picked.includes(r.value) ? " on" : ""}`}
          onClick={() => onToggle(r.value)}>
          <span className="facet-bar" style={{ width: `${Math.max(3, 100 * r.n / top)}%` }} />
          <span className="facet-label">{r.label || r.value}</span>
          <span className="facet-n">{FMT(r.n)}</span>
        </button>
      ))}
      {rows.length > max && (
        <button className="linkish facet-more" onClick={() => setAll(!all)}>
          {all ? "fewer" : `${rows.length - max} more`}</button>
      )}
    </div>
  );
}

export function FreeTextResults({ res, query, open, onRefine }:
  { res: any; query: string; open: (id: string, a?: string) => void;
    onRefine?: (cites: string) => void }) {
  const [f, setF] = useState<FacetState>(EMPTY_FACETS);
  const [citeQ, setCiteQ] = useState("");
  useEffect(() => { setF(EMPTY_FACETS); setCiteQ(""); }, [query, res]);
  if (!res) return null;

  const items: any[] = res.items || [];
  const facets = res.facets || {};
  const cites: any[] = res.network?.cites || [];
  const toggle = (k: keyof FacetState) => (v: string) => setF((p) => {
    const cur = p[k] as string[];
    return { ...p, [k]: cur.includes(v) ? cur.filter((x) => x !== v) : [...cur, v] };
  });

  // Narrowing happens over the loaded page here; the whole matching set is in
  // res.matched, so a future "load more" pages through the same filtered set
  // without a second query.
  const citeSet = f.cites
    ? new Set((cites.find((c) => c.stable_id === f.cites)?.src_ids) || [])
    : null;
  const shown = items.filter((it) => {
    if (f.source.length && !f.source.includes(it.source)) return false;
    if (f.jurisdiction.length && !f.jurisdiction.includes(it.jurisdiction)) return false;
    if (f.doc_type.length && !f.doc_type.includes(it.doc_type)) return false;
    if (f.court.length && !f.court.includes(it.court)) return false;
    if (f.years && it.decision_date) {
      const y = +String(it.decision_date).slice(0, 4);
      if (y < f.years[0] || y > f.years[1]) return false;
    }
    if (citeSet && !citeSet.has(it.stable_id)) return false;
    return true;
  });
  const active = f.source.length + f.jurisdiction.length + f.doc_type.length
    + f.court.length + (f.years ? 1 : 0) + (f.cites ? 1 : 0);
  const citeOpts = cites.filter((c) => !citeQ ||
    (c.title || c.stable_id).toLowerCase().includes(citeQ.toLowerCase()));

  return (
    <div className="ftr">
      <aside className="ftr-rail">
        <div className="ftr-count">
          <b>{(res.verified ?? res.total ?? 0).toLocaleString()}</b> document
          {(res.verified ?? res.total) === 1 ? "" : "s"}
          {res.truncated && <span className="tag" title="the candidate budget was reached — narrow the query for an exact count">+</span>}
          <div className="muted">{res.took_ms}ms{res.exact ? " · exact quotes" : " · stemmed"}</div>
          {active > 0 && (
            <button className="mini" onClick={() => setF(EMPTY_FACETS)}>
              clear {active} filter{active === 1 ? "" : "s"}</button>
          )}
        </div>

        {/* cites — the one facet that is about the network rather than the metadata */}
        <div className="facet-grp">
          <h4>cites</h4>
          <input className="facet-cite" value={citeQ} placeholder="an authority…"
            onChange={(e) => setCiteQ(e.target.value)} />
          {f.cites && (
            <button className="facet-row on" onClick={() => setF({ ...f, cites: null })}>
              <span className="facet-label">
                ✕ {cites.find((c) => c.stable_id === f.cites)?.title || f.cites}</span>
            </button>
          )}
          {!f.cites && citeOpts.slice(0, citeQ ? 8 : 6).map((c) => (
            <button key={c.stable_id} className="facet-row"
              title={c.stable_id}
              onClick={() => setF({ ...f, cites: c.stable_id })}>
              <span className="facet-bar" style={{
                width: `${Math.max(3, 100 * c.citing / (cites[0]?.citing || 1))}%` }} />
              <span className="facet-label">{c.title || c.stable_id}</span>
              <span className="facet-n">{c.citing}</span>
            </button>
          ))}
          {citeQ && citeOpts.length === 0 && onRefine && (
            <button className="linkish facet-more" onClick={() => onRefine(citeQ)}>
              search the whole corpus for “{citeQ}”…</button>
          )}
          <p className="muted facet-hint">
            what these results have in common — the authorities they cite between them
          </p>
        </div>

        <YearFacet years={facets.years} undated={facets.undated}
          value={f.years} onChange={(v) => setF({ ...f, years: v })} />
        <FacetGroup title="jurisdiction" rows={facets.jurisdiction}
          picked={f.jurisdiction} onToggle={toggle("jurisdiction")} />
        <FacetGroup title="type" rows={facets.doc_type}
          picked={f.doc_type} onToggle={toggle("doc_type")} />
        <FacetGroup title="court / body" rows={facets.court}
          picked={f.court} onToggle={toggle("court")} max={10} />
        <FacetGroup title="source" rows={facets.source}
          picked={f.source} onToggle={toggle("source")} />
      </aside>

      <div className="ftr-list">
        {res.notes?.map((n: string, i: number) => (
          <div key={i} className="ft-warn">{n}</div>
        ))}
        {active > 0 && (
          <div className="muted ftr-narrow">
            showing {shown.length} of {items.length} loaded
          </div>
        )}
        {shown.map((it: any) => (
          <div key={it.stable_id + it.char_start} className="ft-hit">
            <DocLink id={it.stable_id} anchor={it.anchor}
              onOpen={() => open(it.stable_id, it.anchor)}>
              <b><Oscola c={it.oscola} fallback={it.title || it.stable_id} /></b>
            </DocLink>
            <span className="muted ft-meta">
              {" "}· {it.court_label || it.court || it.source}
              {it.decision_date ? ` · ${String(it.decision_date).slice(0, 4)}` : ""}
              {it.anchor ? ` · ${it.anchor}` : ""}
            </span>
            <div className="ft-snip"><Marked text={it.snippet} spans={it.highlights} /></div>
          </div>
        ))}
        {shown.length === 0 && items.length > 0 && (
          <p className="muted">Nothing in the loaded results matches those filters.</p>
        )}
      </div>
    </div>
  );
}

// The result set's shape in time, doubling as a range brush. Legal research is
// period-sensitive — "since the Human Rights Act", "before Caparo" — so this is a
// primary control rather than a decoration.
function YearFacet({ years, undated, value, onChange }:
  { years?: { year: string; n: number }[]; undated?: number;
    value: [number, number] | null; onChange: (v: [number, number] | null) => void }) {
  if (!years?.length) return null;
  const map: Record<string, number> = {};
  years.forEach((y) => (map[y.year] = y.n));
  const lo = +years[0].year, hi = +years[years.length - 1].year;
  return (
    <div className="facet-grp">
      <h4>date {value && (
        <button className="linkish" onClick={() => onChange(null)}>
          {value[0]}–{value[1]} ✕</button>)}</h4>
      <Spark years={map} width={190} height={34} brush
        active={value ? [String(value[0]), String(value[1])] : null}
        onBrush={(a, b) => onChange([+a, +b])} />
      <div className="muted facet-hint">
        {lo}–{hi}{undated ? ` · ${FMT(undated)} undated` : ""} · drag to filter
      </div>
    </div>
  );
}
