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
import { Oscola } from "./views";

const FMT = (n: number) => n >= 1_000_000 ? (n / 1_000_000).toFixed(1) + "M"
  : n >= 10_000 ? Math.round(n / 1000) + "k"
  : n >= 1000 ? (n / 1000).toFixed(1) + "k" : String(n);

const KIND_COLOURS: [string, string, string][] = [
  ["cases", "var(--exp-cases)", "case law"],
  ["legislation", "var(--exp-leg)", "legislation"],
  ["guidance", "var(--exp-guid)", "guidance"],
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
  "": "documents", cases: "case law", legislation: "legislation", guidance: "guidance",
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
    ["opinion", "opinions"], ["guidance", "guidance"], ["legislation", "legislation"]];
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
            ["guidance", "Guidance"], ["administrative", "Admin decisions"]].map(([v, l]) => (
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
              <a onClick={() => open(it.id)}><Oscola c={it.oscola} fallback={it.title || it.id} /></a>
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
      <div className="hero">
        <h2 className="hero-title">{shape?.total ? `${shape.total.toLocaleString()} documents` : "RagLex"}
          <span className="muted hero-sub"> — case law, legislation and guidance across {rows.length || "…"} jurisdictions</span></h2>
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
                  <b><Oscola c={o.oscola} fallback={o.title || o.stable_id} /></b>
                  <span className="muted"> · {o.doc_type}{o.court ? ` · ${o.court}` : ""}</span>
                </div>
              ))}
            </div>
          )}
        </div>
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
                    <td className="jname">{r.jurisdiction}
                      <div className="muted jsub">{FMT(r.with_text)} with text · {FMT(r.embedded)} embedded</div></td>
                    <td className="num jtotal">{r.total.toLocaleString()}</td>
                    <td className="jbar"><KindBar r={r} /></td>
                    <td className="jspark"><Spark years={r.years} /></td>
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
