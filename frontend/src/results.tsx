// The result surface, shared by both searches.
//
// There were two of these: a facet sidebar for the metadata search and a separate
// rail built for free text, which diverged the day the second was written — different
// facet markup, different year controls, one with match highlighting and one without.
// They answer different queries but they are the same *thing* on screen, so the
// difference belongs in the data each supplies, not in duplicated components.
//
// The two data models genuinely differ and that is expressed as props rather than as
// separate code. The metadata search filters and facets SERVER-side and pages through
// results, so its facets are single-select and a click re-queries. Free text holds the
// whole matching id set in the browser — the counts have to describe every match, not
// the page — so its facets are multi-select and a click narrows locally with no round
// trip. `mode` is the only thing that knows which.
import { Fragment, useEffect, useRef, useState } from "react";

export type FacetValue = { value: string; label?: string; n: number };
export type FacetDim = { key: string; title: string; rows: FacetValue[]; max?: number };
export type ActiveFacets = Record<string, string[]>;

const FMT = (n: number) => n >= 1_000_000 ? (n / 1_000_000).toFixed(1) + "M"
  : n >= 10_000 ? Math.round(n / 1000) + "k"
  : n >= 1000 ? (n / 1000).toFixed(1) + "k" : String(n);

// --- highlighting ------------------------------------------------------------
// Offsets, not HTML: the backend sends spans as numbers so nothing user-supplied is
// ever interpreted as markup.
export function Marked({ text, spans }: { text: string; spans?: number[][] }) {
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

// --- one facet dimension -----------------------------------------------------
function FacetGroup({ dim, picked, onToggle }:
  { dim: FacetDim; picked: string[]; onToggle: (v: string) => void }) {
  const [all, setAll] = useState(false);
  const max = dim.max ?? 8;
  if (!dim.rows?.length) return null;
  const shown = all ? dim.rows : dim.rows.slice(0, max);
  const top = dim.rows[0]?.n || 1;
  return (
    <div className="facet-grp">
      <h4>{dim.title}</h4>
      {shown.map((r) => (
        <button key={r.value} className={`facet-row${picked.includes(r.value) ? " on" : ""}`}
          title={r.value} onClick={() => onToggle(r.value)}>
          <span className="facet-bar" style={{ width: `${Math.max(3, 100 * r.n / top)}%` }} />
          <span className="facet-label">{r.label || r.value}</span>
          <span className="facet-n">{FMT(r.n)}</span>
        </button>
      ))}
      {dim.rows.length > max && (
        <button className="linkish facet-more" onClick={() => setAll(!all)}>
          {all ? "fewer" : `${dim.rows.length - max} more`}</button>
      )}
    </div>
  );
}

// --- the year distribution, brushable ----------------------------------------
// Legal research is period-sensitive — "since the Human Rights Act", "before Caparo"
// — so a range is the natural control and this is a primary filter, not a decoration.
export function YearHistogram({ years, from, to, onRange, onClear, undated }:
  { years: Record<string, number>; from?: string; to?: string;
    onRange: (from: string, to: string) => void; onClear: () => void; undated?: number }) {
  const [drag, setDrag] = useState<{ a: string; b: string } | null>(null);
  const dragRef = useRef<typeof drag>(null);
  dragRef.current = drag;
  const ys = Object.keys(years).filter((y) => /^\d{4}$/.test(y)).sort();
  // a release outside the bars still commits the brush
  useEffect(() => {
    if (!drag) return;
    const up = () => {
      const d = dragRef.current;
      if (d) { const [a, b] = [d.a, d.b].sort(); onRange(a, b); }
      setDrag(null);
    };
    window.addEventListener("mouseup", up);
    return () => window.removeEventListener("mouseup", up);
  }, [!drag]);
  if (ys.length < 2) return null;
  const max = Math.max(...ys.map((y) => years[y]));
  const lo = ys[0], hi = ys[ys.length - 1];
  const inSel = (y: string) => {
    if (drag) { const [a, b] = [drag.a, drag.b].sort(); return y >= a && y <= b; }
    return !!(from || to) && y >= (from || "0000") && y <= (to || "9999");
  };
  const label = drag ? [drag.a, drag.b].sort().join("–")
    : (from || to) ? `${from || lo}–${to || hi}` : null;
  return (
    <div className="facet-grp">
      <h4>date {label && <span className="histo-range">{label}</span>}
        {(from || to) && !drag && (
          <button className="linkish" onClick={onClear}>clear</button>)}</h4>
      <div className="histo" title="click a year, or drag to select a range">
        {ys.map((y) => (
          <div key={y} className={`histo-bar${inSel(y) ? " on" : ""}`}
            style={{ height: `${Math.max(3, (years[y] / max) * 40)}px` }}
            title={`${y}: ${years[y].toLocaleString()}`}
            onMouseDown={(e) => { e.preventDefault(); setDrag({ a: y, b: y }); }}
            onMouseEnter={() => setDrag((d) => (d ? { ...d, b: y } : d))} />
        ))}
      </div>
      <div className="histo-axis"><span>{lo}</span><span>{hi}</span></div>
      {undated ? <div className="muted facet-hint">{FMT(undated)} undated</div> : null}
    </div>
  );
}

// --- the rail ----------------------------------------------------------------
export function FacetRail({ dims, active, onToggle, years, yearFrom, yearTo, onYearRange,
                            onYearClear, undated, header, extra }:
  { dims: FacetDim[]; active: ActiveFacets; onToggle: (dim: string, value: string) => void;
    years?: Record<string, number>; yearFrom?: string; yearTo?: string;
    onYearRange?: (a: string, b: string) => void; onYearClear?: () => void;
    undated?: number; header?: any; extra?: any }) {
  return (
    <aside className="ftr-rail">
      {header}
      {extra}
      {years && onYearRange && onYearClear && (
        <YearHistogram years={years} from={yearFrom} to={yearTo}
          onRange={onYearRange} onClear={onYearClear} undated={undated} />
      )}
      {dims.map((d) => (
        <FacetGroup key={d.key} dim={d} picked={active[d.key] || []}
          onToggle={(v) => onToggle(d.key, v)} />
      ))}
    </aside>
  );
}

// --- one result --------------------------------------------------------------
// `snippet`/`highlights` are optional: the metadata search has no matched passage to
// show, the free-text one always does.
export function ResultRow({ it, children, link }:
  { it: any; children?: any; link: (it: any) => any }) {
  return (
    <div className="ft-hit">
      {link(it)}
      <span className="muted ft-meta">
        {" "}· {it.court_label || it.court || it.source}
        {it.decision_date ? ` · ${String(it.decision_date).slice(0, 4)}` : ""}
        {it.anchor ? ` · ${it.anchor}` : ""}
      </span>
      {it.snippet && (
        <div className="ft-snip"><Marked text={it.snippet} spans={it.highlights} /></div>
      )}
      {children}
    </div>
  );
}

// --- adapters ----------------------------------------------------------------
// Each search reports its facets in the shape its own endpoint already used; these
// bring both to one vocabulary rather than changing two APIs to suit the UI.
export function dimsFromFreetext(facets: any): FacetDim[] {
  if (!facets) return [];
  return [
    { key: "jurisdiction", title: "jurisdiction", rows: facets.jurisdiction || [] },
    { key: "doc_type", title: "type", rows: facets.doc_type || [] },
    { key: "court", title: "court / body", rows: facets.court || [], max: 10 },
    { key: "source", title: "source", rows: facets.source || [] },
  ].filter((d) => d.rows.length);
}

export function dimsFromCorpus(facets: any): FacetDim[] {
  if (!facets) return [];
  const conv = (rows: any[]) =>
    (rows || []).map((r) => ({ value: r.key, label: r.label || r.key, n: r.n }));
  return [
    { key: "source", title: "source", rows: conv(facets.source) },
    { key: "doc_type", title: "type", rows: conv(facets.doc_type) },
    { key: "court", title: "court / body", rows: conv(facets.court), max: 10 },
  ].filter((d) => d.rows.length);
}

export function yearsFromFreetext(facets: any): Record<string, number> {
  const out: Record<string, number> = {};
  (facets?.years || []).forEach((y: any) => (out[y.year] = y.n));
  return out;
}

export { FMT as fmtCount, Fragment };
