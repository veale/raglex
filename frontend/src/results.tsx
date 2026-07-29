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

// What "influential" means, in one hover. PageRank is not a legal concept and the
// word "authority" is — a lawyer reading "most authoritative" would reasonably take
// it to mean binding precedent, which this is not. So the label says influence and
// the dot says what influence is measured from.
export const INFLUENCE_EXPLAINER =
  "Influence is measured from the citation network: a document ranks higher when the "
  + "documents citing it are themselves widely cited. One citation from a leading "
  + "Supreme Court judgment therefore counts for more than many from routine "
  + "decisions. It reflects how much a document is built on, not whether it binds a "
  + "court.";

export function InfoDot({ text }: { text: string }) {
  return (
    <span className="info-dot" title={text} aria-label={text} role="img">i</span>
  );
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
          // the label is ellipsised to fit the rail, so the full name belongs on hover
          title={r.label && r.label !== r.value ? `${r.label} (${r.value})` : r.value}
          onClick={() => onToggle(r.value)}>
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
  // where the brush started, so dragging back the other way SHRINKS the selection
  // instead of growing it (each move recomputes the range from the anchor)
  const anchor = useRef<{ a: string; b: string } | null>(null);
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
  const lo = ys[0], hi = ys[ys.length - 1];
  // The rail is ~190px wide and the corpus runs from the 13th century to this year, so
  // one bar per year (min-width 2px + a 1px gap) ran to several thousand pixels and
  // spilled out of the rail. Bucket the span into at most MAX_BARS columns — a bar is
  // then a run of years, and brushing still yields a year range, so the filter is
  // unchanged; only the granularity of a single click is.
  const MAX_BARS = 44;
  const span = +hi - +lo + 1;
  const per = Math.max(1, Math.ceil(span / MAX_BARS));
  const bins: { a: string; b: string; n: number }[] = [];
  for (let y = +lo; y <= +hi; y += per) {
    const end = Math.min(y + per - 1, +hi);
    let n = 0;
    for (let k = y; k <= end; k++) n += years[String(k)] || 0;
    bins.push({ a: String(y), b: String(end), n });
  }
  const max = Math.max(...bins.map((b) => b.n), 1);
  const inSel = (bin: { a: string; b: string }) => {
    // a bucket is selected when the selection overlaps ANY of its years
    if (drag) { const [a, b] = [drag.a, drag.b].sort(); return bin.b >= a && bin.a <= b; }
    return !!(from || to) && bin.b >= (from || "0000") && bin.a <= (to || "9999");
  };
  const label = drag ? [drag.a, drag.b].sort().join("–")
    : (from || to) ? `${from || lo}–${to || hi}` : null;
  return (
    <div className="facet-grp">
      <h4>date {label && <span className="histo-range">{label}</span>}
        {(from || to) && !drag && (
          <button className="linkish" onClick={onClear}>clear</button>)}</h4>
      <div className="histo" title={per > 1
        ? `click a ${per}-year period, or drag to select a range`
        : "click a year, or drag to select a range"}>
        {bins.map((bin) => (
          <div key={bin.a} className={`histo-bar${inSel(bin) ? " on" : ""}`}
            style={{ height: `${Math.max(3, (bin.n / max) * 40)}px` }}
            title={`${bin.a === bin.b ? bin.a : `${bin.a}–${bin.b}`}: ${bin.n.toLocaleString()}`}
            onMouseDown={(e) => {
              e.preventDefault();
              anchor.current = { a: bin.a, b: bin.b };
              setDrag({ a: bin.a, b: bin.b });
            }}
            onMouseEnter={() => setDrag((d) => {
              const an = anchor.current;
              if (!d || !an) return d;
              return { a: an.a < bin.a ? an.a : bin.a, b: an.b > bin.b ? an.b : bin.b };
            })} />
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
export function ResultRow({ it, children, link, onOpen }:
  { it: any; children?: any; link: (it: any) => any;
    onOpen?: (id: string, anchor?: string) => void }) {
  return (
    <div className="ft-hit">
      {link(it)}
      <span className="muted ft-meta">
        {" "}· {it.court_label || it.court || it.source}
        {it.decision_date ? ` · ${String(it.decision_date).slice(0, 4)}` : ""}
        {it.anchor ? ` · ${it.anchor}` : ""}
        {it.cited_by ? <span className="ft-cited" title={
          `${it.cited_by.toLocaleString()} documents in the corpus cite this`}>
          {" "}· cited by {FMT(it.cited_by)}</span> : null}
      </span>
      {it.snippet && (
        <div className="ft-snip"><Marked text={it.snippet} spans={it.highlights} /></div>
      )}
      <MorePassages it={it} onOpen={onOpen} />
      {children}
    </div>
  );
}


// How many times the document says it, and where. One mention in passing and eight
// sustained uses are different kinds of hit, and showing only the first passage hides
// the difference. Collapsed by default — the extra previews are the reason to expand,
// not something to scroll past.
function MorePassages({ it, onOpen }:
  { it: any; onOpen?: (id: string, anchor?: string) => void }) {
  const [open, setOpen] = useState(false);
  const rest: any[] = (it.passages || []).slice(1);
  if (!rest.length) return null;
  return (
    <div className="ft-more">
      <button className="linkish" onClick={() => setOpen(!open)}>
        {open ? "fewer passages" : `and ${rest.length} more passage${rest.length === 1 ? "" : "s"}`}
      </button>
      {open && rest.map((p: any, i: number) => (
        <div key={i} className="ft-passage"
          onClick={() => onOpen?.(it.stable_id, p.anchor)}
          title={p.anchor ? `open at ${p.anchor}` : "open the document here"}>
          {p.anchor && <span className="ft-passage-anchor">{p.anchor}</span>}
          <Marked text={p.snippet} spans={p.highlights} />
        </div>
      ))}
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
