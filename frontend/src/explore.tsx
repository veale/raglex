// Explore — the homepage. One screen that puts the corpus's SHAPE in your head:
// a search bar, then a jurisdiction table (counts by kind as a proportional bar,
// a year sparkline, citation density, coverage, the most authoritative document)
// where every element drills DOWN IN PLACE — expanding to courts, kind filters, a
// brushable timeline, and authority-ranked documents with their hanging groupings
// (what cites a statute: cases / guidance / sibling legislation) — rather than
// bouncing to a prefilled search page. PageRank does the ranking throughout.
import { Fragment, useEffect, useRef, useState } from "react";
import { api } from "./api";
import { Oscola } from "./views";

const FMT = (n: number) => n >= 1_000_000 ? (n / 1_000_000).toFixed(1) + "M"
  : n >= 10_000 ? Math.round(n / 1000) + "k"
  : n >= 1000 ? (n / 1000).toFixed(1) + "k" : String(n);

const KIND_COLOURS: [keyof ShapeRow, string, string][] = [
  ["cases", "var(--exp-cases)", "case law"],
  ["legislation", "var(--exp-leg)", "legislation"],
  ["guidance", "var(--exp-guid)", "guidance"],
  ["other", "var(--exp-other)", "other"],
];

type ShapeRow = {
  jurisdiction: string; total: number; cases: number; legislation: number;
  guidance: number; other: number; with_text: number; embedded: number;
  density: number; years: Record<string, number>;
  courts: { court: string; n: number }[];
  sources: { source: string; n: number }[];
  top_authority: { id: string; title: string; doc_type: string; date: string | null;
    percentile: number | null; oscola: any }[];
};

// --- tiny SVG year sparkline (optionally brushable) --------------------------
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
    return Math.round(lo + ((clientX - r.left) / r.width) * span);
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
  );
}

// proportional kind bar — the row's at-a-glance composition
function KindBar({ r }: { r: ShapeRow }) {
  return (
    <div className="kindbar" title={KIND_COLOURS.map(([k, , label]) =>
      `${label}: ${(r[k] as number).toLocaleString()}`).join(" · ")}>
      {KIND_COLOURS.map(([k, colour]) => {
        const frac = (r[k] as number) / (r.total || 1);
        return frac > 0.004 && <span key={k} style={{ width: `${frac * 100}%`, background: colour }} />;
      })}
    </div>
  );
}

function Pct({ p }: { p: number | null }) {
  if (p == null || p < 50) return null;
  return <span className="auth-badge" title={`network authority: above ${p.toFixed(0)}% of cited documents`}>
    top {Math.max(1, Math.round(100 - p))}%</span>;
}

// --- the drill panel: authority-ranked documents of a slice ------------------
function DrillPanel({ jurisdiction, court, years, open }:
  { jurisdiction: string; court: string | null; years: [string, string] | null;
    open: (id: string, a?: string) => void }) {
  const [kind, setKind] = useState<string>("");
  const [data, setData] = useState<any | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    let live = true;
    setBusy(true);
    const p: Record<string, string> = { jurisdiction };
    if (court) p.court = court;
    if (kind) p.kind = kind;
    if (years) { p.year_from = years[0]; p.year_to = years[1]; }
    api.drill(p).then((d) => { if (live) setData(d); }).catch(() => live && setData({ items: [] }))
      .finally(() => live && setBusy(false));
    return () => { live = false; };
  }, [jurisdiction, court, kind, years?.[0], years?.[1]]);
  const HANG: [string, string, string][] = [
    ["judgment", "⚖", "cases citing this"], ["decision", "⚖", "decisions citing this"],
    ["guidance", "◈", "guidance citing this"], ["legislation", "§", "legislation citing this"],
  ];
  return (
    <div className="drill">
      <div className="drill-head">
        <div className="seg-toggle mini-toggle">
          {[["", "All"], ["cases", "Cases"], ["legislation", "Legislation"], ["guidance", "Guidance"]].map(([v, l]) => (
            <button key={v} className={kind === v ? "on" : ""} onClick={() => setKind(v)}>{l}</button>
          ))}
        </div>
        {busy && <span className="muted drill-busy">…</span>}
      </div>
      <ol className="drill-list">
        {(data?.items || []).map((it: any, i: number) => (
          <li key={it.id}>
            <span className="drill-rank">{i + 1}</span>
            <div className="drill-doc">
              <a onClick={() => open(it.id)}><Oscola c={it.oscola} fallback={it.title || it.id} /></a>
              <div className="drill-meta muted">
                <span className="tag">{it.doc_type}</span>
                {it.court && <span>{it.court}</span>}
                {it.date && <span>{it.date.slice(0, 4)}</span>}
                {it.cited_by > 0 && <span>cited by {it.cited_by.toLocaleString()}</span>}
                <Pct p={it.percentile} />
              </div>
              {it.hanging && Object.keys(it.hanging).length > 0 && (
                <div className="hanging">
                  {HANG.filter(([k]) => it.hanging[k]).map(([k, icon, label]) => (
                    <a key={k} className="hang-chip" title={label}
                      onClick={() => open(it.id)}>{icon} {FMT(it.hanging[k])} {label.split(" ")[0]}</a>
                  ))}
                </div>
              )}
            </div>
          </li>
        ))}
      </ol>
      {data && !data.items.length && !busy && <p className="muted">Nothing in this slice{kind ? " of that kind" : ""}.</p>}
    </div>
  );
}

// --- one expanded jurisdiction: courts rail + timeline + drill ---------------
function Expanded({ r, open }: { r: ShapeRow; open: (id: string, a?: string) => void }) {
  const [court, setCourt] = useState<string | null>(null);
  const [years, setYears] = useState<[string, string] | null>(null);
  return (
    <div className="exp-detail">
      <div className="exp-rail">
        <div className="exp-rail-title">Timeline <span className="muted">drag to focus</span></div>
        <Spark years={r.years} width={280} height={44} brush active={years}
          onBrush={(a, b) => setYears(a === b ? [a, a] : [a, b])} />
        {years && <a className="mini-link" onClick={() => setYears(null)}>clear {years[0]}–{years[1]} ✕</a>}
        {r.courts.length > 0 && <>
          <div className="exp-rail-title">Courts &amp; bodies</div>
          <ul className="court-list">
            <li><a className={!court ? "on" : ""} onClick={() => setCourt(null)}>all</a></li>
            {r.courts.map((c) => (
              <li key={c.court}>
                <a className={court === c.court ? "on" : ""} onClick={() => setCourt(court === c.court ? null : c.court)}>
                  <span className="court-name">{c.court}</span>
                  <span className="court-n">{FMT(c.n)}</span>
                </a>
              </li>
            ))}
          </ul>
        </>}
        <div className="exp-rail-title">Sources</div>
        <div className="src-chips">
          {r.sources.map((s) => <span key={s.source} className="tag" title={`${s.n.toLocaleString()} documents`}>{s.source}</span>)}
        </div>
      </div>
      <DrillPanel jurisdiction={r.jurisdiction} court={court} years={years} open={open} />
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
          <span className="muted hero-sub"> — case law, legislation &amp; guidance across {rows.length || "…"} jurisdictions</span></h2>
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
              <th>Timeline</th><th className="num" title="resolved citations per document">Density</th>
              <th>Leading authority</th></tr>
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
                    <td className="num">{r.density ? `${r.density}×` : "—"}</td>
                    <td className="jauth">
                      {r.top_authority[0] && (
                        <a onClick={(e) => { e.stopPropagation(); open(r.top_authority[0].id); }}>
                          <Oscola c={r.top_authority[0].oscola}
                            fallback={r.top_authority[0].title || r.top_authority[0].id} /></a>
                      )}
                    </td>
                  </tr>
                  {on && <tr className="exp-row"><td colSpan={7}><Expanded r={r} open={open} /></td></tr>}
                </Fragment>
              );
            })}
          </tbody>
        </table>
        <div className="kind-legend muted">
          {KIND_COLOURS.map(([k, colour, label]) => (
            <span key={k}><i style={{ background: colour }} />{label}</span>
          ))}
          <span className="legend-hint">click a row to drill in · everything ranks by network authority (PageRank)</span>
        </div>
      </div>
    </div>
  );
}
