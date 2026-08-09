import { Component, createContext, Fragment, lazy, Suspense, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { api, CanliiBudget, Hit, ImportBatchResult, ImportItem, ImportOptions, LIIScope, LIITarget, Setting, StaticBundle, StaticBundleItem, StaticBundleWebhook, UsCaselawBudget } from "./api";
import { AcMore, useAutosuggest } from "./autosuggest";
import { useAuth } from "./auth";
import { DocLink, docHref, opensNewTab } from "./links";
import { FacetRail, INFLUENCE_EXPLAINER, InfoDot, dimsFromCorpus } from "./results";
import { ACTIONS, CADENCE_META, GROUP_ORDER, type Action as MaintAction } from "./maintenance";

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
  | { kind: "mentions"; target: string; anchor?: string; exact?: boolean; label: any }
  | { kind: "cites"; target: string; family: "cases" | "statute"; label: any }
  | { kind: "doc"; id: string; highlightTarget?: string; highlightAnchor?: string;
      occurrenceStart?: number; label: any };
const TrayCtx = createContext<{ stack: Tray[]; push: (t: Tray) => void; closeAt: (i: number) => void } | null>(null);
export function useTray() {
  return useContext(TrayCtx) ?? { stack: [] as Tray[], push: (_t: Tray) => {}, closeAt: (_i: number) => {} };
}
export function TrayProvider({ children }: { children: any }) {
  const [stack, setStack] = useState<Tray[]>([]);
  useEffect(() => { document.body.classList.toggle("has-tray", stack.length > 0); }, [stack.length]);
  const push = (t: Tray) => setStack((s) => [...s, t]);
  // A tray's own × dismisses that tray only. Lower trays are not parents whose
  // removal should cascade through everything opened later; the remaining stack
  // simply closes up the visual gap. Escape still targets only the current top tray.
  const closeAt = (i: number) => setStack((s) => s.filter((_, j) => j !== i));
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
      <div className="tray-body"><TrayErrorBoundary><TrayContent t={t} open={open} /></TrayErrorBoundary></div>
    </aside>
  ))}</>;
}

// A single unexpected legacy metadata value must not unmount the whole React root.
// Besides making the overlay recoverable, this leaves a useful error in the tray
// instead of Firefox's otherwise featureless white screen.
class TrayErrorBoundary extends Component<{ children: any }, { error: string }> {
  state = { error: "" };
  static getDerivedStateFromError(error: unknown) {
    return { error: error instanceof Error ? error.message : String(error) };
  }
  componentDidCatch(error: unknown) { console.error("RagLex tray render failed", error); }
  render() {
    return this.state.error
      ? <div className="error-box">Could not display this panel: {this.state.error}</div>
      : this.props.children;
  }
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
  if (t.kind === "doc") return <MentionReader id={t.id} highlightTarget={t.highlightTarget}
    highlightAnchor={t.highlightAnchor} occurrenceStart={t.occurrenceStart} open={open} />;
  if (t.kind === "cites") return <CitesTray target={t.target} family={t.family} open={open} />;
  return <MentionsTray target={t.target} anchor={t.anchor} exact={t.exact} open={open} />;
}

// A mention snippet with the citation that produced the edge marked out — the reader
// can see *which words* linked the two documents ("Arbitration Act s 7") rather than
// having to hunt for them in the surrounding prose. `mark` is a [start, end] offset
// pair into `text`; without it the snippet renders plain.
function SnipText({ s }: { s: any }) {
  const text = typeof s?.text === "string" ? s.text : String(s?.text ?? "");
  const m: [number, number] | null = s.mark || null;
  if (!m || m[1] <= m[0] || m[1] > text.length) return <>{text}</>;
  return (
    <>
      {text.slice(0, m[0])}
      <mark className="msnip-cite" title={s.raw ? `matched: ${s.raw}` : undefined}>
        {text.slice(m[0], m[1])}</mark>
      {text.slice(m[1])}
    </>
  );
}

// Jurisdiction → ISO-3166 alpha-2 for the circle-flags icon set. Accepts the corpus's
// human-readable bucket names ("United Kingdom", "European Union") and bare codes.
const _FLAG_CODE: Record<string, string> = {
  "european union": "eu", "united kingdom": "gb", "uk": "gb", "gb": "gb", "eu": "eu",
  // The Council of Europe (ECtHR / ECHR) created and shares the European flag with the EU.
  "council of europe": "eu", coe: "eu", "european court of human rights": "eu", ecthr: "eu",
  england: "gb", "england & wales": "gb", scotland: "gb", "northern ireland": "gb",
  ireland: "ie", "data protection commission (ireland)": "ie",
  germany: "de", france: "fr", netherlands: "nl", italy: "it", spain: "es",
  belgium: "be", austria: "at", poland: "pl", greece: "gr", romania: "ro",
  hungary: "hu", sweden: "se", denmark: "dk", finland: "fi", norway: "no",
  iceland: "is", portugal: "pt", czechia: "cz", "czech republic": "cz",
  slovakia: "sk", slovenia: "si", croatia: "hr", bulgaria: "bg", lithuania: "lt",
  latvia: "lv", estonia: "ee", luxembourg: "lu", malta: "mt", cyprus: "cy",
  liechtenstein: "li", australia: "au", "united states": "us", usa: "us", us: "us",
  canada: "ca", "new zealand": "nz", singapore: "sg", "hong kong": "hk", india: "in",
};

function flagCode(jurisdiction?: string | null): string | null {
  if (!jurisdiction) return null;
  return _FLAG_CODE[jurisdiction.trim().toLowerCase()] || null;
}

// A small circle flag (HatScripts/circle-flags). Renders nothing for an unmapped
// jurisdiction so mixed/unknown buckets simply carry no flag.
// `size` is in em, so the flag scales with the surrounding text (a header flag grows
// with the header). Bundled locally (frontend/public/flags, HatScripts/circle-flags).
export function FlagIcon({ jurisdiction, size = 1, opacity, placeholder }:
  { jurisdiction?: string | null; size?: number; opacity?: number; placeholder?: boolean }) {
  const cc = flagCode(jurisdiction);
  // In a ROW of flagged labels ("Other" beside four flagged jurisdictions), the one
  // without a flag has a different baseline from the ones with an image and sits lower.
  // `placeholder` keeps its space so every chip in the row lines up.
  if (!cc) return placeholder
    ? <span className="flag-icon flag-none" aria-hidden="true"
        style={{ width: `${size}em`, height: `${size}em` }} />
    : null;
  return (
    <img className="flag-icon" loading="lazy"
      src={`${import.meta.env.BASE_URL}flags/${cc}.svg`}
      alt={jurisdiction || ""} title={jurisdiction || ""}
      style={{ width: `${size}em`, height: `${size}em`, verticalAlign: "-0.15em",
               borderRadius: "50%", flex: "0 0 auto",
               ...(opacity != null ? { opacity } : {}) }} />
  );
}

// Grouped-by-citer mentions of a document (or one of its paragraphs), most-authoritative
// first — with the passages where each cites it, and a jump to the full citing document.
function MentionsTray({ target, anchor, exact, open }: { target: string; anchor?: string; exact?: boolean; open: (id: string, a?: string) => void }) {
  const { push } = useTray();
  // PageRank by default — raw citation counts flatter the merely-popular over the
  // judgment that actually settled the point
  const [sort, setSort] = useState("pagerank");
  const [slice, setSlice] = useState<string | null>(null);
  // Paginated accumulation: previews load for EVERY citer, a page at a time, as the
  // tray scrolls — a heavily-cited authority no longer runs out of previews partway.
  const PAGE = 40;
  const [groups, setGroups] = useState<any[]>([]);
  const [meta, setMeta] = useState<any | null>(null);
  const [nextOffset, setNextOffset] = useState(0);
  const [loading, setLoading] = useState(true);
  const [failed, setFailed] = useState(false);
  const sentinel = useRef<HTMLDivElement | null>(null);
  const norm = (g: any) => ({ ...g, snippets: Array.isArray(g.snippets) ? g.snippets : [] });

  // A selected facet is applied at the SERVER (jurisdiction + kind), so picking
  // "FR administrative decisions" pages through all of them — sieving the loaded page
  // instead would show a handful under a chip promising hundreds.
  const [sliceJur, sliceKind] = (slice || "|").split("|");
  useEffect(() => {
    let live = true;
    setGroups([]); setMeta(null); setNextOffset(0); setFailed(false); setLoading(true);
    api.mentions(target, anchor, sort, 0, PAGE, exact, sliceJur || null, sliceKind || null).then((d) => {
      if (!live) return;
      setGroups((Array.isArray(d.groups) ? d.groups : []).filter((g: any) => g && typeof g === "object").map(norm));
      setMeta(d); setNextOffset(PAGE); setLoading(false);
    }).catch(() => { if (live) { setFailed(true); setLoading(false); } });
    return () => { live = false; };
  }, [target, anchor, sort, slice]);

  const loadMore = useCallback(() => {
    if (loading || !meta || !meta.has_more) return;
    setLoading(true);
    api.mentions(target, anchor, sort, nextOffset, PAGE, exact, sliceJur || null, sliceKind || null).then((d) => {
      setGroups((prev) => [...prev, ...(Array.isArray(d.groups) ? d.groups : []).filter((g: any) => g && typeof g === "object").map(norm)]);
      setMeta((m: any) => (m ? { ...m, has_more: d.has_more } : m));
      setNextOffset((o) => o + PAGE); setLoading(false);
    }).catch(() => setLoading(false));
  }, [loading, meta, nextOffset, sort, target, anchor, slice]);

  useEffect(() => {
    const el = sentinel.current;
    if (!el || !meta || !meta.has_more) return;
    const io = new IntersectionObserver((es) => { if (es[0]?.isIntersecting) loadMore(); }, { rootMargin: "300px" });
    io.observe(el);
    return () => io.disconnect();
  }, [loadMore, meta]);

  if (failed) return <div className="error-box">Could not load mentions. <button className="mini" onClick={() => setSort((s) => s)}>retry</button></div>;
  if (!meta) return <p className="muted loading-pulse">Loading mentions…</p>;
  const data = meta;
  const sorts: Record<string, string> = data.sorts && typeof data.sorts === "object" ? data.sorts : {};
  const sorter = Object.keys(sorts).length > 0 && (
    <div className="tray-sort">
      <select value={sort} onChange={(e) => setSort(e.target.value)} title="order these citing documents by…">
        {Object.entries(sorts).map(([v, l]) => <option key={v} value={v}>{l}</option>)}
      </select>
    </div>
  );
  // Cross-section filter tokens — jurisdiction × kind ("UK cases 7 | EU legislation 3"),
  // same as the cited-by panel. No relationship-type chips here: the mentions box goes
  // straight to the categories.
  const KIND_LABEL: Record<string, string> = {
    cases: "cases", legislation: "legislation", guidance: "guidance & reports",
    preparatory: "preparatory documents", explanatory: "explanatory notes",
    // A question put to the Court is not a decision of it. Kept apart from "cases" (and
    // out of the "other" bucket that swallowed it before): a pending reference is what
    // is about to change, not what has been settled.
    preliminary_references: "preliminary references (pending)",
    pending_cases: "other pending proceedings",
    administrative: "admin decisions", other: "other",
  };
  // Counts come from the server's facets, which describe the WHOLE anchor-scoped set —
  // the tray only holds one page, so counting its own rows summarised 40 documents while
  // the header said 912, and anything below the first page read as absent. (An older
  // server without the crossed facet falls back to the loaded rows.)
  const crossed: any[] = Array.isArray(data.facets?.jurisdiction_kind)
    ? data.facets.jurisdiction_kind : [];
  const facets = new Map<string, { jur: string; kind: string; n: number }>();
  if (crossed.length) {
    for (const f of crossed) facets.set(`${f.jurisdiction}|${f.kind}`,
      { jur: f.jurisdiction, kind: f.kind, n: f.documents });
  } else {
    for (const g of groups) {
      if (!g.src_jurisdiction || !g.src_kind) continue;
      const key = `${g.src_jurisdiction}|${g.src_kind}`;
      const f = facets.get(key) || { jur: g.src_jurisdiction, kind: g.src_kind, n: 0 };
      f.n++; facets.set(key, f);
    }
  }
  const tokens = [...facets.entries()].sort((a, b) => b[1].n - a[1].n).slice(0, 10);
  // Live CJEU proceedings are lifted out of the run of citing documents and shown
  // FIRST, in their own section: a pending reference is not a document that has said
  // something about this provision, it is a question about to be answered — and read as
  // one row among 900 settled ones (under "other", where doc_type note filed it) it was
  // indistinguishable from a stray notice.
  const isPending = (g: any) =>
    g.src_kind === "preliminary_references" || g.src_kind === "pending_cases";
  const pending: any[] = groups.filter(isPending);
  const shown = groups.filter((g) => !isPending(g));   // the narrowing happens server-side
  const preparatory: any[] = Array.isArray(data.preparatory_groups) ? data.preparatory_groups : [];
  const pendingFacet = (kind: string) =>
    (crossed.find((f: any) => f.kind === kind) || {}).documents
    || pending.filter((g) => g.src_kind === kind).length;

  const mentionGroup = (g: any, i: number, prefix: string) => (
    <div className={`mgroup${isPending(g) ? " mgroup-pending" : ""}`} key={`${prefix}-${i}`}>
      {isPending(g) && (
        <div className="mgroup-flag" title={g.src_kind === "preliminary_references"
          ? "An Article 267 reference lodged with the Court and not yet decided"
          : "A pending action before the Court — it does not ask what this provision means"}>
          {g.src_kind === "preliminary_references" ? "Reference pending" : "Pending"}
          {g.case_number ? ` · ${g.case_number}` : ""}
          {g.pending_proceeding ? ` · ${g.pending_proceeding}` : ""}
          {g.referring_court ? ` · referred by ${g.referring_court}` : ""}
        </div>
      )}
      <div className="mgroup-head">
        <DocLink className="mgroup-title" id={g.src_id}
          title="Open this document in a tray, with its linked passages highlighted (⌘-click for a new tab)"
          onOpen={() => push({ kind: "doc", id: g.src_id, highlightTarget: target, highlightAnchor: anchor,
            occurrenceStart: g.snippets[0]?.start, label: <Oscola c={g.src_oscola} fallback={g.src_id} /> })}>
          <Oscola c={g.src_oscola} fallback={g.src_id} /></DocLink>
        {g.count > 1 && <span className="tag" title={`${g.count} separate mentions in this document`}>↔ {g.count}</span>}
        <DocLink className="mini" title="Open the full document in the main view (⌘-click for a new tab)"
          id={g.src_id} onOpen={() => open(g.src_id)}>open ↗</DocLink>
      </div>
      <div className="mgroup-meta muted">
        <FlagIcon jurisdiction={g.src_jurisdiction} />{" "}
        {[g.src_court_label || g.src_court, g.src_jurisdiction].filter(Boolean).join(" · ")}
        {g.src_date ? ` · ${String(g.src_date).slice(0, 4)}` : ""}
        {g.count > 1 ? ` · ${g.count} passages` : ""}
      </div>
      {g.snippets.map((s: any, j: number) => (
        <div className="msnip" key={j} role="button" title="Open at this exact mention"
          onClick={() => push({ kind: "doc", id: g.src_id, highlightTarget: target,
            highlightAnchor: anchor, occurrenceStart: s.start,
            label: <Oscola c={g.src_oscola} fallback={g.src_id} /> })}>
          {s.anchor && g.snippets.length > 1 && <span className="msnip-anchor">{s.anchor}</span>}
          <span className="msnip-text">…<SnipText s={s} />…</span></div>
      ))}
    </div>
  );

  if (!groups.length && !preparatory.length) return <><p className="muted">Nothing mentions this yet.</p></>;
  return (
    <div>
      {sorter}
      {tokens.length > 1 && (
        <div className="active-chips cited-by-facets" style={{ marginBottom: 8 }}>
          {tokens.map(([key, f]) => (
            <button key={key} className={`tag tag-btn${slice === key ? " on" : ""}`}
              title={`Show only ${f.jur} ${KIND_LABEL[f.kind] || f.kind} citing this`}
              onClick={() => setSlice(slice === key ? null : key)}>
              <FlagIcon jurisdiction={f.jur} size={0.9} /> {f.jur} {KIND_LABEL[f.kind] || f.kind} <b>{f.n}</b></button>
          ))}
          {slice && (
            <button className="tag tag-btn tag-clear" onClick={() => setSlice(null)}
              title="Show every citing document again">clear ✕</button>
          )}
        </div>
      )}
      {data.total > groups.length && (
        <p className="muted" style={{ fontSize: 12 }}>
          {data.total} citing document{data.total === 1 ? "" : "s"}
          {slice ? " in this filter" : ""} · showing {groups.length}</p>)}
      {pending.length > 0 && (
        <section className="pending-mentions">
          <h4>Before the Court{" "}
            <span className="tag tag-pending">{pendingFacet("preliminary_references")} reference{pendingFacet("preliminary_references") === 1 ? "" : "s"}</span>
            {pendingFacet("pending_cases") > 0 &&
              <span className="tag">{pendingFacet("pending_cases")} other pending</span>}
          </h4>
          <p className="muted">Not yet decided. A preliminary reference asks the Court what
            this text means; the other proceedings cite it without asking that.</p>
          {pending.map((g, i) => mentionGroup(g, i, "pending"))}
        </section>
      )}
      {shown.map((g, i) => mentionGroup(g, i, "mention"))}
      {/* infinite-scroll sentinel: loads the next page of previews as it nears view */}
      {data.has_more && <div ref={sentinel} style={{ height: 1 }} />}
      {loading && groups.length > 0 && <p className="muted loading-pulse" style={{ fontSize: 12 }}>Loading more…</p>}
      {preparatory.length > 0 && (
        <section className="preparatory-documents" style={{ marginTop: 18 }}>
          <h4>Preparatory documents <span className="tag">{data.preparatory_count}</span></h4>
          <p className="muted">Impact assessments, proposals, communications and other legislative history linked to this item.</p>
          {preparatory.map((g, i) => mentionGroup(g, i, "preparatory"))}
        </section>
      )}
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
              ? <DocLink id={it.resolved_id} onOpen={() => push({ kind: "doc", id: it.resolved_id, label: <Oscola c={it.oscola} fallback={it.resolved_id} /> })}>
                  <Oscola c={it.oscola} fallback={it.raw || it.candidate} /></DocLink>
              : <span><Oscola c={it.oscola} fallback={it.raw || it.candidate} /> <span className="muted">· not held</span></span>}
            {it.pinpoints?.length > 0 && <span className="crow-pins"> {it.pinpoints.join(", ")}</span>}
          </div>
          {it.resolved_id && <DocLink className="mini" id={it.resolved_id} onOpen={() => open(it.resolved_id)}>open ↗</DocLink>}
        </div>
      ))}
    </div>
  );
}

// A read-only reader inside a tray, highlighting the paragraphs where the document cites
// the origin document (the "bit linked from"), scrolled to the first.
function MentionReader({ id, highlightTarget, highlightAnchor, occurrenceStart, open }:
  { id: string; highlightTarget?: string; highlightAnchor?: string; occurrenceStart?: number;
    open: (id: string, a?: string) => void }) {
  const [body, , reloadBody] = useAsync(() => api.documentBody(id), [id]);
  const [occIndex, setOccIndex] = useState(0);
  const peek = usePeek();
  const onCite = (c: any) => peek.push(citePeek(c));
  const segs: any[] = (body?.segments || []).length ? body.segments
    : body?.text ? [{ label: "document", char_start: 0, char_end: body.text.length, kind: "document", level: 0 }] : [];
  const cites: any[] = body?.citations || [];
  const occurrences = highlightTarget ? cites.filter((c: any) => c.resolved_id === highlightTarget)
    .sort((a: any, b: any) => a.char_start - b.char_start) : [];
  // paragraphs where THIS document cites the target. When the reader arrived from a
  // specific pinpoint ("Article 25 GDPR"), the citation of THAT pinpoint is the one
  // to jump to — not merely the first mention of the instrument (which is often an
  // earlier, general "the GDPR" reference). We highlight every target mention but
  // prioritise the pinpoint match for the scroll.
  const wantKey = highlightAnchor ? anchorKey(highlightAnchor) : null;
  const hi = new Set<number>();
  let pinpointSeg: number | null = null;
  if (highlightTarget && body) {
    segs.forEach((s: any, i: number) => {
      const hits = cites.filter((c: any) => c.char_start >= s.char_start
        && c.char_start < s.char_end && c.resolved_id === highlightTarget);
      if (!hits.length) return;
      hi.add(i);
      if (pinpointSeg == null && wantKey
          && hits.some((c: any) => c.pinpoint && anchorKey(c.pinpoint) === wantKey))
        pinpointSeg = i;
    });
  }
  useEffect(() => {
    if (!occurrences.length) return;
    const exact = occurrenceStart == null ? -1
      : occurrences.findIndex((c: any) => c.char_start === occurrenceStart);
    const pinned = wantKey ? occurrences.findIndex((c: any) => c.pinpoint && anchorKey(c.pinpoint) === wantKey) : -1;
    setOccIndex(exact >= 0 ? exact : pinned >= 0 ? pinned : 0);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [body, occurrenceStart, highlightTarget, highlightAnchor]);
  useEffect(() => {
    if (!body) return;
    const occurrence = occurrences[occIndex];
    // An exact citation span wins even in one enormous/unsegmented paragraph.
    // Segment fallback remains for old structured edges without stored offsets.
    const target = pinpointSeg != null ? pinpointSeg : [...hi][0];
    if (occurrence || target != null) {
      const el = occurrence
        ? document.getElementById(`tray-${id}-cite-${occurrence.char_start}`)
        : document.getElementById(`tray-${id}-seg-${target}`);
      if (el) setTimeout(() => { el.scrollIntoView({ behavior: "smooth", block: "center" });
        el.classList.add("seg-flash"); setTimeout(() => el.classList.remove("seg-flash"), 1600); }, 80);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [body, occIndex]);
  if (!body) return <p className="muted loading-pulse">Loading…</p>;
  return (
    <SelectionShorthand docId={id} onLinked={reloadBody}>
      <div className="tray-doc-head">
        <b><Oscola c={body.oscola} fallback={body.title || id} /></b>
        <DocLink className="mini" id={id} onOpen={() => open(id)}>open full ↗</DocLink>
      </div>
      {!body.text && <p className="muted">No text (metadata only).</p>}
      <div className="reader">
        {segs.map((s: any, i: number) => {
          const sb = segBody(body.text, s, cites, onCite, undefined, undefined, `tray-${id}`);
          return (
            <div className={`seg lvl${Math.min(s.level, 2)} kind-${s.kind}${hi.has(i) ? " seg-hi" : ""}${i === pinpointSeg ? " seg-pinpoint" : ""}`} key={i} id={`tray-${id}-seg-${i}`}>
              {sb.showLabel && <span className="seg-label">{s.label}</span>}
              <span className="seg-body">{sb.body}</span>
            </div>
          );
        })}
      </div>
      {occurrences.length > 1 && <div className="tray-occ-nav">
        <button className="mini" disabled={occIndex <= 0} onClick={() => setOccIndex((n) => Math.max(0, n - 1))}>← previous</button>
        <span className="muted">mention {occIndex + 1} of {occurrences.length}</span>
        <button className="mini" disabled={occIndex >= occurrences.length - 1}
          onClick={() => setOccIndex((n) => Math.min(occurrences.length - 1, n + 1))}>next →</button>
      </div>}
    </SelectionShorthand>
  );
}

// The date to SHOW for a document. A judgment date where the source gave one; otherwise
// the year its own identifier carries (ewca/civ/1975/5 is a 1975 judgment) — which is
// how 68,158 held common-law judgments have a date at all. An inferred date is never
// dressed up as a judgment date: it shows the year alone, with a marker.
function docDate(d: any): { text: string; inferred: boolean } | null {
  if (!d) return null;
  if (d.decision_date) return { text: String(d.decision_date).slice(0, 10), inferred: false };
  if (d.effective_date) return { text: String(d.effective_date).slice(0, 4), inferred: true };
  return null;
}

function DocDate({ d, prefix = " · " }: { d: any; prefix?: string }) {
  const dd = docDate(d);
  if (!dd) return null;
  return <span title={dd.inferred ? "no judgment date on the record — the year comes from the citation" : undefined}>
    {prefix}{dd.text}{dd.inferred && <span className="muted"> (from the citation)</span>}</span>;
}


// "Data Protection Act 1998 (as at 2018-05-24)" + "s. 7" → "Data Protection Act 1998 s. 7";
// the GDPR's forty-word title → "Regulation (EU) 2016/679".
//
// A full instrument title inside a running sentence is not a citation, it is an
// obstruction, and the point-in-time suffix — which is how the corpus keys a dated version
// and belongs on that version's own page — simply breaks the prose here. This is the same
// progression the static export already applies (``_short_instrument_title``), kept in step
// deliberately: the reader and the published page should name a provision identically. The
// full title stays in the link's tooltip.
const EU_INSTRUMENT_RE =
  /\b(Council|Commission|European Parliament and Council)?\s*(Framework|Implementing|Delegated)?\s*(Directive|Regulation|Decision|Recommendation)\s+(\((?:EU|EC|EEC|ECSC|Euratom)(?:\s*,\s*Euratom)?\)\s*)?(?:No\s*)?(\d{1,4}\/\d{1,4}(?:\/(?:EU|EC|EEC|JHA|CFSP|Euratom|ECSC))?)\b/i;
// Common-law drafting names its instruments the other way round, and the name IS short.
const ACT_RE = /^(.{0,90}?\b(?:Act|Regulations|Order|Rules|Measure|Ordinance)\s+\d{4})\b/;

export function shortInstrumentTitle(title: string, fallback = ""): string {
  const text = String(title || "").replace(/\s+/g, " ").trim();
  if (!text) return fallback;
  const eu = EU_INSTRUMENT_RE.exec(text);
  if (eu) return [eu[1], eu[2], eu[3], (eu[4] || "").trim(), eu[5]]
    .filter((p) => p && p.trim()).map((p) => p.trim()).join(" ");
  const act = ACT_RE.exec(text);
  if (act) return act[1];
  let t = text;
  if (t.length > 64) {
    const clause = t.split(/,| of the | of \d| on the | establishing /)[0].trim();
    if (clause.length > 8 && clause.length < t.length) t = clause;
  }
  return t.length <= 90 ? t : t.slice(0, 87).trimEnd() + "…";
}

function provisionLabel(title: string, provAnchor: string, fallbackId = ""): string {
  const t = shortInstrumentTitle(title, fallbackId);
  return provAnchor ? `${t} ${provAnchor}` : t;
}

// The inline "Mentioned by A, B, C and n more. See all mentions." line under a paragraph.
//
// Direct citers lead. Documents that cited a MAPPED provision of another instrument
// (an earlier iteration, or a parallel provision in a companion instrument — the AI Act
// against the NLF regulations) follow on their own line, naming the instrument they
// actually cited: they belong beside the article, but calling them mentions of it would
// be untrue.
function MentionedBy({ list, target, anchor }: { list: any[]; target: string; anchor: string }) {
  const { push } = useTray();
  const { push: peek } = usePeek();
  const direct = list.filter((m: any) => !m.inherited);
  const inherited = list.filter((m: any) => m.inherited);
  const top = direct.slice(0, 3);
  const more = direct.length - top.length;
  // Gathered by the provision actually cited, biggest first, so the instrument a reader
  // is most likely to be looking for leads rather than being hidden behind "and others".
  const inheritedGroups = useMemo(() => {
    const by = new Map<string, any>();
    for (const m of inherited) {
      const key = `${m.mapping_type}|${m.from_id}|${m.from_anchor}`;
      let g = by.get(key);
      if (!g) {
        by.set(key, g = { key, from_id: m.from_id, from_anchor: m.from_anchor,
                          from_title: m.from_title, mapping_type: m.mapping_type,
                          label: provisionLabel(m.from_title || m.from_id, m.from_anchor, m.from_id),
                          items: [] });
      }
      g.items.push(m);
    }
    return [...by.values()].sort((a, b) => b.items.length - a.items.length);
  }, [inherited]);
  const openDoc = (m: any) => push({ kind: "doc", id: m.src_id, highlightTarget: target,
    // carry the provision anchor so the reader scrolls to the mention of THIS
    // section, not merely the first mention of the instrument (a general "the
    // Privacy Act 1988" reference earlier in the citing document).
    highlightAnchor: anchor, label: <Oscola c={m.src_oscola} fallback={m.src_id} /> });
  return (
    <div className="mentioned-by">
      {direct.length > 0 && <>
        <span className="mb-label">Mentioned by </span>
        {top.map((m, i) => (
          <Fragment key={i}>{i > 0 && ", "}
            <a title="Open this citing document, at the passage that cites this provision"
              onClick={() => openDoc(m)}>
              <Oscola c={m.src_oscola} fallback={m.src_id} /></a>
          </Fragment>
        ))}
        {more > 0 && <span> and {more} more</span>}.{" "}
        <a className="mb-all" onClick={() => push({ kind: "mentions", target, anchor, label: <>Mentions of {anchor}</> })}>See all mentions</a>
      </>}
      {/* ONE LINE PER SOURCE PROVISION. A provision may correspond to several others at
          once — assimilated GDPR Article 15 maps to the EU Regulation's own Article 15
          (parallel), to Directive 95/46 Article 12, AND to DPA 1998 s. 7 — and all three
          used to collapse into "via <the first one> and others". So the DPA link, which is
          the one a UK reader wants, was invisible unless it happened to sort first; the
          "and others" that hid it was not even a link; and the whole line took its label
          from the first mapping, which called citers of the parallel EU provision an
          "earlier iteration". Grouping fixes all three: each line names its own provision,
          in bold, linked to it, and carries the label its OWN mapping type asserts. */}
      {inheritedGroups.map((g) => (
        <div className="mb-inherited" key={g.key}>
          <span className="mb-label">
            {mappingKind(g.mapping_type).row === "parallel provision"
              ? "Parallel provision cited by " : "Earlier iteration cited by "}</span>
          {g.items.slice(0, 3).map((m: any, i: number) => (
            <Fragment key={i}>{i > 0 && ", "}
              <a title={`This document cites ${g.label}, mapped to this provision`}
                onClick={() => openDoc(m)}>
                <Oscola c={m.src_oscola} fallback={m.src_id} /></a>
            </Fragment>
          ))}
          {g.items.length > 3 && <span> and <a title={`See every document citing ${g.label}`}
            onClick={() => push({ kind: "mentions", target, anchor, label: <>Mentions of {anchor}</> })}>
            {g.items.length - 3} more</a></span>}
          <span className="muted"> — via </span>
          <a className="mb-via" title={`Open ${g.from_title || g.from_id} ${g.from_anchor}`}
            onClick={() => peek({ kind: "doc", id: g.from_id, anchor: g.from_anchor })}>
            {g.label}</a>.
        </div>
      ))}
    </div>
  );
}

// A neat speech bubble (rounded rect + tail) — a pill so any count, single- or
// many-digit, sits comfortably inside.
function SpeechBubble({ n }: { n: number }) {
  return (
    <span className="mbubble" aria-hidden="true">
      <svg viewBox="0 0 20 16" className="mbubble-svg" width="1em" height="1em">
        <path d="M3 1h14a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H8l-4 3v-3H3a2 2 0 0 1-2-2V3a2 2 0 0 1 2-2z"
          fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round" />
      </svg>
      <span className="mbubble-n">{n.toLocaleString()}</span>
    </span>
  );
}

// A compact prev / "11–20 of N" / next pager, so a long list shows one short page at a
// time instead of running the whole admin page to thousands of rows. Renders nothing when
// everything already fits on one page.
// Cap a long list at a readable height with a "show all" toggle. The panels under the
// operative text are reference material, not the reading surface: a document citing 300
// authorities pushed everything below it off the page, and the reader had to scroll past
// all of it to reach the next panel. The count is always visible, so nothing is hidden
// silently — only deferred.
function useShowMore<T>(items: T[], initial = 12): [T[], any] {
  const [all, setAll] = useState(false);
  const hidden = Math.max(0, items.length - initial);
  const control = hidden === 0 ? null : (
    <a className="mini-link show-more" onClick={() => setAll((v) => !v)}
      title={all ? "Collapse this list again" : `Show the remaining ${hidden}`}>
      {all ? `▴ Show fewer` : `▾ Show all ${items.length}`}</a>
  );
  return [all ? items : items.slice(0, initial), control];
}

function Pager({ page, pageSize, total, onPage, noun = "items" }:
  { page: number; pageSize: number; total: number; onPage: (p: number) => void; noun?: string }) {
  const pages = Math.ceil(total / pageSize);
  if (pages <= 1) return null;
  const from = page * pageSize + 1;
  const to = Math.min(total, page * pageSize + pageSize);
  return (
    <div className="pager">
      <button className="mini" disabled={page <= 0} onClick={() => onPage(page - 1)}>← prev</button>
      <span className="muted">{from.toLocaleString()}–{to.toLocaleString()} of {total.toLocaleString()} {noun}</span>
      <button className="mini" disabled={page >= pages - 1} onClick={() => onPage(page + 1)}>next →</button>
    </div>
  );
}

// The parenthetical pinpoint beyond a provision's base number:
// "section 7(1)(a)" → "(1)(a)", "Article 47(2)" → "(2)"; null for a bare provision.
function subPart(anchor: string): string | null {
  const m = /\d+[a-z]?((?:\s*\([^()]+\))+)\s*$/i.exec((anchor || "").trim());
  return m ? m[1].replace(/\s+/g, "") : null;
}

type SubPara = { anchor: string; part: string; count: number };

// The cited sub-provisions of one section, keyed by their parenthetical part ("(1)",
// "(2)(a)"), each with the citer count. Shared by the per-line placement (a badge at the
// end of its own provision line) and the segment-level fallback (a badge row at the foot,
// for sub-parts whose line the drafting-hierarchy pass couldn't pinpoint).
function subPartMap(byAnchorRaw: Record<string, any[]>, sectionLabel: string): Map<string, SubPara> {
  const sk = anchorKey(sectionLabel);
  const byPart = new Map<string, { anchor: string; part: string; srcs: Set<string> }>();
  if (!sk) return new Map();
  for (const [a, list] of Object.entries(byAnchorRaw || {})) {
    if (anchorKey(a) !== sk) continue;      // only this provision's family
    const part = subPart(a);
    if (!part) continue;                    // the bare provision → the MentionedBy line
    const cur = byPart.get(part) || { anchor: a, part, srcs: new Set<string>() };
    for (const m of (list || [])) cur.srcs.add(m.src_id);
    byPart.set(part, cur);
  }
  const out = new Map<string, SubPara>();
  for (const [part, v] of byPart) if (v.srcs.size > 0) out.set(part, { anchor: v.anchor, part, count: v.srcs.size });
  return out;
}

// One clickable speech-bubble badge for a cited sub-provision; opens the mentions tray
// filtered to exactly that sub-paragraph.
function SubBadge({ sp, sectionLabel, target }: { sp: SubPara; sectionLabel: string; target: string }) {
  const { push } = useTray();
  return (
    <button className="subpara-badge"
      title={`${sp.count.toLocaleString()} document${sp.count === 1 ? "" : "s"} cite ${sp.part} specifically — see them`}
      onClick={() => push({ kind: "mentions", target, anchor: sp.anchor, exact: true,
        label: <>Mentions of {(sectionLabel.match(/^\s*(?:art(?:icle)?|s(?:ection)?|reg(?:ulation)?)\.?\s*\d+[a-z]?/i)?.[0] || sectionLabel).trim()}{sp.part}</> })}>
      <span className="sp-part">{sp.part}</span>
      <SpeechBubble n={sp.count} />
    </button>
  );
}

const REL_TYPES = [
  "analyses", "criticises", "summarises", "annotates", "follows", "distinguishes",
  "overrules", "applies", "considers", "interprets", "mentions",
];
const DOC_TYPES = ["judgment", "decision", "opinion", "legislation", "preparatory", "guidance", "commentary", "article", "note", "annotation"];
// treatments a citation edge can carry — for the inline reclassify control
const TREATMENTS = ["mentions", "follows", "distinguishes", "overrules", "applies", "considers", "interprets", "implements"];
const DOC_TYPE_LABEL: Record<string, string> = {
  preparatory: "EU preparatory / policy document",
};
const RELATION_LABEL: Record<string, string> = {
  adopted_as: "adopted as",
  related_to: "accompanies / relates to",
};
const docTypeLabel = (t?: string | null) => t ? (DOC_TYPE_LABEL[t] || t.replace(/_/g, " ")) : "";
const relationLabel = (t: string) => RELATION_LABEL[t] || t.replace(/_/g, " ");

// Start a background job and poll it to completion, reporting progress as it goes.
async function runJob(kind: "harvest-all", body: Record<string, unknown>,
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
// "Best match" ranks by how well the title matches what was typed; with no query it is
// the same as Newest (there is nothing to be relevant to).
const SORTS: [string, string][] = [["relevance", "Best match"], ["date", "Newest"], ["date_asc", "Oldest"],
  ["title", "Title A–Z"], ["cited", "Most cited"],
  ["authority", "Most influential"], ["authority_recent", "Most influential (recent)"]];
const GROUPS: [string, string][] = [["none", "No grouping"], ["source", "Source"], ["doc_type", "Type"], ["court", "Court"], ["decade", "Decade"]];

const activeFilters = (f: Filters): Record<string, string> => {
  const o: Record<string, string> = {};
  Object.entries(f).forEach(([k, v]) => v && !k.startsWith("_") && (o[k] = String(v)));
  return o;
};

// The metadata search's rail — the same component the free-text surface renders,
// given this search's data. Its facets are single-select and a click re-queries,
// because the filtering and the counts are both computed server-side; free text is
// multi-select and narrows in the browser. That difference is the `active`/`onToggle`
// contract, not a second implementation.
function FacetSidebar({ facets, filters, patch }:
  { facets: any; filters: Filters; patch: (p: Partial<Filters>) => void }) {
  if (!facets) return null;
  const active: Record<string, string[]> = {};
  (["source", "doc_type", "court"] as const).forEach((d) => {
    const v = (filters as any)[d];
    active[d] = v ? [v] : [];
  });
  return (
    <FacetRail
      dims={dimsFromCorpus(facets)}
      active={active}
      onToggle={(dim, value) =>
        patch({ [dim]: (filters as any)[dim] === value ? undefined : value } as any)}
      years={facets.year || {}}
      yearFrom={filters.year_from}
      yearTo={filters.year_to}
      onYearRange={(a, b) => patch({ year_from: a, year_to: b })}
      onYearClear={() => patch({ year_from: undefined, year_to: undefined })}
    />
  );
}

export function SearchView({ open, initialFilter }: { open: (id: string, a?: string) => void; initialFilter?: Record<string, string> }) {
  const [mode, setMode] = useState<"simple" | "advanced">(
    initialFilter && Object.keys(initialFilter).length ? "advanced" : "simple");
  const [filters, setFilters] = useState<Filters>(initialFilter || {});
  const [sort, setSort] = useState("relevance");
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
                    <select value={sort} onChange={(e) => { setSort(e.target.value); setPage(0); }}>{SORTS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select>
                    <InfoDot text={INFLUENCE_EXPLAINER} /></label>
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
  const [openList, setOpenList] = useState(false);
  const ac = useAutosuggest<any>(
    q,
    (limit) => api.searchCorpus({ query: q.trim(), limit: String(limit), facets: "false" })
      .then((r: any) => r.items || []),
    { batch: 8, delay: 110, enabled: !semantic });
  useEffect(() => { if (ac.items.length) setOpenList(true); }, [ac.items]);
  const pick = (o: any) => { if (o) { open(o.stable_id); setOpenList(false); } };
  const search = () => { setOpenList(false); onSearch(); };   // running a search dismisses the dropdown
  return (
    <div>
      <div className="row ac" style={{ position: "relative" }}>
        <input autoFocus value={q} placeholder="Search cases, statutes… (any words, any order)"
          onChange={(e) => { setQuery(e.target.value); }}
          onFocus={() => ac.items.length && setOpenList(true)}
          onBlur={() => setTimeout(() => setOpenList(false), 150)}
          onKeyDown={(e) => {
            if (e.key === "Escape") { setOpenList(false); return; }
            if (ac.onNavKey(e)) return;
            if (e.key === "Enter") {
              if (!openList) { search(); return; }
              // the "show more" row is a list member, so Enter on it fetches rather
              // than running the search and throwing the dropdown away
              if (ac.onMoreRow) { e.preventDefault(); ac.showMore(); }
              else if (ac.highlighted) pick(ac.highlighted);
              else search();
            }
          }} />
        <button className="primary" style={{ flex: "0 0 auto" }} onClick={search}>Search</button>
        {openList && ac.items.length > 0 && (
          <div className="ac-list">
            {ac.items.map((o, i) => (
              <div key={o.stable_id} className={`ac-opt${i === ac.hi ? " hi" : ""}`}
                onMouseEnter={() => ac.setHi(i)} onMouseDown={(e) => { e.preventDefault(); pick(o); }}>
                <b><Oscola c={o.oscola} fallback={o.title || o.stable_id} /></b>
                <span className="muted"> · {o.source}/{docTypeLabel(o.doc_type)}{o.court ? " · " + o.court : ""}</span>
              </div>
            ))}
            {ac.hasMore && <AcMore onClick={ac.showMore} loading={ac.loading}
              hi={ac.onMoreRow} onHover={() => ac.setHi(ac.items.length)} />}
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
  // The error is READ, not discarded. Previously this destructured only the data, so a
  // failed /facet-values — a 401 during session bootstrap, a transient blip — rendered
  // exactly like "there are no sources", permanently: the deps array is empty, so it
  // never retried and nothing on screen said anything was wrong.
  const [fv, fvErr, reloadFacets, fvLoading] = useAsync(() => api.facetValues(), []);
  const set = (k: keyof Filters, v: string) => setFilters((f) => ({ ...f, [k]: v || undefined }));
  const clear = (k: keyof Filters) => setFilters((f) => ({ ...f, [k]: undefined }));

  if (fvErr) {
    return (
      <div className="adv-form">
        <div className="adv-fail">
          <b>Couldn’t load the filter options.</b>
          <div className="muted">{fvErr}</div>
          <button className="mini" onClick={reloadFacets}>try again</button>
        </div>
        <div className="adv-row">
          <label>Title / id contains</label>
          <input value={filters.query || ""} placeholder="e.g. unfair dismissal"
            onChange={(e) => set("query", e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && onSearch()} />
        </div>
        <button className="primary" onClick={onSearch}>Search</button>
      </div>
    );
  }

  return (
    <div className="adv-form">
      <div className="adv-row">
        <label>Title / id contains <span className="muted">(words in any order; also
          searches “also cited as”)</span></label>
        <input value={filters.query || ""} placeholder="e.g. unfair dismissal"
          onChange={(e) => set("query", e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && onSearch()} />
      </div>

      <div className="adv-grid">
        <PickField label="Source" hint="where it came from" value={filters.source}
          rows={fv?.sources} loading={fvLoading}
          onPick={(v) => set("source", v)} onClear={() => clear("source")} />
        <PickField label="Type" hint="judgment, legislation, guidance…" value={filters.doc_type}
          rows={fv?.doc_types} loading={fvLoading}
          onPick={(v) => set("doc_type", v)} onClear={() => clear("doc_type")} />
        <PickField label="Court / body" hint="788 to choose from" value={filters.court}
          rows={fv?.courts} loading={fvLoading} searchable
          onPick={(v) => set("court", v)} onClear={() => clear("court")} />
        <PickField label="Tag" hint="your collections" value={filters.tag}
          rows={fv?.tags} loading={fvLoading}
          onPick={(v) => set("tag", v)} onClear={() => clear("tag")} />
        <div className="adv-years">
          <label>Years</label>
          <div className="row" style={{ gap: 6 }}>
            <input type="number" min={1200} max={2100} value={filters.year_from || ""}
              placeholder="from" onChange={(e) => set("year_from", e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && onSearch()} />
            <span className="muted">–</span>
            <input type="number" min={1200} max={2100} value={filters.year_to || ""}
              placeholder="to" onChange={(e) => set("year_to", e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && onSearch()} />
          </div>
        </div>
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

// One filter field. A <select> was the wrong control here: 788 courts is not a list you
// scroll, the counts are what tell you whether a value is worth picking, and an empty
// list looked identical to a broken one. This shows the count beside every value, says
// so when there are none, and filters as you type once the list is long.
function PickField({ label, hint, value, rows, loading, searchable, onPick, onClear }:
  { label: string; hint?: string; value?: string; rows?: any[]; loading?: boolean;
    searchable?: boolean; onPick: (v: string) => void; onClear: () => void }) {
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState("");
  const all: any[] = rows || [];
  const shown = q ? all.filter((r) => r.key.toLowerCase().includes(q.toLowerCase())) : all;
  const total = all.reduce((a, r) => a + (r.n || 0), 0);
  return (
    <div className="pick">
      <label>{label} {hint && <span className="muted">— {hint}</span>}</label>
      {value ? (
        <div className="pick-chosen">
          <span className="tag">{value}</span>
          <button className="linkish" onClick={onClear}>change</button>
        </div>
      ) : (
        <button className="pick-open" onClick={() => setOpen(!open)}>
          {loading ? "loading…"
            : all.length ? `any — ${all.length} to choose from`
            : "none available"}
          <span className="muted">{open ? " ▾" : " ▸"}</span>
        </button>
      )}
      {open && !value && (
        <div className="pick-list">
          {searchable && all.length > 12 && (
            <input className="pick-search" autoFocus value={q} placeholder="filter…"
              onChange={(e) => setQ(e.target.value)} />
          )}
          {shown.length === 0 && (
            <p className="muted pick-empty">
              {all.length === 0
                ? "The corpus reports no values for this filter."
                : `Nothing matches “${q}”.`}
            </p>
          )}
          {shown.slice(0, 60).map((r) => (
            <button key={r.key} className="pick-opt"
              onClick={() => { onPick(r.key); setOpen(false); setQ(""); }}>
              <span className="pick-bar" style={{
                width: `${Math.max(2, 100 * (r.n || 0) / (all[0]?.n || 1))}%` }} />
              <span className="pick-opt-label">{r.label || r.key}</span>
              <span className="pick-opt-n">{(r.n || 0).toLocaleString()}</span>
            </button>
          ))}
          {shown.length > 60 && (
            <p className="muted pick-empty">
              {shown.length - 60} more — type to narrow
            </p>
          )}
          {all.length > 0 && (
            <p className="muted pick-empty">{total.toLocaleString()} documents in total</p>
          )}
        </div>
      )}
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

// "top N%" authority chip — only shown when the document is in the upper reaches of
// the citation network (a low percentile is noise, not information).
function AuthorityBadge({ pct }: { pct?: number | null }) {
  if (pct == null || pct < 80) return null;
  const top = Math.max(1, Math.round(100 - pct));
  return <span className="auth-badge" title={`More influential than ${pct.toFixed(0)}% of cited documents`}> · top {top}%</span>;
}

// One results list, optionally grouped, each row an OSCOLA citation + metadata.
function ResultsList({ items, group, open }: { items: any[]; group: string; open: (id: string, a?: string) => void }) {
  if (!items.length) return <p className="muted" style={{ marginTop: 8 }}>No matches. Loosen a filter, or try the semantic toggle for concepts.</p>;
  const keyFor = (d: any): string => {
    if (group === "source") return d.source || "—";
    if (group === "doc_type") return d.doc_type || "—";
    if (group === "court") return d.court || "—";
    if (group === "decade") { const y = (d.decision_date || d.effective_date || "").slice(0, 4); return y ? y.slice(0, 3) + "0s" : "undated"; }
    return "";
  };
  const row = (d: any) => (
    <div className="result-row" key={d.stable_id}>
      <DocLink className="result-cite" id={d.stable_id} onOpen={() => open(d.stable_id)}><Oscola c={d.oscola} fallback={d.title || d.stable_id} /></DocLink>
      <div className="result-meta muted">
        <span className="tag">{docTypeLabel(d.doc_type)}</span>
        {d.court && <span> · {d.court}</span>}
        <DocDate d={d} />
        {d.cited_by > 0 && <span> · cited by {d.cited_by.toLocaleString()}</span>}
        <AuthorityBadge pct={d.authority_percentile} />
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

// "Why is this here" chips: which retrieval signals placed the hit — exact words
// (keyword), meaning (semantic), and the citation network (authority). Teaches the
// system's behaviour instead of presenting an opaque score.
function WhyChips({ s }: { s: Hit["signals"] }) {
  if (!s) return null;
  const chips: { t: string; title: string }[] = [];
  if (s.lexical_rank != null) chips.push({ t: `keyword #${s.lexical_rank}`, title: "matched the exact words (full-text rank)" });
  if (s.semantic_rank != null) chips.push({ t: `semantic #${s.semantic_rank}`, title: "matched by meaning (vector rank)" });
  if (s.authority_percentile != null && s.authority_percentile >= 80)
    chips.push({ t: `influence top ${Math.max(1, Math.round(100 - s.authority_percentile))}%`,
      title: "highly cited in the citation network (PageRank)" });
  if (!chips.length) return null;
  return <span className="why-chips">{chips.map((c, i) =>
    <span key={i} className="why-chip" title={c.title}>{c.t}</span>)}</span>;
}

// One semantic hit with a KWIC-style expander: "show context" pulls the enclosing
// segment ± neighbours (exact char spans, via /provision) and the heading breadcrumb,
// so you judge the passage without leaving the results page.
function SemanticHit({ h, open }: { h: Hit; open: (id: string, a?: string) => void }) {
  const [ctx, setCtx] = useState<any | null>(null);
  const [busy, setBusy] = useState(false);
  const expand = async () => {
    if (ctx) { setCtx(null); return; }
    setBusy(true);
    try { setCtx(await api.provision(h.doc_id, { start: h.char_start ?? 0, end: h.char_end ?? undefined, n: 2 })); }
    catch { setCtx({ error: true }); }
    setBusy(false);
  };
  const focusLabel = ctx?.segments?.find((s: any) => s.focus)?.label;
  return (
    <div className="hit">
      <div>
        <DocLink className="hit-title" id={h.doc_id} anchor={h.structural_unit || undefined}
          onOpen={() => open(h.doc_id, h.structural_unit || undefined)}>
          <Oscola c={h.oscola} fallback={h.title || h.ecli || h.doc_id} /></DocLink>{" "}
        <span className="muted">
          {h.court ? h.court : h.source}
          <DocDate d={h} />
          {h.structural_unit ? ` · ${h.structural_unit}` : ""}
        </span>
        <WhyChips s={h.signals} />
      </div>
      {ctx?.path?.length > 0 && (
        <div className="hit-crumb muted">{ctx.path.map((p: string, i: number) => (
          <Fragment key={i}>{i > 0 && " › "}<DocLink id={h.doc_id} anchor={p} onOpen={() => open(h.doc_id, p)}>{p}</DocLink></Fragment>
        ))}{focusLabel ? <> › <b>{focusLabel}</b></> : null}</div>
      )}
      {!ctx && <div className="snippet">{h.chunk_text.slice(0, 320)}{h.chunk_text.length > 320 ? "…" : ""}</div>}
      {ctx && !ctx.error && (
        <div className="hit-context">
          {(ctx.segments || []).map((s: any, i: number) => (
            <div key={i} className={`ctx-seg${s.focus ? " focus" : ""}`}>
              {s.label && <span className="seg-label">{s.label}</span>}
              <span>{s.text.length > 900 && !s.focus ? s.text.slice(0, 900) + "…" : s.text}</span>
            </div>
          ))}
        </div>
      )}
      <div className="hit-actions">
        <a className="mini-link" onClick={expand}>{busy ? "…" : ctx ? "hide context" : "⌄ show context"}</a>
        <DocLink className="mini-link" id={h.doc_id} anchor={h.structural_unit || undefined}
          onOpen={() => open(h.doc_id, h.structural_unit || undefined)}>open at this passage ↗</DocLink>
        {h.neighbours.length > 0 && (
          <span className="nbr">graph: {h.neighbours.slice(0, 3).map((n, j) =>
            <span key={j}>{n.direction === "out" ? "→" : "←"} {relationLabel(n.relationship_type)}{" "}
              <DocLink id={n.id} onOpen={() => open(n.id)} title={n.title || n.id}>{n.title ? (n.title.length > 40 ? n.title.slice(0, 40) + "…" : n.title) : n.id}</DocLink>; </span>)}</span>
        )}
      </div>
    </div>
  );
}

// Semantic (full-text) hits — hybrid keyword + vector + authority, fused (RRF).
function SemanticResults({ hits, open }: { hits: Hit[]; open: (id: string, a?: string) => void }) {
  return (
    <div className="panel">
      <p className="muted">{hits.length} result{hits.length === 1 ? "" : "s"} · keyword + semantic + influence, fused (RRF), with graph neighbours</p>
      {hits.length === 0 && <p className="muted">No matches. Try fewer filters, or embed first (Dashboard → Embed pending).</p>}
      {hits.map((h, i) => <SemanticHit key={i} h={h} open={open} />)}
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
function formattedRun(text: string, start: number, end: number, formatting: any[] = [], key = "fmt") {
  const marks = formatting.filter((m) => m.char_start < end && m.char_end > start);
  if (!marks.length) return text.slice(start, end);
  const cuts = Array.from(new Set([start, end, ...marks.flatMap((m) =>
    [Math.max(start, m.char_start), Math.min(end, m.char_end)])])).sort((a, b) => a - b);
  return cuts.slice(0, -1).map((a, i) => {
    const b = cuts[i + 1];
    const active = new Set(marks.filter((m) => m.char_start <= a && m.char_end >= b).map((m) => m.kind));
    let node: any = text.slice(a, b);
    if (active.has("underline")) node = <u>{node}</u>;
    if (active.has("italic")) node = <em>{node}</em>;
    if (active.has("bold")) node = <strong>{node}</strong>;
    return <Fragment key={`${key}-${a}`}>{node}</Fragment>;
  });
}

function renderRun(text: string, key: string, paraSet?: Set<string>, onPara?: (n: string) => void,
                   start = 0, end = text.length, formatting: any[] = []) {
  const run = text.slice(start, end);
  if (!onPara || !paraSet || paraSet.size === 0) return formattedRun(text, start, end, formatting, key);
  const out: any[] = [];
  // [N] (optionally a range/list) immediately followed by above|below
  const re = /\[(\d{1,3})\](?:\s*[-–]\s*\[\d{1,3}\]|\s*,?\s*(?:and|to)\s*\[\d{1,3}\])?\s+(above|below)\b/gi;
  let last = 0, m: RegExpExecArray | null, k = 0;
  while ((m = re.exec(run))) {
    const n = m[1];
    if (!paraSet.has(n)) continue;
    if (m.index > last) out.push(formattedRun(text, start + last, start + m.index, formatting, `${key}-t${k}`));
    out.push(<a key={`${key}-p${k++}`} className="pararef" title={`go to paragraph ${n} (this judgment)`}
      onClick={() => onPara(n)}>{formattedRun(text, start + m.index, start + re.lastIndex, formatting, `${key}-p`)}</a>);
    last = re.lastIndex;
  }
  if (last < run.length) out.push(formattedRun(text, start + last, end, formatting, `${key}-tail`));
  return out.length ? out : formattedRun(text, start, end, formatting, key);
}

// Render a slice of text with its recognised citations wrapped as live links to the
// cited authority (JADE-style inline links) — resolved → peek the authority (+ pinpoint),
// pending → marked as a citation we've parsed but don't yet hold. Paragraph refs jump.
function renderCited(text: string, segStart: number, segEnd: number, cites: any[],
                     onCite: (c: any) => void, paraSet?: Set<string>, onPara?: (n: string) => void,
                     idPrefix?: string, formatting: any[] = []) {
  const within = cites
    .filter((c) => c.char_start >= segStart && c.char_end <= segEnd)
    .sort((a, b) => a.char_start - b.char_start);
  const nodes: any[] = [];
  let cursor = segStart;
  within.forEach((c, k) => {
    if (c.char_start < cursor) return; // skip overlaps
    if (c.char_start > cursor) nodes.push(renderRun(text, `g${k}`, paraSet, onPara,
      cursor, c.char_start, formatting));
    const label = text.slice(c.char_start, c.char_end);
    const state = c.state || (c.resolved_id ? "resolved" : c.candidate_id ? "pending" : "maybe");
    // heuristic "carried-forward" provision (e.g. a bare "section 5" linked to the
    // last-named statute) — flag it as an uncertain guess for the reader.
    const guess = c.method === "carry_forward" || c.extracted_via === "inferred";
    const title = guess ? `inferred: “${label}” taken to mean ${c.pinpoint || ""} of ${c.candidate_id || c.resolved_id} — uncertain, click to check`
      : state === "resolved" ? `${c.entity_kind}${c.pinpoint ? " · " + c.pinpoint : ""} → ${c.resolved_id}`
      : state === "pending" ? `${c.entity_kind}: ${c.candidate_id} — not in the corpus yet (click to fetch)`
      : `${c.entity_kind} reference — not resolvable automatically (click to search)`;
    // resolved links get a rich hover card (CiteHoverLayer) instead of the native tooltip
    // A RESOLVED citation gets the target's real URL, so the reader can ⌘-click a case
    // cited in a judgment and read it in a new tab with the judgment still open — the
    // thing a click handler alone could never offer. Unresolved ones stay handler-only:
    // they trigger a fetch/search, and there is no document to link to yet.
    nodes.push(<a key={k} id={idPrefix ? `${idPrefix}-cite-${c.char_start}` : undefined}
      className={`cite cite-${state}${guess ? " cite-inferred" : ""}`}
      title={state === "resolved" && !guess ? undefined : title}
      href={state === "resolved" && c.resolved_id ? docHref(c.resolved_id, c.pinpoint) : undefined}
      data-doc={state === "resolved" ? c.resolved_id : undefined} data-pin={c.pinpoint || undefined}
      onClick={(e) => { if (opensNewTab(e)) return; e.preventDefault(); onCite(c); }}>
        {formattedRun(text, c.char_start, c.char_end, formatting, `cite-${k}`)}</a>);
    cursor = c.char_end;
  });
  if (cursor < segEnd) nodes.push(renderRun(text, "tail", paraSet, onPara, cursor, segEnd, formatting));
  return nodes.length ? nodes : formattedRun(text, segStart, segEnd, formatting);
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
  const t = (label || "").trim();
  // "43." / "[43]" — and "para 43", which is how every Find Case Law judgment labels its
  // paragraphs. Without the second form the reader printed "para 43" as a heading ABOVE a
  // paragraph that already opens "43.", which double-spaced the whole judgment and read
  // nothing like a law report. The number belongs in the rail, once.
  const m = /^\[?(\d{1,4})[.\]\)]?$/.exec(t)
    || /^para(?:graph)?\.?\s*(\d{1,4})$/i.exec(t);
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
  // Multi-level numbers are real citation units: "paragraph 3.19" of a code of
  // practice, "r 3.1" of the rules. Stopping at the first dot folded every paragraph of
  // a chapter onto its chapter number. Must stay identical to the server's _anchor_key.
  const m = /^([a-z]+)?\.?\s*(\d+(?:\.\d+)*[a-z]?)/.exec(t);
  if (!m || !m[2]) return null;
  const typ = m[1] ? _ANCHOR_TYPE[m[1]] : "";
  return typ ? `${typ}:${m[2]}` : m[2];
}

// Render one segment's body, de-duplicating the paragraph number: judgments store the
// number both as a label AND at the head of the prose ("1. This is an appeal…"). When the
// prose already carries it, we drop the separate label and style the inline number instead
// (greeny-blue, bold, in flow) so the text reads without a repeated, orphaned number.
// Doc types whose segments are flat numbered paragraphs (a judgment, an opinion) as
// opposed to a drafted hierarchy. Only these get the vertical paragraph rail: a
// statute's label carries real information (the section's name), which doesn't
// survive being turned on its side.
const CASE_DOC_TYPES = new Set(["judgment", "decision", "opinion", "order", "ruling"]);

// The rail's caption for a segment: "para 14", "recital 79", "art 17". Falls back to
// null when the label isn't a numbered provision (a named heading keeps its label
// in the flow instead).
const _RAIL_PREFIX: Record<string, string> = {
  paragraph: "para", recital: "recital", article: "art", section: "s",
  point: "pt", rule: "r",
};
function railCaption(s: { label: string; kind: string }): { prefix: string; num: string } | null {
  const label = (s.label || "").trim();
  const m = /^\[?(\d{1,4}[a-z]?)[.\])]?$/i.exec(label)
    || /^(?:para(?:graph)?|recital|art(?:icle)?|s(?:ection)?|pt|point|r(?:ule)?)\.?\s*(\d{1,4}[a-z]?)\b/i.exec(label);
  if (!m) return null;
  return { prefix: _RAIL_PREFIX[s.kind] || "para", num: m[1] };
}

// A statute section arrives as one segment whose body is newline-separated provisions;
// `lines` (from the backend's drafting-hierarchy reader) gives each its nesting depth.
// Each line becomes its own block so the indent applies to the WHOLE provision, wrapped
// lines included — not just the first line, which a text-indent would give.
function segLines(text: string, s: any, cites: any[], onCite: (c: any) => void,
                  paraSet?: Set<string>, onPara?: (n: string) => void, idPrefix?: string,
                  lineBadge?: (anchorPath: string) => any) {
  return (
    <>
      {s.lines.map((ln: any, i: number) => (
        <div className="stat-line" key={i}
          style={ln.depth ? { paddingLeft: `calc(var(--indent-step) * ${ln.depth})` } : undefined}>
          {renderCited(text, ln.start, ln.end, cites, onCite, paraSet, onPara, idPrefix, s.formatting)}
          {/* the sub-provision's mention badge, at the END of its own line (not bunched
              at the section foot) — only where the drafting hierarchy pinpointed the line */}
          {ln.anchor && lineBadge ? lineBadge(ln.anchor) : null}
        </div>
      ))}
    </>
  );
}

function segBody(text: string, s: { label: string; char_start: number; char_end: number; kind?: string; lines?: any[]; formatting?: any[] },
                 cites: any[], onCite: (c: any) => void, paraSet?: Set<string>, onPara?: (n: string) => void,
                 idPrefix?: string, lineBadge?: (anchorPath: string) => any) {
  // A section heading IS its own text ("Legal context"), so printing the label beside the
  // body would print it twice.
  if (s.kind === "heading") {
    return { showLabel: false,
             body: renderCited(text, s.char_start, s.char_end, cites, onCite, paraSet, onPara, idPrefix, s.formatting) };
  }
  // drafted hierarchy (legislation): render provision-by-provision, indented
  if (s.lines && s.lines.length > 1) {
    return { showLabel: true, body: segLines(text, s, cites, onCite, paraSet, onPara, idPrefix, lineBadge) };
  }
  const num = labelNum(s.label);
  const raw = text.slice(s.char_start, s.char_end);
  const m = num ? new RegExp(`^(\\s*)(${num})([.)\\]]?)(\\s+)`).exec(raw) : null;
  if (!m) return { showLabel: true, body: renderCited(text, s.char_start, s.char_end, cites, onCite, paraSet, onPara, idPrefix, s.formatting) };
  const numEnd = s.char_start + m[0].length;
  return {
    showLabel: false,
    body: <>
      <b className="seg-num">{m[2]}{m[3]}</b>{" "}
      {renderCited(text, numEnd, s.char_end, cites, onCite, paraSet, onPara, idPrefix, s.formatting)}
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
  const [doc, docErr, reload] = useAsync(() => api.document(id), [id]);
  const [body, , reloadBody] = useAsync(() => api.documentBody(id), [id]);
  const segs = (body?.segments || []) as any[];
  const inheritedRecitals = body?.inherited_recitals;
  const recitalSegs = (inheritedRecitals?.segments || []) as any[];
  // jump to the pinpointed paragraph/section once the full text has rendered
  useEffect(() => {
    if (!body?.text) return;
    const recitalIdx = matchSegIndex(recitalSegs, anchor);
    const idx = recitalIdx < 0 ? matchSegIndex(segs, anchor) : -1;
    const el = recitalIdx >= 0
      ? document.getElementById("peek-recital-" + recitalIdx)
      : idx >= 0 ? document.getElementById("peek-seg-" + idx) : null;
    if (el) setTimeout(() => { el.scrollIntoView({ behavior: "smooth", block: "start" }); el.classList.add("seg-flash"); setTimeout(() => el.classList.remove("seg-flash"), 2000); }, 60);
  }, [body, anchor]);
  // Three non-happy paths, each with a REAL affordance (a dead "Open full" on a
  // half-loaded panel was the old failure mode):
  //  - the document isn't held → the fetch prompt (targeted fetch / URL paste);
  //  - the API call itself failed (network blip, pool pressure) → retry;
  //  - still loading → say so.
  if (doc?.error) return <FetchPrompt refId={id} raw={raw} onDone={reload} />;
  if (docErr) return (
    <div>
      <p className="err">Couldn’t reach the server ({String(docErr).slice(0, 80)}).</p>
      <button className="primary" onClick={reload}>↻ Retry</button>
    </div>
  );
  if (!doc) return <p className="muted loading-pulse">Loading preview…</p>;
  const d = doc?.document;
  const cites = body?.citations || [];
  return (
    <SelectionShorthand docId={id} onLinked={reloadBody}>
      <div className="peek-doc-head">
        <b><Oscola c={(doc as any)?.oscola} fallback={d?.title || id} /></b>
        <div className="muted" style={{ fontSize: 12 }}>
          {/* name the court and its jurisdiction — never the raw slug ("ewca") */}
          {provenance([doc?.court_label || d?.court, doc?.jurisdiction])}
          <DocDate d={d} />
          {doc?.cited_by_count ? ` · cited by ${doc.cited_by_count}` : ""}{anchor ? ` · ${anchor}` : ""}</div>
        <button style={{ marginTop: 4 }} onClick={() => openFull(id, anchor)}>open full ↗</button>
      </div>
      {!body?.text && doc && <p className="muted">No text yet (metadata only).</p>}
      {inheritedRecitals?.text && recitalSegs.length > 0 && (
        <>
          <p className="leg-version-state">
            {inheritedRecitals.note}
          </p>
          <div className="reader">
            {recitalSegs.map((s, i) => {
              const sb = segBody(
                inheritedRecitals.text, s, inheritedRecitals.citations || [], onCite);
              return (
                <div className={`seg lvl${Math.min(s.level, 2)} kind-recital`}
                     key={i} id={"peek-recital-" + i}>
                  {sb.showLabel && <span className="seg-label">{s.label}</span>}
                  <span className="seg-body">{sb.body}</span>
                </div>
              );
            })}
          </div>
        </>
      )}
      {body?.text && segs.length > 0 && (
        <div className={`reader${CASE_DOC_TYPES.has(body.doc_type) ? " has-rails" : ""}`}>
          {segs.map((s, i) => {
            const sb = segBody(body.text, s, cites, onCite);
            // the same on-its-side paragraph marker as the main reader, so a
            // paragraph is identified the same way wherever it is shown
            const rail = CASE_DOC_TYPES.has(body.doc_type) ? railCaption(s) : null;
            return (
            <div className={`seg lvl${Math.min(s.level, 2)} kind-${s.kind}${rail ? " has-rail" : ""}`}
                 key={i} id={"peek-seg-" + i}>
              {rail && (
                <span className="seg-rail" aria-hidden="true">
                  <span className="rail-line" />
                  <span className="rail-label">{rail.prefix}&nbsp;<b>{rail.num}</b></span>
                  <span className="rail-line" />
                </span>
              )}
              {sb.showLabel && <span className="seg-label">{s.label}</span>}
              <span className="seg-body">{sb.body}</span>
            </div>
            );
          })}
        </div>
      )}
      {body?.text && !segs.length && <div className="reader"><div className="seg-body">{renderCited(body.text, 0, body.text.length, cites, onCite)}</div></div>}
    </SelectionShorthand>
  );
}

// The "read it here" block for an unfetched/unfetchable case: the free LII page(s) the
// citation resolves to (AustLII / NZLII / CanLII / SAFLII / HKLII / BAILII), constructed
// from the citation. A "derived" link is a real judgment URL; a search link is the fallback
// when no page can be built. Doubles as the source to save-and-upload for an in-place fix.
function ReferenceLiiLinks({ refId, raw }: { refId: string; raw?: string }) {
  const [data] = useAsync(() => api.referenceLiiLinks(refId, raw), [refId, raw]);
  const links = data?.links || [];
  if (!links.length) return null;
  return (
    <div className="lii-links" style={{ margin: "6px 0 10px" }}>
      <h4 style={{ margin: "0 0 4px", fontSize: 12 }}>Read it on a legal-information institute</h4>
      <ul style={{ margin: 0 }}>
        {links.map((l, i) => (
          <li key={i} style={{ fontSize: 13 }}>
            <a href={l.url} target="_blank" rel="noopener noreferrer">
              {l.site_name || l.site || "link"} ↗</a>
            {l.certainty && l.certainty !== "recorded" && (
              <span className={`lii-tag lii-${l.certainty}`}
                title={(LII_CERTAINTY as any)[l.certainty]}>{l.certainty}</span>
            )}
          </li>
        ))}
      </ul>
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
  // "Link to something already held" — the common case for a report citation whose
  // case IS in the corpus under a different identifier ("(1948) 1 KB 223" against a
  // judgment held by neutral citation). Fetching it again would mint a duplicate.
  const [linking, setLinking] = useState(false);
  const [q, setQ] = useState(raw || "");
  const [hits, setHits] = useState<any[] | null>(null);
  async function searchExisting() {
    setBusy(true); setMsg("searching…");
    try {
      const r = await api.searchCorpus({ query: q, limit: "8", facets: "false" });
      setHits(r.items || []);
      setMsg("");
    } catch (e: any) { setMsg("error: " + e); } finally { setBusy(false); }
  }
  async function linkTo(existing_id: string) {
    setBusy(true); setMsg("linking…");
    try {
      await api.resolveReference({ ref: raw || refId, existing_id });
      setMsg("✓ linked — opening…"); setTimeout(onDone, 600);
    } catch (e: any) { setMsg("error: " + e); } finally { setBusy(false); }
  }
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
      <ReferenceLiiLinks refId={refId} raw={raw} />
      <button className="primary" disabled={busy} onClick={fetchIt}>⤓ Try to fetch this</button>
      <div className="row" style={{ marginTop: 8 }}>
        <input value={url} onChange={(e) => setUrl(e.target.value)} placeholder="…or paste a URL (BAILII / Find Case Law) to add it" />
        <button disabled={busy || !url} style={{ flex: "0 0 auto" }} onClick={fetchUrl}>add</button>
      </div>
      {!linking ? (
        <p style={{ marginTop: 8 }}>
          <a className="mini-link" onClick={() => { setLinking(true); if (!hits) searchExisting(); }}>
            …or link it to something already in the corpus</a></p>
      ) : (
        <div style={{ marginTop: 8 }}>
          <div className="row">
            <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="search the corpus by name or citation"
              onKeyDown={(e) => { if (e.key === "Enter") searchExisting(); }} />
            <button disabled={busy || !q} style={{ flex: "0 0 auto" }} onClick={searchExisting}>find</button>
          </div>
          {hits && hits.length === 0 && <p className="muted" style={{ fontSize: 12 }}>nothing matched.</p>}
          {hits && hits.length > 0 && (
            <table><tbody>
              {hits.map((h: any) => (
                <tr key={h.stable_id}>
                  <td><Oscola c={h.oscola} fallback={h.title || h.stable_id} /></td>
                  <td className="muted" style={{ whiteSpace: "nowrap" }}>
                    {String(h.decision_date || h.effective_date || "").slice(0, 4)}</td>
                  <td style={{ whiteSpace: "nowrap" }}>
                    <button className="mini" disabled={busy}
                      title="Point this citation at that document — every other citation of it resolves too"
                      onClick={() => linkTo(h.stable_id)}>link</button></td>
                </tr>
              ))}
            </tbody></table>
          )}
        </div>
      )}
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
  // n / p step through find-matches from anywhere in the document (not while typing)
  useEffect(() => {
    if (!query) return;
    const down = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement;
      if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable)) return;
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      if (e.key === "n") { e.preventDefault(); step(1); }
      else if (e.key === "p") { e.preventDefault(); step(-1); }
    };
    window.addEventListener("keydown", down);
    return () => window.removeEventListener("keydown", down);
  });
  return (
    <nav className="doc-nav">
      <div className="doc-nav-title" title={title}><Oscola c={oscola} fallback={title || id} /></div>
      {landingUrl && <a className="doc-nav-orig" href={landingUrl} target="_blank" rel="noreferrer">link to original ↗</a>}
      <div className="doc-nav-find">
        <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Find in document"
          onKeyDown={(e) => { if (e.key === "Enter") step(e.shiftKey ? -1 : 1); }} />
        {query && <div className="doc-nav-find-n">
          {matches.length ? <>{at + 1}/{matches.length}
            <a onClick={() => step(-1)} title="previous (p)"> ‹</a><a onClick={() => step(1)} title="next (n)"> ›</a>
            <span className="muted" style={{ marginLeft: 4 }}>n/p</span></>
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

// Mobile document navigation: the desktop side index doesn't fit a phone, and iOS won't
// let you grab the scrollbar — so for long statutes/judgments a subtle "Sections" button
// rides the bottom-left once you've scrolled in, opening a bottom-sheet skeleton of the
// document (headings / sections / schedules / paragraphs) you can tap to jump to. Labels
// wrap rather than overflow, however long a section name is. Desktop hides it (CSS).
const _JUMP_KINDS = new Set(["section", "article", "schedule", "part", "chapter", "ruling", "recital"]);
function MobileJumpNav({ segs }: { segs: any[] }) {
  const [open, setOpen] = useState(false);
  const [shown, setShown] = useState(false);
  useEffect(() => {
    const onScroll = () => setShown(window.scrollY > 500);
    window.addEventListener("scroll", onScroll, { passive: true });
    onScroll();
    return () => window.removeEventListener("scroll", onScroll);
  }, []);
  if (!segs || segs.length < 2) return null;
  const structural = segs.map((s, i) => ({ s, i }))
    .filter(({ s }) => isHeading(s) || _JUMP_KINDS.has(s.kind));
  const items = structural.length >= 3 ? structural : segs.map((s, i) => ({ s, i }));
  const jump = (i: number) => { scrollToSeg(segId(segs[i].label)); setOpen(false); };
  return (
    <div className={`mjump${shown || open ? " show" : ""}`}>
      {open && (
        <>
          <div className="mjump-scrim" onClick={() => setOpen(false)} />
          <div className="mjump-sheet" role="dialog" aria-label="Jump to section">
            <div className="mjump-sheet-head"><b>Jump to</b>
              <a onClick={() => setOpen(false)} aria-label="Close">✕</a></div>
            <ol className="mjump-list">
              {items.map(({ s, i }) => (
                <li key={i} className={`nav-lvl${Math.min(s.level, 2)}`}>
                  <a onClick={() => jump(i)}>{s.label || "—"}</a></li>
              ))}
            </ol>
          </div>
        </>
      )}
      <button className="mjump-btn" onClick={() => setOpen((o) => !o)} aria-label="Jump to section">
        ☰ Sections
      </button>
    </div>
  );
}

// --- Structured reader (legislation hierarchy / judgment paragraphs) -------
// Where to read a case the corpus can't show. The AustLII-family institutes name their
// files deterministically, so the URL is constructed locally from the citation rather
// than looked up — see citations/lii.py. Nothing is fetched on the user's behalf: these
// are links a person follows, which is what those (largely charity-funded) sites expect.
const LII_CERTAINTY: Record<string, string> = {
  recorded: "the exact URL this record was imported from",
  derived: "built from the citation — this institute's filenames are deterministic",
  probable: "this institute assigns its own numbering, so the link may miss",
};

function LiiLinks({ id }: { id: string }) {
  const [data, err] = useAsync(() => api.liiLinks(id), [id]);
  if (err || !data || !data.links.length) return null;
  return (
    <div className="lii-links">
      <h4>Read this case elsewhere</h4>
      <ul>
        {data.links.map((l, i) => (
          <li key={i}>
            <a href={l.url} target="_blank" rel="noopener noreferrer">{l.site_name} ↗</a>
            {l.certainty !== "recorded" && (
              <span className={`lii-tag lii-${l.certainty}`} title={LII_CERTAINTY[l.certainty]}>
                {l.certainty}
              </span>
            )}
            <div className="muted lii-url">{l.url}</div>
          </li>
        ))}
      </ul>
    </div>
  );
}

function Reader({ id, incoming, pinpoint, oscola, landingUrl, title }:
  { id: string; incoming: any[]; pinpoint?: string | null; oscola?: OscolaCite | null; landingUrl?: string; title?: string }) {
  const { canWrite } = useAuth();  // gate per-paragraph "link authority" (＋) for readers
  const [body, , reloadBody] = useAsync(() => api.documentBody(id), [id]);
  // "original" pane: the stored source file (guidance PDF via the linkified pdf.js
  // viewer, styled BAILII HTML in a sandboxed frame) alongside the extracted text
  const rawKind = body?.raw_ext === "pdf" ? "pdf"
    : body?.raw_ext === "html" || body?.raw_ext === "htm" ? "html" : null;
  const [view, setView] = useState<"text" | "orig">("text");
  const [languageCheck, setLanguageCheck] = useState<"" | "checking" | "english" | "still-french" | "error">("");
  useEffect(() => { setView(body && !body.text && rawKind ? "orig" : "text"); }, [id, !body]);
  useEffect(() => { setLanguageCheck(""); }, [id]);
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
  const readerRef = useRef<HTMLDivElement>(null);   // minimap measures this
  const onCite = (c: any) => peek.push(citePeek(c));
  const onPara = (n: string) => scrollToSeg(segId(n + "."));   // jump to paragraph n
  const paraSet = paraNumbers(body?.segments || []);
  // deep-link: when opened at a pinpoint (a paragraph "para 80" or a section
  // "Article 17"), scroll to the matching segment.
  useEffect(() => {
    if (!body || !pinpoint) return;
    const allSegments = [
      ...(body.inherited_recitals?.segments || []),
      ...(body.segments || []),
    ];
    const idx = matchSegIndex(allSegments, pinpoint);
    if (idx >= 0) setTimeout(() => scrollToSeg(segId(allSegments[idx].label)), 80);
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
      <LiiLinks id={id} />
    </div>
  );
  const segs = body.segments as { label: string; kind: string; level: number; char_start: number; char_end: number }[];
  const cites = body.citations || [];
  const inheritedRecitals = body.inherited_recitals;
  const recitalSegs = (inheritedRecitals?.segments || []) as {
    label: string; kind: string; level: number; char_start: number; char_end: number
  }[];
  // The pinned line shows CURATED commentary links (analyses/summarises/annotations)
  // anchored to this paragraph. Plain citation edges (mentions etc.) are excluded —
  // the "Mentioned by" roll-up below already owns those, and showing both painted
  // the same case twice on one paragraph ("mentions" and "mentioned by").
  const CITE_TYPES = new Set(["mentions", "cites", "applies", "follows", "considers",
    "distinguishes", "overrules", "interprets"]);
  const pinned = (label: string) => (incoming || []).filter(
    (r) => r.dst_anchor === label && !CITE_TYPES.has(r.relationship_type));
  const isCase = CASE_DOC_TYPES.has(body.doc_type);
  const checkForEnglish = async () => {
    setLanguageCheck("checking");
    try {
      const result = await api.checkEnglishRendition(id);
      if (result.error) throw new Error(result.error);
      setLanguageCheck(result.english_available ? "english" : "still-french");
      reloadBody();
    } catch {
      setLanguageCheck("error");
    }
  };
  const languageBanner = body.language_fallback === "en-to-fr" && (
    <div className="language-fallback-box" role="note">
      <div>
        <b>Full English text is not currently available.</b>{" "}
        The complete decision below is the official French rendition.
        {body.english_oj_notice && " An English Official Journal notice containing the operative ruling is appended at the end."}
      </div>
      <div className="language-fallback-actions">
        {body.english_oj_notice && <button type="button" className="mini" onClick={() =>
          scrollToSeg(segId(body.english_oj_notice.anchor))}>Skip to the English ruling ↓</button>}
        <button type="button" className="mini" disabled={languageCheck === "checking"}
          onClick={checkForEnglish}>
          {languageCheck === "checking" ? "Checking EUR-Lex…" : "Check again for English"}
        </button>
        {languageCheck === "still-french" && <span className="muted">Still French only; this text has been kept.</span>}
        {languageCheck === "english" && <span className="ok">English is now available; the reader has been refreshed.</span>}
        {languageCheck === "error" && <span className="err">EUR-Lex could not be checked just now.</span>}
      </div>
    </div>
  );
  const recitalContent = inheritedRecitals?.text && recitalSegs.length > 0 ? (
    <div className="inherited-recitals">
      <div className="leg-version-state">
        <b>Recitals from the original act.</b>{" "}
        {inheritedRecitals.note}
        {inheritedRecitals.source_url && <>{" "}
          <a href={inheritedRecitals.source_url} target="_blank" rel="noreferrer">
            View original act ↗</a></>}
      </div>
      <div className="reader">
        {recitalSegs.map((s, i) => {
          const sb = segBody(
            inheritedRecitals.text, s, inheritedRecitals.citations || [], onCite);
          const mb = mentionsFor(s.label);
          return (
            <div className={`seg lvl${Math.min(s.level, 2)} kind-recital`
                   + (mb && mb.list.length ? " has-mentions" : "")}
                 key={i} id={segId(s.label)}>
              {canWrite && <a className="seg-plus"
                title="Link commentary or an authority to this recital"
                onClick={() => peek.push({ kind: "augment", docId: id, anchor: s.label })}>＋</a>}
              {sb.showLabel && <span className="seg-label">{s.label}</span>}
              <span className="seg-body">{sb.body}</span>
              {pinned(s.label).map((r, j) => (
                <div className="pinned" key={j}>💬 {r.relationship_type}:{" "}
                  <DocLink id={r.src_id}
                    onOpen={() => peek.push({ kind: "doc", id: r.src_id })}>
                    {r.src_title || r.src_id}</DocLink>
                  {r.src_anchor && <span className="muted"> ({r.src_anchor})</span>}
                </div>
              ))}
              {mb && mb.list.length > 0
                && <MentionedBy list={mb.list} target={id} anchor={s.label} />}
            </div>
          );
        })}
      </div>
    </div>
  ) : null;
  const content = !body.text ? null : (!segs || segs.length === 0)
    ? <div className="reader"><div className="seg"><div className="seg-body">
        {body.lines && body.lines.length > 1
          ? segLines(body.text, body, cites, onCite, paraSet, onPara)
          : renderCited(body.text, 0, body.text.length, cites, onCite, paraSet, onPara)}
      </div></div></div>
    : (
      <div className={`reader${isCase ? " has-rails" : ""}`}>
        {segs.map((s, i) => {
          // Sub-provision mention badges, each on its own provision line where the
          // drafting hierarchy pinpointed it. One that pins to no line is not shown:
          // see the note further down.
          const parts = subPartMap(mentions?.by_anchor || {}, s.label);
          const lineBadge = (anchorPath: string) => {
            const sp = parts.get(anchorPath);
            return sp ? <SubBadge sp={sp} sectionLabel={s.label} target={id} /> : null;
          };
          const sb = segBody(body.text, s, cites, onCite, paraSet, onPara, undefined, lineBadge);
          const rail = isCase ? railCaption(s) : null;
          const mb = mentionsFor(s.label);
          return (
          <div className={`seg lvl${Math.min(s.level, 2)} kind-${s.kind}`
                 + (rail ? " has-rail" : "") + (mb && mb.list.length ? " has-mentions" : "")}
               key={i} id={segId(s.label)}>
            {/* the paragraph's number, set on its side against a rule that spans the
                whole provision — subtle, because the number is usually in the prose
                too; it's there to mark the extent of the paragraph, not to shout */}
            {rail && (
              <span className="seg-rail" aria-hidden="true">
                <span className="rail-line" />
                <span className="rail-label">{rail.prefix}&nbsp;<b>{rail.num}</b></span>
                <span className="rail-line" />
              </span>
            )}
            {canWrite && <a className="seg-plus" title="Link commentary or an authority to this paragraph"
              onClick={() => peek.push({ kind: "augment", docId: id, anchor: s.label })}>＋</a>}
            {sb.showLabel && <span className="seg-label">{s.label}</span>}
            <span className="seg-body">{sb.body}</span>
            {/* Sub-paragraph badges are placed on their own provision line above, and
                ONLY there. A cited sub-part we cannot pin to a line is a pinpoint that
                does not correspond to anything — "s. 11(4)" of a section running (1),
                (2), (2A), (3), or a mangled capture — and a foot-of-section row of them
                read as "[43 mentions] [1 mention] [1 mention] [1 mention]…", one badge
                per misreading, each opening a list of one. They are already counted in
                the provision-level "Mentioned by" line below (a pinpoint folds into its
                parent's anchor key), which is the only place they can honestly be read. */}
            {pinned(s.label).map((r, j) => (
              <div className="pinned" key={j}>💬 {r.relationship_type}: <DocLink id={r.src_id} onOpen={() => peek.push({ kind: "doc", id: r.src_id })}>{r.src_title || r.src_id}</DocLink>
                {r.src_anchor && <span className="muted"> ({r.src_anchor})</span>}</div>
            ))}
            {mb && mb.list.length > 0
              && <MentionedBy list={mb.list} target={id} anchor={s.label} />}
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
    : <>{recitalContent}{content}</>;
  const navPrefix = inheritedRecitals?.text
    ? inheritedRecitals.text + "\n\n" : "";
  const navSegs = [
    ...recitalSegs,
    ...(segs || []).map((segment) => ({
      ...segment,
      char_start: segment.char_start + navPrefix.length,
      char_end: segment.char_end + navPrefix.length,
    })),
  ];
  const navText = navPrefix + (body.text || "");
  const mentionAnchors = new Set(
    navSegs.filter((s) => { const mb = mentionsFor(s.label); return mb && mb.list.length > 0; })
      .map((s) => s.label));
  return (
    <SelectionShorthand docId={id} onLinked={reloadBody}>
      <div className="doc-layout">
        <DocNav segs={navSegs} text={navText} oscola={oscola} title={title} landingUrl={landingUrl} id={id} />
        <MobileJumpNav segs={navSegs} />
        <div className="doc-main" ref={readerRef}>{languageBanner}{chips}{pdfBanner}{tabs}{main}</div>
        {view === "text" && body.text && (
          <Minimap containerRef={readerRef} segs={segs || []} cites={cites}
            mentionAnchors={mentionAnchors} textLen={body.text.length} />
        )}
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
// ``autoFocus`` is OPT-IN, and must stay that way. It used to be hard-coded on, which is
// right when the field appears because someone clicked "add" or "something else…" — and
// badly wrong when the field is simply part of a form that is already on the page. The
// Maintain page ends with the Add-rule form, so opening Admin → Maintain focused an input
// near the bottom of a very long page and the browser scrolled the caret into view: you
// arrived most of the way down a page you had not scrolled. Pass it only where the field's
// appearance IS the user's action.
export function DocAutocomplete({ initial, onPick, placeholder, autoFocus, instrumentOnly }:
  { initial?: string; onPick: (id: string, title: string) => void; placeholder?: string;
    autoFocus?: boolean; instrumentOnly?: boolean }) {
  const [q, setQ] = useState(initial || "");
  const ac = useAutosuggest<any>(
    q, (limit) => api.listDocuments({ query: q.trim(), limit: String(limit),
      ...(instrumentOnly ? { instrument_only: "true" } : {}) }),
    { batch: 8, delay: 160 });
  const opts = ac.items;
  const pick = (o: any) => o && onPick(o.stable_id, o.title || o.stable_id);
  return (
    <div className="ac">
      <input autoFocus={!!autoFocus} value={q} placeholder={placeholder || "find a case or act by name…"}
        onChange={(e) => setQ(e.target.value)}
        onKeyDown={(e) => {
          if (ac.onNavKey(e)) return;
          if (e.key === "Enter") {
            e.preventDefault();
            if (ac.onMoreRow) ac.showMore();
            // no highlight yet means "the obvious one" — the first row, as before
            else pick(ac.highlighted ?? opts[0]);
          }
        }} />
      {opts.length > 0 && <div className="ac-list">
        {opts.map((o, i) => (
          <div key={o.stable_id} className={`ac-opt${i === ac.hi ? " hi" : ""}`}
            onMouseEnter={() => ac.setHi(i)}
            onMouseDown={(e) => { e.preventDefault(); pick(o); }}>
            {/* jurisdiction token — the search spans every jurisdiction, so a UK case
                citing an Irish Act shows an "Ireland" tag right in the dropdown */}
            {o.jurisdiction && o.jurisdiction !== "Other" &&
              <span className="tag ac-jur" title={o.court_label || o.court || o.jurisdiction}>{o.jurisdiction}</span>}
            <b>{o.title || o.stable_id}</b>
            <span className="muted"> {o.court_label || `${o.source}/${o.doc_type}`} · {o.stable_id}</span>
          </div>
        ))}
        {ac.hasMore && <AcMore onClick={ac.showMore} loading={ac.loading}
          hi={ac.onMoreRow} onHover={() => ac.setHi(opts.length)} />}
      </div>}
    </div>
  );
}

// --- Highlight a word → make it a shorthand rule for a case/act ------------
// Pick a target case/act (name autocomplete), then optionally a pinpoint WITHIN it — a
// paragraph, article, section, schedule or recital — autocompleted from the target's own
// structure (its segment labels). Used by the highlight-to-link popover.
function LinkTargetPicker({ initial, onCreate }:
  { initial: string; onCreate: (id: string, title: string, pinpoint?: string, alsoAlias?: boolean) => void }) {
  const [target, setTarget] = useState<{ id: string; title: string } | null>(null);
  const [pin, setPin] = useState("");
  const [alsoAlias, setAlsoAlias] = useState(false);
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
          onClick={() => onCreate(target.id, target.title, pin.trim() || undefined, alsoAlias)}>Link</button>
      </div>
      <label style={{ fontSize: 11, display: "flex", gap: 5, alignItems: "center", marginTop: 5 }} className="muted"
        title="Also make this phrase link to the target EVERYWHERE (a corpus-wide shorthand rule, applied on the next re-extraction). Leave off for a one-off link here.">
        <input type="checkbox" checked={alsoAlias} onChange={(e) => setAlsoAlias(e.target.checked)} />
        also apply this phrase everywhere (shorthand rule)
      </label>
    </div>
  );
}

type SelInfo = {
  text: string; x: number; y: number;
  anchor: string | null;           // enclosing segment's label, when the selection is in one
  context: string;                 // the enclosing segment's text (truncated)
  links: { text: string; state: string; title: string | null }[];  // citations linked in that segment NOW
};

function SelectionShorthand({ children, docId, onLinked }: { children: any; docId?: string; onLinked?: () => void }) {
  // Linking-by-highlight mutates the citation graph → admins only. Flagging a passage is a
  // reader-safe write, so it stays. (Both are enforced server-side too.)
  const { canWrite } = useAuth();
  const ref = useRef<HTMLDivElement>(null);
  const [sel, setSel] = useState<SelInfo | null>(null);
  const [mode, setMode] = useState<"menu" | "link" | "flag">("menu");
  const [note, setNote] = useState("");
  const [msg, setMsg] = useState("");
  // Whether the current click STARTED inside the popover. Testing the mouseup
  // target is not enough: picking an autocomplete suggestion fires on mousedown,
  // which re-renders the popover and unmounts the row that was clicked, so by
  // mouseup `e.target` is detached and `closest(".sel-pop")` finds nothing —
  // the guard fell through and dismissed the popover mid-task. Recorded in the
  // capture phase, before React can unmount anything.
  const downInPop = useRef(false);
  useEffect(() => {
    function onDown(e: MouseEvent) {
      downInPop.current = !!(e.target as HTMLElement)?.closest?.(".sel-pop");
    }
    function onUp(e: MouseEvent) {
      if (downInPop.current) { downInPop.current = false; return; }
      if ((e.target as HTMLElement)?.closest?.(".sel-pop")) return;  // clicking inside our popover
      const s = window.getSelection();
      const text = s?.toString().trim() || "";
      // Allow much longer selections than a shorthand rule would want, because
      // flagging a badly-linked PASSAGE for refinement often spans several lines.
      // The cap is only to avoid a whole-document accidental select-all; the "Link"
      // action self-limits (it's hidden for long selections — see the menu below).
      if (!text || text.length > 4000 || !ref.current || !s?.anchorNode || !ref.current.contains(s.anchorNode)) {
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
    document.addEventListener("mousedown", onDown, true);
    document.addEventListener("mouseup", onUp);
    return () => {
      document.removeEventListener("mousedown", onDown, true);
      document.removeEventListener("mouseup", onUp);
    };
  }, []);
  const dismiss = (delay = 2400) =>
    setTimeout(() => { setSel(null); setMsg(""); setMode("menu"); window.getSelection()?.removeAllRanges(); }, delay);
  const create = async (id: string, title: string, pinpoint?: string, alsoAlias?: boolean) => {
    if (!sel) return;
    try {
      // Anchor a manual citation AT the highlighted span, so it renders inline right here
      // (and survives re-extraction) — the fix for "my manual link never showed up".
      if (docId) {
        const r = await api.linkAtSelection({ doc_id: docId, target_id: id, selected_text: sel.text,
          context: sel.context, pinpoint });
        if (r.error) { setMsg("error: " + r.error); return; }
      }
      // Optionally ALSO define the corpus-wide phrase → target shorthand (propagates to
      // other documents on the next extraction). Opt-in now, so a one-off link doesn't
      // silently rewrite every occurrence everywhere.
      if (alsoAlias) { try { await api.createAlias(sel.text, id); } catch { /* non-fatal */ } }
      setMsg(`✓ linked “${sel.text}” → ${title}${pinpoint ? " · " + pinpoint : ""}${alsoAlias ? " · +shorthand" : ""}`);
      onLinked?.();  // refetch the reader body so the new inline link appears immediately
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
              {/* a shorthand rule wants a short phrase; hide Link for a long passage
                  selection (which is a flag-for-refinement, not an alias) */}
              {canWrite && sel.text.length <= 80 && (
                <button style={{ flex: "0 0 auto" }} onClick={() => setMode("link")}>
                  🔖 Link “{sel.text.length > 24 ? sel.text.slice(0, 24) + "…" : sel.text}” to…</button>
              )}
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
  const parts = Array.isArray(c?.parts) ? c.parts : [];
  if (parts.length === 0) return <>{fallback ?? ""}</>;
  return <>{parts.map((p, i) => p?.i ? <i key={i}>{String(p?.t ?? "")}</i> : <Fragment key={i}>{String(p?.t ?? "")}</Fragment>)}</>;
}

// "Court · jurisdiction · date" for a document head. Some court labels already name
// their country — a national data-protection authority is labelled "Data Protection
// Authority · Belgium", because a rail of thirty identical "Data Protection Authority"
// rows would be useless — and naively appending the jurisdiction then read
// "… · Belgium · Belgium". Drop the part the label already says.
export function provenance(parts: (string | null | undefined)[]): string {
  const out: string[] = [];
  for (const raw of parts) {
    const p = (raw || "").trim();
    if (!p) continue;
    if (out.some((seen) => seen === p || seen.split(" · ").includes(p))) continue;
    out.push(p);
  }
  return out.join(" · ");
}

// --- Document reader + augment ---------------------------------------------
// The originating decision links a report carries but whose text we deliberately do not
// hold (e.g. a GDPRhub case report → the DPA's own decision page/PDF). Shown as
// "See on <source> (<language>) ↗" so a reader — or an MCP bot — can reach the original.
function OriginalSources({ meta }: { meta?: any }) {
  const srcs: any[] = (meta && meta.original_sources) || [];
  if (!srcs.length) return null;
  return (
    <p className="original-sources muted" title="The issuing authority's own copy of this decision (not held in the corpus)">
      Original {srcs.length > 1 ? "sources" : "source"}:{" "}
      {srcs.map((s, i) => (
        <Fragment key={i}>
          {i > 0 && <span className="summary-sep"> · </span>}
          {s.url
            ? <a href={s.url} target="_blank" rel="noopener noreferrer">
                See on {s.name || "source"}{s.language ? ` (${s.language})` : ""} ↗</a>
            : <span>{s.name}{s.language ? ` (${s.language})` : ""}</span>}
        </Fragment>
      ))}
    </p>
  );
}

// "Before the Court": the Article 267 references pending on this instrument, with the
// provisions each turns on. A statute page otherwise shows only settled law — this is
// the part that is still moving, and it is the first thing an adviser needs to know.
function PendingReferencesBox({ id, open }: { id: string; open: (id: string, a?: string) => void }) {
  // The aggregate is cached server-side (stale-while-revalidate); a COLD load returns
  // {preliminary: [], _warming: true} and computes in the background. Without the poll
  // the box read "no references pending" on every first visit to a statute — and the
  // cache is dropped whenever the citation graph changes, so first visits are common.
  const [data, , reload] = useAsync<any>(() => api.pendingReferences(id), [id]);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [showAll, setShowAll] = useState(false);
  const { push } = useTray();
  useEffect(() => {
    if (!data?._warming) return;
    const iv = setInterval(() => reload(), 2500);
    return () => clearInterval(iv);
  }, [data?._warming]);
  if (!data || data.error) return null;
  if (data._warming) return (
    <div className="panel pref-box">
      <p className="muted loading-pulse">⏳ Finding the cases pending before the Court…</p>
    </div>
  );
  const prelim: any[] = data.preliminary || [];
  const other: any[] = data.other || [];
  // One list: references first, then the direct actions (annulments, appeals, staff
  // cases). They are all "what is before the Court on this instrument", and each row
  // says which kind it is.
  const rows: any[] = data.pending || [...prelim, ...other];
  if (!rows.length) return null;
  // Three is what fits above the fold beside everything else a statute page opens with.
  const visible = showAll ? rows : rows.slice(0, 3);

  // Which provisions a reference is about, in the order the instrument is written.
  const provisions = (anchors: string[]) => {
    const recitals = anchors.filter((a) => /^recital/i.test(a));
    const rest = anchors.filter((a) => !/^recital/i.test(a));
    return [...recitals, ...rest];
  };

  // Rows open in the TRAY, not the main view: you are reading a statute and glancing at
  // what is pending on it — replacing the page you are on loses your place, and the
  // notice is a paragraph long. ⌘-click still opens a tab (DocLink keeps the href).
  const row = (r: any) => {
    const provs = provisions(r.anchors || []);
    const isOpen = expanded === r.stable_id;
    // Previewed inline, capped to what fits on ONE line: the provisions ARE the reason
    // to look at this list, so making every row a click to find out defeats it.
    const PREVIEW = 4;
    const shown = isOpen ? provs : provs.slice(0, PREVIEW);
    return (
      <div className="pref-row" key={r.stable_id}>
        <div className="pref-head">
          <DocLink className="pref-case" id={r.stable_id}
            onOpen={() => push({ kind: "doc", id: r.stable_id,
              label: <>{r.case_number || r.stable_id}</> })}
            title="Open the pending notice in a side tray (⌘-click for a new tab)">
            {r.case_number || r.stable_id}</DocLink>
          <span className="pref-title">{String(r.title || "")
            .replace(/^Pending:\s*/, "").replace(/\s*\([CT]-\d+\/\d+\)\s*$/, "")}</span>
          <span className={`tag${r.preliminary ? " tag-pending" : ""}`}
            title={r.preliminary
              ? "An Article 267 reference: a national court asking what this text means"
              : "A direct action before the Court citing this instrument — it does not ask what the text means"}>
            {r.procedure_label || (r.preliminary ? "Preliminary reference" : "Pending")}</span>
          {r.ag_opinion && (
            <DocLink className="tag tag-ag" id={r.ag_opinion.stable_id}
              onOpen={() => push({ kind: "doc", id: r.ag_opinion.stable_id,
                label: <>AG opinion · {r.case_number || r.ag_opinion.stable_id}</> })}
              title={`Opinion of the Advocate General${r.ag_opinion.advocate_general
                ? ` ${r.ag_opinion.advocate_general}` : ""}${r.ag_opinion.date
                ? `, ${r.ag_opinion.date}` : ""} — delivered, but the Court has not yet ruled`}>
              AG opinion{r.ag_opinion.date ? ` · ${String(r.ag_opinion.date).slice(0, 4)}` : ""}</DocLink>
          )}
        </div>
        <div className={`pref-meta muted${isOpen ? "" : " one-line"}`}>
          {[r.referring_court, r.origin_country, r.date].filter(Boolean).join(" · ")}
          {provs.length > 0 && <>
            {" · "}
            {shown.map((a) => (
              <a key={a} className="pref-prov" title={`Go to ${a} and see everything that cites it`}
                onClick={() => open(id, a)}>{a}</a>
            ))}
            {provs.length > PREVIEW && (
              <a className="pref-provs" onClick={() => setExpanded(isOpen ? null : r.stable_id)}
                title={isOpen ? "Collapse" : `All ${provs.length} provisions this case turns on`}>
                {isOpen ? "show fewer" : `see all ${provs.length}`}</a>
            )}
          </>}
        </div>
      </div>
    );
  };

  return (
    <div className="panel pref-box">
      <h3 className="pref-h">
        Before the Court
        <span className="muted">
          {" — "}{prelim.length} preliminary reference{prelim.length === 1 ? "" : "s"}
          {other.length > 0 && ` and ${other.length} other pending proceeding${other.length === 1 ? "" : "s"}`}
          {data.with_ag_opinion ? `, ${data.with_ag_opinion} with an AG opinion` : ""}
        </span>
      </h3>
      <div className="pref-list">{visible.map(row)}</div>
      {rows.length > 3 && (
        <a className="mini-link show-more" onClick={() => setShowAll((v) => !v)}
          title={showAll ? "Collapse the list" : "Show every pending proceeding on this instrument"}>
          {showAll ? "▴ Show fewer" : `▾ Show all ${rows.length}`}</a>
      )}
      {data.stale_count > 0 && (
        <p className="muted pref-stale">
          {data.stale_count} further notice{data.stale_count === 1 ? " is" : "s are"} older than{" "}
          {data.stale_after_years} years and no longer counted as live — a reference that
          old has almost certainly been decided or withdrawn.
        </p>
      )}
    </div>
  );
}

function StaticExportMenu({ id }: { id: string }) {
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");

  const download = async () => {
    setBusy(true);
    setMessage("Preparing the current export…");
    try {
      const filename = await api.downloadStaticLaw(id, setMessage);
      setMessage(`Downloaded ${filename}`);
    } catch {
      setMessage("The static edition could not be prepared.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <details className="doc-action-menu">
      <summary aria-label="Document actions" title="Document actions">…</summary>
      <div className="doc-action-sheet">
        <button type="button" disabled={busy} onClick={download}>
          {busy ? "Preparing static edition…" : "Build and download static edition"}
        </button>
        <p>One HTML file built from the current corpus, with this text, citation excerpts and public source links.</p>
        {message && <p className={message.startsWith("The ") ? "err" : "muted"}>{message}</p>}
      </div>
    </details>
  );
}

export function DocumentView({ id, open, openGraph, pinpoint, onCitation }: {
  id: string;
  open: (id: string, a?: string, replace?: boolean) => void;
  openGraph: (id: string) => void;
  pinpoint?: string | null;
  onCitation?: (citation: string) => void;
}) {
  const [displayId, setDisplayId] = useState(id);
  const [showingOriginal, setShowingOriginal] = useState(false);
  useEffect(() => {
    setDisplayId(id);
    setShowingOriginal(false);
  }, [id]);
  const [doc, err, reload] = useAsync(() => api.document(displayId), [displayId]);
  const canonicalRead = doc?.canonical_read?.stable_id;
  const displayedCitation = String(doc?.oscola?.text || doc?.document?.title || "").trim();
  useEffect(() => {
    if (displayedCitation) onCitation?.(displayedCitation);
    // The callback deliberately updates the current history entry; the citation is the
    // only data dependency and prevents a parent render from relabelling in a loop.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [displayedCitation]);
  useEffect(() => {
    if (!showingOriginal && canonicalRead && canonicalRead !== id) {
      open(canonicalRead, pinpoint || undefined, true);
    }
  }, [canonicalRead, showingOriginal, id, pinpoint]);
  const [pinAnchor, setPinAnchor] = useState("");
  const [editing, setEditing] = useState(false);
  // Options (find-citing, graph, fix-metadata) and provenance metadata are hidden by default
  // behind a subtle toggle so the reading surface stays uncluttered.
  const [showOpts, setShowOpts] = useState(false);
  const tray = useTray();
  const { canWrite } = useAuth();  // readers get a read-only document view
  if (err) return <p className="err">{err}</p>;
  if (!doc) return <p className="muted loading-pulse">Loading…</p>;
  if (doc.error) return <p className="err">{doc.error}: {id}</p>;
  const d = doc.document;
  const versions = doc.versions || [];
  return (
    <div>
      <div className="panel">
        <div className="doc-title-row">
          <h2 className="doc-title" style={{ marginTop: 0 }}><Oscola c={doc.oscola} fallback={d.title || d.stable_id} /></h2>
          {canWrite && d.doc_type === "legislation" && <StaticExportMenu id={d.stable_id} />}
        </div>
        {/* who decided this, and where — "Court of Appeal (Civil Division) ·
            England & Wales", matching the typology the explorer uses */}
        <div className="doc-provenance muted">
          <FlagIcon jurisdiction={doc.jurisdiction} opacity={0.85} />{" "}
          {provenance([doc.court_label || d.court, doc.jurisdiction])}
          <DocDate d={d} />
          {d.landing_url && (
            <> · <a href={d.landing_url} target="_blank" rel="noopener noreferrer"
                    title={`open at ${doc.link_label}`}>{doc.link_label} ↗</a></>
          )}
        </div>
        <div className="doc-summary">
          <a className="summary-stat" title="Later documents that cite this one — the full list is at the foot of the page"
            onClick={() => {
              const el = document.getElementById("cited-by-panel");
              if (el) { el.scrollIntoView({ behavior: "smooth", block: "start" }); el.classList.add("seg-flash"); setTimeout(() => el.classList.remove("seg-flash"), 1500); }
              else tray.push({ kind: "mentions", target: d.stable_id, label: "Citations to this decision" });
            }}>Cited by <b>{doc.cited_by_count ?? 0}</b> ↓</a>
          <span className="summary-sep">|</span>
          <a className="summary-stat" title="Distinct cases this document cites"
            onClick={() => tray.push({ kind: "cites", target: d.stable_id, family: "cases", label: "Cases cited" })}>Cases cited <b>{doc.cases_cited_count ?? 0}</b></a>
          <span className="summary-sep">|</span>
          <a className="summary-stat" title="Distinct statutory material this document cites"
            onClick={() => tray.push({ kind: "cites", target: d.stable_id, family: "statute", label: "Statutory material cited" })}>Statutory material cited <b>{doc.statute_cited_count ?? 0}</b></a>
        </div>
        <CitatorStrip id={d.stable_id} />
        {/* Who decided it, and who argued it — read off the judgment's own first page
            (meta.coram / meta.representation) and standardised to the way a lawyer writes
            a judge's name. Absent for a document whose header we couldn't read. */}
        {(doc.meta?.coram?.length || doc.meta?.representation?.length) && (
          <div className="bench-box">
            {doc.meta?.coram?.length > 0 && (
              <div><span className="bench-label">Before</span>{" "}
                {doc.meta.coram.join(" · ")}</div>)}
            {doc.meta?.representation?.length > 0 && (
              <div className="bench-rep muted">{doc.meta.representation.map((r: string, i: number) =>
                <div key={i}>{r}</div>)}</div>)}
          </div>
        )}
        {doc.companion && (
          /* The other half of a CJEU case. Reading the judgment you want to know an
             Opinion exists (and vice versa) — it is a different document with a different
             ECLI, so nothing else on the page says so. */
          <p className="companion-box">
            <span className="companion-label">
              {doc.companion.role === "ag_opinion" ? "Opinion of the Advocate General" : "Judgment of the Court"}</span>{" "}
            <DocLink id={doc.companion.stable_id} onOpen={() => open(doc.companion.stable_id)}
              title={`Open ${doc.companion.role === "ag_opinion" ? "the AG Opinion" : "the judgment"} (⌘-click for a new tab)`}>
              {doc.companion.role === "ag_opinion" ? "Read the AG Opinion" : "Read the judgment"} →</DocLink>
            <span className="muted"> <Oscola c={doc.companion.oscola} fallback={doc.companion.stable_id} />
              {doc.companion.date ? ` · ${doc.companion.date}` : ""}</span>
          </p>
        )}
        {doc.counterpart && (
          /* The same instrument on the other side of the 2020 split. Two separate laws
             that started identical and drift apart with every amendment — a reader of
             one needs a route to the other to check a citation's real force. */
          <p className="companion-box counterpart-box">
            <span className="companion-label">
              {doc.counterpart.role === "eu_original" ? "EU original" : "UK assimilated version"}</span>{" "}
            {doc.counterpart.stable_id
              ? <DocLink id={doc.counterpart.stable_id} onOpen={() => open(doc.counterpart.stable_id)}
                  title={doc.counterpart.note}>
                  {doc.counterpart.title || doc.counterpart.stable_id} →</DocLink>
              : <a href={doc.counterpart.url} target="_blank" rel="noopener noreferrer"
                   title={doc.counterpart.note}>
                  {doc.counterpart.celex || doc.counterpart.title} (not held — open the official text) ↗</a>}
            <span className="muted"> {doc.counterpart.note}</span>
          </p>
        )}
        {(doc.also_cited_as || []).length > 0 && (
          <p className="also-cited muted" title="Alternative citation forms linked to this document (parallel-citation mining, report matching, your confirmations)">
            Also cited as {doc.also_cited_as.map((a: string, i: number) =>
              <Fragment key={i}>{i > 0 && <span className="summary-sep"> · </span>}<b>{a}</b></Fragment>)}
          </p>
        )}
        <OriginalSources meta={doc.meta} />
        <a className="opts-toggle muted" onClick={() => setShowOpts((v) => !v)}>
          {showOpts ? "▾ Hide options and metadata" : "▸ Expand options and metadata"}</a>
        {showOpts && (
          <div className="opts-tray">
            <div className="row" style={{ alignItems: "flex-start" }}>
              {canWrite && <FindCiting seed={d.stable_id} onDone={reload} />}
              {canWrite && <button onClick={() => setEditing((e) => !e)} style={{ flex: "0 0 auto" }}>✎ {editing ? "cancel" : "fix metadata"}</button>}
              <button onClick={() => openGraph(d.stable_id)} style={{ flex: "0 0 auto" }}>◴ View citation graph</button>
            </div>
            <p className="muted" style={{ marginTop: 8 }}>{d.ecli || d.stable_id} · {d.source}/{d.court} · {docTypeLabel(d.doc_type)}
              {" "}· added_by <b>{d.added_by}</b> · v{d.version} · {d.upstream_status}
              {d.landing_url && <> · <a href={d.landing_url} target="_blank" rel="noreferrer">open original ↗</a></>}</p>
            {editing && canWrite && <MetadataEditor d={d} onDone={() => { setEditing(false); reload(); }} />}
            <div>{(doc.tags || []).map((t: any, i: number) => (
              <span className="tag" key={i}>{t.tag} · {t.method}
                {canWrite && t.method === "manual" && <a title="remove tag" style={{ cursor: "pointer", marginLeft: 4 }}
                  onClick={async () => { await api.untag(d.stable_id, t.tag); reload(); }}>✗</a>}
              </span>
            ))}</div>
            {versions.length > 0 && <p className="versions">Version history: v{d.version} (latest){versions.map((v: any) =>
              <span key={v.version}> · v{v.version} archived {String(v.archived_at).slice(0, 10)}</span>)}</p>}
          </div>
        )}
      </div>
      {doc.original_act && !showingOriginal && (
        <div className="leg-version-state">
          Reading the latest consolidation applicable today.{" "}
          <button type="button" className="mini" onClick={() => {
            setShowingOriginal(true);
            setDisplayId(doc.original_act.stable_id);
          }}>View the original act</button>
        </div>
      )}
      {showingOriginal && (
        <div className="leg-version-state">
          Reading the original/base act.{" "}
          <button type="button" className="mini" onClick={() => {
            setShowingOriginal(false);
            setDisplayId(id);
          }}>Return to the applicable consolidation</button>
        </div>
      )}
      {/* Above the currency banner, not below it: on a consolidated act that banner runs
          to several paragraphs (version state, amendments, effects), which pushed "what
          is still before the Court" off the first screen — the one thing on the page that
          is not yet settled law. */}
      {d.doc_type === "legislation" && <PendingReferencesBox id={d.stable_id} open={open} />}
      {d.doc_type === "legislation" && <LegStatusBanner id={d.stable_id} open={open} />}
      <div className="panel">
        <Reader id={d.stable_id} incoming={doc.incoming || []} pinpoint={pinpoint}
          oscola={doc.oscola} title={d.title || d.stable_id} landingUrl={d.landing_url} />
      </div>
      {(doc.incoming || []).length > 0 &&
        <div id="cited-by-panel"><CitedByPanel id={d.stable_id} incoming={doc.incoming}
          count={doc.version_cited_by_count ?? doc.direct_cited_by_count ?? doc.cited_by_count}
          inferred={doc.inferred_by_count} /></div>}
      {(doc.inherited_incoming || []).length > 0 &&
        <InheritedProvisionMentions incoming={doc.inherited_incoming}
          mappings={doc.provision_mappings || []} open={open} />}
      <RelatedPanel id={d.stable_id} open={open} />
      {d.doc_type === "legislation" && <EffectsBanner id={d.stable_id} open={open} />}
      {d.doc_type === "legislation" && <ChangesPanel id={d.stable_id} open={open} />}
      {d.doc_type === "legislation" && canWrite && <ProvisionMappingPanel id={d.stable_id} open={open} />}
      {d.doc_type === "legislation" && <VersionPanel id={d.stable_id} open={open} />}
      {canWrite && <AugmentPanel docId={d.stable_id} onDone={reload} pinAnchor={pinAnchor} clearPin={() => setPinAnchor("")} />}
      <div className="grid2">
        <OutgoingCitationsPanel relations={doc.relations || []} open={open}
          suppressed={doc.suppressed_count} onDone={reload} />
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

// Everything this document cites, as editable rows. Its own component so the list can
// be capped with a hook: a judgment citing 300 authorities rendered 300 rows and buried
// the panels after it.
function OutgoingCitationsPanel({ relations, open, suppressed, onDone }:
  { relations: any[]; open: (id: string, a?: string) => void; suppressed?: number; onDone: () => void }) {
  const [visible, showMore] = useShowMore(relations);
  return (
    <div className="panel">
      <h3>Citations (outgoing) <span className="muted">— reclassify, re-point, or reject (✗) a wrong citation</span></h3>
      {relations.length === 0 && <p className="muted">none</p>}
      <table><tbody>
        {visible.map((r: any) => (
          <RelationRow key={r.relation_id} r={r} open={open} onDone={onDone} />
        ))}
      </tbody></table>
      {showMore}
      {(suppressed || 0) > 0 && <p className="muted">+ {suppressed} suppressed (rejected) citation(s) hidden</p>}
    </div>
  );
}

// "Cited by" — JADE's reverse-citation gloss, but treatment-aware: it shows not
// just who cites this authority, but HOW (follows / distinguishes / overrules …).
function CitedByPanel({ id, incoming, count, inferred }: { id?: string; incoming: any[]; count?: number; inferred?: number }) {
  const peek = usePeek();
  const tray = useTray();
  const open = (id: string, a?: string) => peek.push({ kind: "doc", id, anchor: a });
  // ordered by the citing document's own network authority (server default);
  // the discreet control swaps to recency within the loaded slice
  const [sort, setSort] = useState<"authority" | "newest" | "oldest">("authority");
  const [page, setPage] = useState(0);
  const [listOpen, setListOpen] = useState(false);
  // which citing documents have their extra passages disclosed
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const PER = 50;

  // Cross-section tokens — "UK cases 7 | EU legislation 35". A citing body reads
  // very differently depending on where it comes from and what kind of instrument
  // it is, so let the reader slice on both at once.
  //
  // Counts come from the WHOLE-corpus breakdown, not the loaded rows: the loaded
  // page is the top slice by PageRank, and on a mega-authority that slice fills
  // with a few jurisdictions' heavyweights — 2,484 French decisions citing the
  // GDPR once read as "no French case law" because none cracked the top page.
  const KIND_LABEL: Record<string, string> = {
    cases: "cases", legislation: "legislation", guidance: "guidance & reports",
    preparatory: "preparatory documents", explanatory: "explanatory notes",
    // A question put to the Court is not a decision of it. Kept apart from "cases" (and
    // out of the "other" bucket that swallowed it before): a pending reference is what
    // is about to change, not what has been settled.
    preliminary_references: "preliminary references (pending)",
    pending_cases: "other pending proceedings",
    administrative: "admin decisions", other: "other",
  };
  const [slice, setSlice] = useState<string | null>(null);
  const [breakdown] = useAsync(
    () => (id ? api.citedByBreakdown(id) : Promise.resolve(null as any)), [id]);
  const facets = new Map<string, { jur: string; kind: string; n: number }>();
  for (const r of incoming) {
    const jur = r.src_jurisdiction, kind = r.src_kind;
    if (!jur || !kind) continue;
    const key = `${jur}|${kind}`;
    const f = facets.get(key) || { jur, kind, n: 0 };
    f.n++; facets.set(key, f);
  }
  // biggest cross-sections first — the long tail of one-offs stays out of the way
  const tokens: [string, { jur: string; kind: string; n: number }][] =
    breakdown?.buckets?.length
      ? breakdown.buckets.slice(0, 12).map((b: any) =>
          [`${b.jurisdiction}|${b.kind}`,
           { jur: b.jurisdiction, kind: b.kind, n: b.documents }] as
             [string, { jur: string; kind: string; n: number }])
      : [...facets.entries()].sort((a, b) => b[1].n - a[1].n).slice(0, 10);
  // A clicked facet whose documents mostly fall OUTSIDE the loaded top slice is
  // fetched from the server (top citers of that jurisdiction × kind, PageRank-
  // ordered) rather than filtered down to the few that happened to be loaded.
  const [sliceRows, setSliceRows] = useState<any[] | null>(null);
  useEffect(() => {
    setSliceRows(null);
    if (!slice || !id) return;
    const loaded = incoming.filter((r) => `${r.src_jurisdiction}|${r.src_kind}` === slice).length;
    const want = tokens.find(([k]) => k === slice)?.[1].n ?? 0;
    if (want <= loaded) return;
    let alive = true;
    const [jur, kind] = slice.split("|");
    api.citedBySlice(id, jur, kind).then((r) => { if (alive) setSliceRows(r.incoming || []); })
      .catch(() => { /* keep the loaded-rows fallback */ });
    return () => { alive = false; };
  }, [slice, id]);
  const filtered = slice
    ? (sliceRows ?? incoming.filter((r) => `${r.src_jurisdiction}|${r.src_kind}` === slice))
    : incoming;

  const sorted = [...filtered].sort((a, b) =>
    sort === "authority" ? (b.src_authority || 0) - (a.src_authority || 0)
    : sort === "newest" ? String(b.src_date || "").localeCompare(String(a.src_date || ""))
    : String(a.src_date || "9999").localeCompare(String(b.src_date || "9999")));
  // The server sends one row per citing DOCUMENT (strongest treatment wins) and hands
  // over the document's other citing passages alongside, so a judgment that engaged
  // with this authority five times reads as one citer that did so five times.
  const shown = sorted.map((r, i) => ({
    key: r.src_id || String(i), head: r, rest: (r.other_passages || []) as any[],
  }));
  const byType: Record<string, number> = {};
  for (const r of incoming) byType[r.relationship_type] = (byType[r.relationship_type] || 0) + 1;
  const order = ["overrules", "distinguishes", "applies", "follows", "considers", "mentions"];
  const colour: Record<string, string> = { overrules: "var(--bad)", distinguishes: "var(--warn)", applies: "var(--ok)", follows: "var(--ok)" };
  // "mentions" is confusing from the cited-authority's side — read it as "mentioned by".
  const treat = (t: string) => (t === "mentions" ? "mentioned by" : relationLabel(t));
  // plain-language explanation of each treatment, for a rollover on the chips
  const TREAT_HELP: Record<string, string> = {
    overrules: "A later court held this decision was wrong and replaced it.",
    distinguishes: "A later court set this decision aside as not applying to its facts.",
    applies: "A later document applied this decision's rule to its own case.",
    follows: "A later court followed this decision as binding or persuasive.",
    considers: "A later document discussed this decision without applying or rejecting it.",
    mentions: "A later document referred to this one (the general case — no specific treatment detected).",
  };
  return (
    <div className="panel">
      <select className="sort-select" style={{ float: "right" }} value={sort} aria-label="ordering"
        onChange={(e) => setSort(e.target.value as any)}>
        <option value="authority">most authoritative</option>
        <option value="newest">newest first</option>
        <option value="oldest">oldest first</option>
      </select>
      <h3>Cited by <b>{(count ?? incoming.length).toLocaleString()}</b> later {(count ?? incoming.length) === 1 ? "document" : "documents"}
        {" "}<Info t="Documents elsewhere in the corpus that cite THIS one. The coloured chips below break them down by how they treat it (applied, distinguished, overruled…) and by where they come from. Click any document to open it." />
        {slice && (() => {
          const f = tokens.find(([k]) => k === slice)?.[1] || facets.get(slice);
          return <span className="muted" style={{ fontWeight: 400 }}> — showing the <b>{shown.length}</b> that {f ? `are ${f.jur} ${KIND_LABEL[f.kind] || f.kind}` : "match"}</span>;
        })()}
        {inferred ? <span className="muted" style={{ fontWeight: 400 }}> {" · "}
          plus <b>{inferred.toLocaleString()}</b> auto-linked {inferred === 1 ? "reference" : "references"}
          {" "}<Info t={`These are references RagLex joined up itself but that nobody wrote as a citation — for example a bare "Section 12" that we attached to the last-named Act. They're likely right but unconfirmed, so they're kept separate and NOT counted in the "cited by" total above.`} /></span> : null}</h3>
      <div className="active-chips" style={{ marginBottom: 6 }}>
        {order.filter((t) => byType[t]).map((t) => (
          <span key={t} className="tag" title={TREAT_HELP[t] || `${treat(t)} this document`}
            style={{ borderColor: colour[t] || "var(--line)", color: colour[t] || "inherit", cursor: "help" }}>
            {byType[t]} {treat(t)}</span>
        ))}
      </div>
      {tokens.length > 1 && (
        <div className="active-chips cited-by-facets" style={{ marginBottom: 8 }}>
          {tokens.map(([key, f]) => (
            <button key={key} className={`tag tag-btn${slice === key ? " on" : ""}`}
              title={`Show only ${f.jur} ${KIND_LABEL[f.kind] || f.kind} citing this`}
              onClick={() => { setSlice(slice === key ? null : key); setPage(0); }}>
              <FlagIcon jurisdiction={f.jur} size={0.9} /> {f.jur} {KIND_LABEL[f.kind] || f.kind} <b>{f.n}</b></button>
          ))}
          {slice && (
            <button className="tag tag-btn tag-clear" onClick={() => { setSlice(null); setPage(0); }}
              title="Show every citing document again">clear ✕</button>
          )}
        </div>
      )}
      <table><tbody>
        {(listOpen ? shown.slice(page * PER, (page + 1) * PER) : shown.slice(0, 10)).map((g) => {
          const r = g.head;
          const opened = expanded.has(g.key);
          return (
          <Fragment key={g.key}>
            <tr>
              <td style={{ whiteSpace: "nowrap", color: colour[r.relationship_type] || "var(--subtext)" }}>{treat(r.relationship_type)}</td>
              <td><FlagIcon jurisdiction={r.src_jurisdiction} />{" "}
                <DocLink id={r.src_id} anchor={r.dst_anchor} onOpen={() => open(r.src_id, r.dst_anchor)}>
                  <Oscola c={r.src_oscola} fallback={r.src_title || r.src_id} /></DocLink>
                {r.dst_anchor && <span className="muted"> → {r.dst_anchor}</span>}
                {r.src_cited_by ? <span className="muted" style={{ fontSize: 11 }}>
                  {" "}[cited by {r.src_cited_by.toLocaleString()}]</span> : null}
                {g.rest.length > 0 && (
                  <div><a className="mini-link" style={{ fontSize: 12 }}
                    title="Show the other passages in this document that cite this authority"
                    onClick={() => setExpanded((s) => {
                      const n = new Set(s); n.has(g.key) ? n.delete(g.key) : n.add(g.key); return n;
                    })}>
                    {opened ? "▾ hide" : `▸ and ${g.rest.length} other place${g.rest.length === 1 ? "" : "s"}`}</a></div>
                )}</td>
              <td className="muted" style={{ whiteSpace: "nowrap" }}>{r.src_date ? String(r.src_date).slice(0, 4) : ""}</td>
            </tr>
            {opened && g.rest.map((o, j) => (
              <tr key={g.key + ":" + j} className="cited-by-passage">
                <td />
                <td style={{ paddingLeft: 18 }}>
                  {/* an extra passage belongs to the SAME citing document as the row it
                      sits under; jump to where that document says it (src_anchor), not to
                      the anchor of the passage being cited here. */}
                  <DocLink id={o.src_id || r.src_id} anchor={o.src_anchor || undefined}
                    title={`Open ${r.src_title || r.src_id} at this passage`}
                    onOpen={() => open(o.src_id || r.src_id, o.src_anchor || undefined)} className="muted">
                    {o.dst_anchor ? `→ ${o.dst_anchor}` : "→ another passage"}</DocLink>
                  {o.relationship_type !== r.relationship_type && (
                    <span className="muted" style={{ fontSize: 11 }}> · {treat(o.relationship_type)}</span>)}
                </td>
                <td />
              </tr>
            ))}
          </Fragment>
          );
        })}
      </tbody></table>
      {/* Collapsed to ten by default, like the other reference panels: 16,498 citing
          documents paged fifty at a time still filled the screen before you had decided
          you wanted them. Expanding restores the pager. */}
      {shown.length > 10 && (
        <a className="mini-link show-more" onClick={() => { setListOpen((v) => !v); setPage(0); }}
          title={listOpen ? "Collapse this list" : "Show every citing document, paged"}>
          {listOpen ? "▴ Show fewer" : `▾ Show all ${shown.length.toLocaleString()}`}</a>
      )}
      {listOpen && shown.length > PER && (
        <div className="row" style={{ justifyContent: "center", alignItems: "baseline", marginTop: 8 }}>
          <button className="mini" disabled={page === 0} onClick={() => setPage((p) => p - 1)}>‹ prev</button>
          <span className="muted" style={{ flex: "0 0 auto", fontSize: 12 }}>
            {page * PER + 1}–{Math.min((page + 1) * PER, shown.length)} of the top {shown.length}
            {count && count > shown.length ? ` (of ${count.toLocaleString()} total)` : ""}</span>
          <button className="mini" disabled={(page + 1) * PER >= shown.length} onClick={() => setPage((p) => p + 1)}>next ›</button>
        </div>
      )}
      {count != null && count > shown.length && incoming[0]?.dst_id && (
        <p className="muted" style={{ fontSize: 12, marginTop: 4 }}>
          Showing the {shown.length} most authoritative citers of {count.toLocaleString()} —{" "}
          <a className="mini-link" onClick={() => tray.push({ kind: "mentions",
            target: incoming[0].dst_id, label: "All citations to this decision" })}>
            see all, with the citing passages</a></p>
      )}
    </div>
  );
}

// What a mapping row asserts, in the reader's words. A repealed predecessor's citers are
// this provision's history; a companion instrument's are a parallel provision's, in force
// alongside — calling the latter "previous" would be simply untrue.
const MAPPING_KIND: Record<string, { row: string; heading: string; blurb: string }> = {
  functional_predecessor: {
    row: "earlier iteration",
    heading: "Previous, functionally similar iterations",
    blurb: "These authors cited the earlier law, not the current one. They are included as functional history and remain distinct from direct citations.",
  },
  equivalent: {
    row: "parallel provision",
    heading: "Parallel provisions in companion instruments",
    blurb: "These authors cited a provision of a companion instrument that is in force alongside this one — not an earlier iteration of it. They remain distinct from direct citations.",
  },
};
const mappingKind = (t?: string) => MAPPING_KIND[t || "functional_predecessor"] || MAPPING_KIND.functional_predecessor;

function InheritedProvisionMentions({ incoming, mappings, open }:
  { incoming: any[]; mappings: any[]; open: (id: string, a?: string) => void }) {
  const [anchor, setAnchor] = useState("");
  // The dropdown is how you get from "148 documents" to "the 45 that cited ECD Article 14,
  // shown against DSA Article 6" — so it names the pairing, not just the current anchor.
  const pairs = [...new Map(mappings.map((m: any) => [m.current_anchor, m])).values()] as any[];
  const shown = anchor
    ? incoming.filter((r: any) => r.inherited_current_anchor === anchor) : incoming;
  const kinds = [...new Set(shown.map((r: any) => r.mapping_type || "functional_predecessor"))];
  const kind = kinds.length === 1 ? mappingKind(kinds[0] as string) : null;
  const [visible, showMore] = useShowMore(shown);
  return <div className="panel">
    <select className="sort-select" style={{ float: "right" }} value={anchor}
      onChange={(e) => setAnchor(e.target.value)} aria-label="current provision filter">
      <option value="">all mapped provisions</option>
      {pairs.map((m: any) => <option key={m.current_anchor} value={m.current_anchor}>
        {m.current_anchor} ← {m.previous_anchor}</option>)}
    </select>
    <h3>{kind ? kind.heading : "Corresponding provisions in other instruments"} mentioned by <b>{shown.length}</b> document{shown.length === 1 ? "" : "s"}</h3>
    <p className="muted" style={{ fontSize: 12 }}>
      {kind ? kind.blurb : "These authors cited a corresponding provision of another instrument, not this one. Each row says what the mapping claims; all remain distinct from direct citations."}
    </p>
    {/* Two real columns, not "whatever the longest title leaves over": auto layout gave
        the citing document ~80% and squeezed the mapping — the shorter, more structured
        side — into a sliver that wrapped over five lines per row. */}
    <table className="two-col"><tbody>{visible.map((r: any) => <tr key={`${r.src_id}-${r.mapping_id}`}>
      <td><DocLink id={r.src_id} anchor={r.src_anchor}
        onOpen={() => open(r.src_id, r.src_anchor)}>{r.src_title || r.src_id}</DocLink></td>
      <td className="muted">{r.inherited_current_anchor} ←{" "}
        <DocLink id={r.inherited_from_id} anchor={r.inherited_from_anchor}
          onOpen={() => open(r.inherited_from_id, r.inherited_from_anchor)}>
          {r.inherited_from_title || r.inherited_from_id} {r.inherited_from_anchor}</DocLink>
        {!kind && <span className="tag" style={{ marginLeft: 6, fontSize: 10 }}>{mappingKind(r.mapping_type).row}</span>}</td>
    </tr>)}</tbody></table>
    {showMore}
  </div>;
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

// Discover NEW judgments that cite this document, via the live source (Find Case Law /
// CELLAR) — forward-citation discovery.
function FindCiting({ seed, onDone }: { seed: string; onDone: () => void }) {
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");
  async function inbound() {
    setBusy(true); setMsg("searching the source for cases citing this…");
    try {
      const r = await api.discoverCiting(seed);
      setMsg(r.error ? "error: " + r.error : `✓ found ${r.count} new case(s) citing this (via ${r.via})`); onDone();
    } catch (e: any) { setMsg("error: " + e); } finally { setBusy(false); }
  }
  return (
    <span style={{ flex: "0 0 auto", display: "inline-flex", alignItems: "center", gap: 4 }}>
      <button disabled={busy} onClick={inbound} title="Find NEW judgments that cite this, via Find Case Law / CELLAR">🔎 {busy ? "finding…" : "Find citing cases"}</button>
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
        {DOC_TYPES.map((t) => <option key={t} value={t}>{docTypeLabel(t)}</option>)}
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
  const { canWrite } = useAuth();  // readers see citations but cannot reclassify/re-point/reject
  const [repoint, setRepoint] = useState(false);
  const [dst, setDst] = useState("");
  async function correct(body: Record<string, unknown>) { await api.correctCitation({ relation_id: r.relation_id, ...body }); onDone(); }
  return (
    <tr>
      <td>
        {canWrite ? (
          <select value={r.relationship_type} title="reclassify treatment"
            onChange={(e) => correct({ treatment: e.target.value })}
            style={{ background: "transparent", border: "none", color: "inherit", cursor: "pointer" }}>
            {[...new Set([r.relationship_type, ...TREATMENTS])].map((t) => <option key={t}>{t}</option>)}
          </select>
        ) : <span>{r.relationship_type}</span>}
        {r.extracted_via === "manual" && <span className="muted" title="human-corrected"> ✎</span>}
      </td>
      <td>{r.dst_id ? <DocLink id={r.dst_id} onOpen={() => open(r.dst_id)}>{r.dst_id}</DocLink> : <span className="muted">{r.raw_citation_string}</span>}
        {r.dst_anchor && <span className="muted"> ◆ {r.dst_anchor}</span>}</td>
      <td className="muted">{r.resolution_status}</td>
      {canWrite && <td style={{ whiteSpace: "nowrap" }}>
        <a title="re-point to the correct document" style={{ cursor: "pointer" }} onClick={() => setRepoint((v) => !v)}>⤳</a>{" "}
        <a title="reject as a false positive" style={{ cursor: "pointer" }} onClick={() => correct({ suppress: true })}>✗</a>
        {repoint && <div className="row" style={{ marginTop: 4 }}>
          <input value={dst} onChange={(e) => setDst(e.target.value)} placeholder="correct stable_id" style={{ minWidth: 180 }} />
          <button style={{ flex: "0 0 auto" }} onClick={() => dst && correct({ dst_id: dst })}>set</button>
        </div>}
      </td>}
    </tr>
  );
}

// CourtListener quota, shown under the us-caselaw source row. US case law is the only
// source with a hard *daily* ceiling, so a stalled-looking queue is usually a spent
// budget rather than a fault — this row is what tells the two apart.
function UsBudgetRow({ budget }: { budget: UsCaselawBudget | null | undefined }) {
  if (!budget) return null;
  if (!budget.configured) {
    return (
      <tr><td colSpan={4} className="muted" style={{ fontSize: 12, paddingLeft: 12 }}>
        ↳ no API token — set <span className="kbd">RAGLEX_COURTLISTENER_TOKEN</span> in Settings to fetch US cases
      </td></tr>
    );
  }
  const wait = budget.retry_after_seconds;
  const waitLabel = wait >= 3600 ? `${Math.round(wait / 3600)}h` : wait >= 60 ? `${Math.round(wait / 60)}m` : `${wait}s`;
  return (
    <tr><td colSpan={4} style={{ fontSize: 12, paddingLeft: 12 }}>
      <span className="muted">↳ quota </span>
      {Object.entries(budget.windows).map(([name, w]) => (
        // limit === null means this window doesn't bind for the account — show the
        // usage without a denominator rather than a ratio against a sentinel
        <span className="tag" key={name}
          title={w.limit === null
            ? `${w.used} requests in the rolling ${name}; no ${name} limit set`
            : `${w.used} of ${w.limit} requests used in the rolling ${name}`}>
          {name}: {w.limit === null ? `${w.used} · uncapped` : `${w.used}/${w.limit}`}
        </span>
      ))}
      {budget.tier === "custom" && <span className="tag" title="raised limits set in Settings">membership</span>}
      {budget.allowed_now
        ? <span className="ok"> ready</span>
        : <span className="err"> {budget.blocked_by} limit reached — resumes in {waitLabel}</span>}
      {budget.pending_us_references > 0 && (
        <span className="muted">
          {" · "}{budget.pending_us_references.toLocaleString()} US citations queued
          {budget.estimated_days_to_clear !== null && budget.estimated_days_to_clear > 0 &&
            <> · ~{budget.estimated_days_to_clear}d to clear at {Math.round(budget.queue_reserve * 100)}% of quota</>}
        </span>
      )}
    </td></tr>
  );
}

// CanLII quota, shown under the Canadian case-law source row. Same idea as the
// CourtListener row: the API is metered (a persisted ledger below CanLII's ceiling),
// so a stalled-looking Canadian queue is usually a spent budget. Metadata-only —
// the backlogs are citations to resolve into stubs + held docs awaiting enrichment.
function CanliiBudgetRow({ budget }: { budget: CanliiBudget | null | undefined }) {
  if (!budget) return null;
  if (!budget.configured) {
    return (
      <tr><td colSpan={4} className="muted" style={{ fontSize: 12, paddingLeft: 12 }}>
        ↳ no CanLII key — set <span className="kbd">RAGLEX_CANLII_API_KEY</span> in Settings to resolve + enrich Canadian cases
      </td></tr>
    );
  }
  const wait = budget.retry_after_seconds;
  const waitLabel = wait >= 3600 ? `${Math.round(wait / 3600)}h` : wait >= 60 ? `${Math.round(wait / 60)}m` : `${wait}s`;
  const backlog = budget.pending_ca_references + budget.unenriched_documents;
  return (
    <tr><td colSpan={4} style={{ fontSize: 12, paddingLeft: 12 }}>
      <span className="muted">↳ CanLII quota </span>
      {Object.entries(budget.windows).map(([name, w]) => (
        <span className="tag" key={name}
          title={w.limit === null
            ? `${w.used} requests in the rolling ${name}; no ${name} limit set`
            : `${w.used} of ${w.limit} requests used in the rolling ${name}`}>
          {name}: {w.limit === null ? `${w.used} · uncapped` : `${w.used}/${w.limit}`}
        </span>
      ))}
      {budget.tier === "custom" && <span className="tag" title="raised limits set in Settings">custom limits</span>}
      {budget.allowed_now
        ? <span className="ok"> ready</span>
        : <span className="err"> {budget.blocked_by} limit reached — resumes in {waitLabel}</span>}
      {backlog > 0 && (
        <span className="muted">
          {" · "}{budget.pending_ca_references.toLocaleString()} CA citations queued
          {" · "}{budget.unenriched_documents.toLocaleString()} held docs to enrich
          {budget.estimated_days_to_clear !== null && budget.estimated_days_to_clear > 0 &&
            <> · ~{budget.estimated_days_to_clear}d to clear</>}
        </span>
      )}
    </td></tr>
  );
}

// --- Dashboard -------------------------------------------------------------
export function Dashboard({ open: _open, navigate }: { open: (id: string) => void; navigate?: (f: Record<string, string>) => void }) {
  const [sources, , reloadSources] = useAsync(() => api.sources(), []);
  // Its own call, not a field on /sources: counting the pending US backlog is a scan,
  // and /sources is polled on every dashboard refresh.
  const [usBudget, , reloadUsBudget] = useAsync(() => api.usCaselawBudget(), []);
  const [caBudget, , reloadCaBudget] = useAsync(() => api.canliiBudget(), []);
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

  const refresh = () => { reloadSources(); reloadUsBudget(); reloadCaBudget(); reloadQueues(); reloadAlerts(); reloadStats(); reloadWork(); reloadBacklog(); };
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
              <Fragment key={s.key}>
                <tr><td>{s.key}</td><td>{s.documents}</td>
                  <td className={s.consecutive_failures ? "err" : ""}>{s.consecutive_failures}</td>
                  <td className="muted">{s.last_yield_at?.slice(0, 10) || "—"}</td></tr>
                {s.key === "us-caselaw" && <UsBudgetRow budget={usBudget} />}
                {/* attach the CanLII quota to the first Canadian case-law row present */}
                {(s.key === "ca-caselaw" ||
                  (s.key === "ca-canlii" && !(sources ?? []).some((x) => x.key === "ca-caselaw")))
                  && <CanliiBudgetRow budget={caBudget} />}
              </Fragment>
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
      {stats && stats.total == null && (
        <div className="panel"><h3 className="loading-pulse muted">Corpus · computing totals…</h3></div>
      )}
      {stats && stats.total != null && (
        <div className="panel">
          <h3>Corpus · {stats.total.toLocaleString()} documents · resolution {Math.round((stats.resolution?.coverage || 0) * 100)}%</h3>
          <div>{Object.entries(stats.by_doc_type || {}).map(([k, v]: any) =>
            <span className="tag" key={k}>{navigate ? <a onClick={() => navigate({ doc_type: k })} title="browse in Search">{docTypeLabel(k)}: {v}</a> : <>{docTypeLabel(k)}: {v}</>}</span>)}</div>
          <div>{Object.entries(stats.by_source || {}).map(([k, v]: any) =>
            <span className="tag" key={k}>{navigate ? <a onClick={() => navigate({ source: k })} title="browse in Search">{k}: {v}</a> : <>{k}: {v}</>}</span>)}</div>
          <div>{Object.entries(stats.by_tag || {}).map(([k, v]: any) =>
            <span className="tag" key={k}>{navigate ? <a onClick={() => navigate({ tag: k })} title="browse in Search">#{k}: {v}</a> : <>#{k}: {v}</>}</span>)}</div>
        </div>
      )}
    </div>
  );
}


export function ImportView({ open }: { open?: (id: string) => void }) {
  const [msg, setMsg] = useState("");
  const show = (r: any) => setMsg(typeof r === "string" ? r : JSON.stringify(r));
  return (
    <div>
      <StandaloneImportPanel open={open} />
      <LegislationAknPanel open={open} />
      <CaseLawImportPanel />
      <LiiWorklistPanel />
      <ZoteroPanel show={show} />
      <GuidanceRulesPanel />
      {msg && <div className="panel"><pre style={{ whiteSpace: "pre-wrap", wordBreak: "break-all" }}>{msg}</pre></div>}
    </div>
  );
}

// Drop a set of files, label each one, import the lot.
//
// The labelling is the point. A document's JURISDICTION in particular is not
// cosmetic: it decides which citation grammars are trusted inside it (an Irish
// decision's "the 2018 Act" must not bind to the UK statute of that name; a single-
// letter reporter series is a citation in US material and page notation elsewhere), so
// a hand-uploaded document declares it and is thereafter treated exactly as a harvested
// document of that jurisdiction is. The rest — court, date, citation, tags — is the
// metadata every facet, filter and export in the app already reads.
// `tags` is a comma-separated string while it is being typed, and only becomes the
// array the API wants at submit — a half-typed "seminar, read" is not two tags yet.
type ImportRow = Omit<ImportItem, "tags"> & {
  file: File; expanded?: boolean; tags?: string;
};

const importRowFor = (file: File): ImportRow => ({
  file,
  // The filename is the operator's own name for the thing far more often than not;
  // strip the extension and let them correct it.
  title: file.name.replace(/\.[a-z0-9]{1,5}$/i, ""),
  doc_type: "commentary",
  jurisdiction: "",
  structure: "auto",
});

function StandaloneImportPanel({ open }: { open?: (id: string) => void }) {
  const [opts, setOpts] = useState<ImportOptions | null>(null);
  const [rows, setRows] = useState<ImportRow[]>([]);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<ImportBatchResult | null>(null);
  const [err, setErr] = useState("");
  const fileInput = useRef<HTMLInputElement>(null);

  useEffect(() => { api.importOptions().then(setOpts).catch(() => setOpts(null)); }, []);

  const patch = (i: number, change: Partial<ImportRow>) =>
    setRows((rs) => rs.map((r, n) => (n === i ? { ...r, ...change } : r)));
  // "Set for every file" — the reason a drop of twenty is quicker than twenty drops.
  const patchAll = (change: Partial<ImportRow>) =>
    setRows((rs) => rs.map((r) => ({ ...r, ...change })));

  const addFiles = (list: FileList | null) => {
    const picked = Array.from(list ?? []);
    if (picked.length) setRows((rs) => [...rs, ...picked.map(importRowFor)]);
    setResult(null); setErr("");
    if (fileInput.current) fileInput.current.value = "";   // so the same file can be re-picked
  };

  const jurisdictionLabel = (code?: string) =>
    opts?.jurisdictions.find((j) => j.code === code)?.label || "";
  const courtsFor = (code?: string) =>
    opts?.courts_by_jurisdiction[jurisdictionLabel(code)] || [];

  const go = async () => {
    if (!rows.length) { setErr("choose one or more files"); return; }
    setBusy(true); setErr(""); setResult(null);
    try {
      setResult(await api.importFiles(
        rows.map((r) => r.file),
        rows.map(({ file: _file, expanded: _expanded, tags, ...item }) => ({
          ...item,
          tags: (tags ?? "").split(",").map((t) => t.trim()).filter(Boolean),
        })),
      ));
      setRows([]);
    } catch (e: any) { setErr(String(e)); }
    finally { setBusy(false); }
  };

  return (
    <div className="panel">
      <h3>Import standalone documents</h3>
      <p className="muted">
        Material that stands on its own — a judgment or statute no adapter can reach, a
        regulator’s PDF, an article, a set of seminar readings. Drop several at once and
        label each below. To attach material to a <i>specific</i> case or law section
        instead, open it in Search/Corpus and use its “Augment” panel.
      </p>
      <div className="row">
        <input ref={fileInput} type="file" multiple
          onChange={(e) => addFiles(e.target.files)} />
        {rows.length > 0 && (
          <button style={{ flex: "0 0 auto" }} onClick={() => { setRows([]); setResult(null); }}>
            Clear {rows.length} file{rows.length > 1 ? "s" : ""}
          </button>
        )}
      </div>

      {rows.length > 1 && opts && (
        <div className="row import-all" style={{ marginTop: 10, alignItems: "center" }}>
          <span className="muted" style={{ flex: "0 0 auto" }}>Set for every file:</span>
          <select defaultValue=""
            onChange={(e) => { patchAll({ doc_type: e.target.value }); e.target.value = ""; }}>
            <option value="" disabled>type…</option>
            {opts.doc_types.map((t) => <option key={t} value={t}>{docTypeLabel(t)}</option>)}
          </select>
          <select defaultValue=""
            onChange={(e) => { patchAll({ jurisdiction: e.target.value, court: "" }); e.target.value = ""; }}>
            <option value="" disabled>jurisdiction…</option>
            {opts.jurisdictions.map((j) => <option key={j.code} value={j.code}>{j.label}</option>)}
          </select>
          <select defaultValue=""
            onChange={(e) => { patchAll({ structure: e.target.value }); e.target.value = ""; }}>
            <option value="" disabled>structure…</option>
            {opts.structures.map((s) => <option key={s.value} value={s.value}>{s.label}</option>)}
          </select>
        </div>
      )}

      {rows.length > 0 && (
        <div style={{ overflowX: "auto", marginTop: 10 }}>
          <table className="import-table">
            <colgroup>
              <col style={{ width: "9rem" }} /><col style={{ width: "14rem" }} />
              <col style={{ width: "10.5rem" }} /><col style={{ width: "10rem" }} />
              <col style={{ width: "10rem" }} /><col style={{ width: "9rem" }} />
              <col style={{ width: "11rem" }} /><col style={{ width: "5rem" }} />
            </colgroup>
            <thead><tr>
            <th>File</th><th>Name</th><th>Type</th><th>Jurisdiction</th>
            <th>Court / body</th><th>Date</th><th>Structure</th><th />
          </tr></thead><tbody>
            {rows.map((r, i) => (
              <Fragment key={i}>
                <tr>
                  <td className="muted import-file"
                    title={`${r.file.name} · ${Math.max(1, Math.round(r.file.size / 1024)).toLocaleString()} kB`}>
                    {r.file.name}
                  </td>
                  <td><input value={r.title ?? ""} style={{ minWidth: 180 }}
                    onChange={(e) => patch(i, { title: e.target.value })} /></td>
                  <td>
                    <select value={r.doc_type} onChange={(e) => patch(i, { doc_type: e.target.value })}>
                      {(opts?.doc_types ?? DOC_TYPES).map((t) =>
                        <option key={t} value={t}>{docTypeLabel(t)}</option>)}
                    </select>
                  </td>
                  <td>
                    <select value={r.jurisdiction ?? ""}
                      onChange={(e) => patch(i, { jurisdiction: e.target.value, court: "" })}>
                      <option value="">— unplaced —</option>
                      {(opts?.jurisdictions ?? []).map((j) =>
                        <option key={j.code} value={j.code}>{j.label}</option>)}
                    </select>
                  </td>
                  <td>
                    {/* a datalist, not a select: the picker offers the courts the corpus
                        holds, but a body it has never seen must still be typeable */}
                    <input list={`courts-${i}`} value={r.court ?? ""} style={{ minWidth: 140 }}
                      placeholder={r.jurisdiction ? "court / regulator" : ""}
                      onChange={(e) => patch(i, { court: e.target.value })} />
                    <datalist id={`courts-${i}`}>
                      {courtsFor(r.jurisdiction).map((c) =>
                        <option key={c.court} value={c.court}>{c.label}</option>)}
                    </datalist>
                  </td>
                  <td><input type="date" value={r.decision_date ?? ""} style={{ minWidth: 130 }}
                    onChange={(e) => patch(i, { decision_date: e.target.value })} /></td>
                  <td>
                    <select value={r.structure} onChange={(e) => patch(i, { structure: e.target.value })}
                      title="How to find this document's own citable units. Best effort — it falls back to pages.">
                      {(opts?.structures ?? [{ value: "auto", label: "Best effort" }]).map((s) =>
                        <option key={s.value} value={s.value}>{s.label}</option>)}
                    </select>
                  </td>
                  <td>
                    <button title="citation, language, tags, and what this document is about"
                      onClick={() => patch(i, { expanded: !r.expanded })}>
                      {r.expanded ? "less" : "more…"}</button>
                  </td>
                </tr>
                {r.expanded && (
                  <tr className="import-more"><td /><td colSpan={7}>
                    <div className="row">
                      <label style={{ flex: "1 1 15rem" }}>Its own citation
                        <input value={r.citation ?? ""} placeholder="[2024] UKSC 12 · ECLI:EU:C:2020:559"
                          title="Registered as an alias, so references to this citation elsewhere in the corpus resolve onto this document."
                          onChange={(e) => patch(i, { citation: e.target.value })} /></label>
                      <label style={{ flex: "0 1 9rem" }}>Language
                        <input list="import-languages" value={r.language ?? ""} placeholder="en"
                          onChange={(e) => patch(i, { language: e.target.value })} /></label>
                      <label style={{ flex: "1 1 12rem" }}>Tags
                        <input list="import-tags" value={r.tags ?? ""}
                          placeholder="comma, separated"
                          onChange={(e) => patch(i, { tags: e.target.value })} /></label>
                    </div>
                    <div className="row">
                      <label style={{ flex: "2 1 18rem" }}>About (stable_id of a case / law section)
                        <input value={r.link_to ?? ""} placeholder="ECLI:EU:C:2020:559"
                          onChange={(e) => patch(i, { link_to: e.target.value })} /></label>
                      <label style={{ flex: "1 1 10rem" }}>Relationship
                        <select value={r.relationship ?? ""} disabled={!r.link_to}
                          onChange={(e) => patch(i, { relationship: e.target.value })}>
                          <option value="">default for its type</option>
                          {(opts?.relationships ?? REL_TYPES).map((t) =>
                            <option key={t} value={t}>{relationLabel(t)}</option>)}
                        </select></label>
                    </div>
                  </td></tr>
                )}
              </Fragment>
            ))}
          </tbody></table>
          <datalist id="import-languages">
            {(opts?.languages ?? []).map((l) => <option key={l} value={l} />)}
          </datalist>
          <datalist id="import-tags">
            {(opts?.tags ?? []).map((t) => <option key={t} value={t} />)}
          </datalist>
        </div>
      )}

      <p style={{ marginTop: 10 }}>
        <button className="primary" disabled={busy || !rows.length} onClick={go}>
          {busy ? "importing…" : `Import ${rows.length || ""} ${rows.length === 1 ? "document" : "documents"}`}
        </button>
        {rows.length > 0 && <span className="muted" style={{ marginLeft: 10 }}>
          A jurisdiction is worth setting: it decides which citation grammars are trusted
          inside the document.
        </span>}
      </p>
      {err && <p className="err">{err}</p>}
      {result && (
        <div>
          <p className={result.failed ? "err" : "ok"}>
            ✓ imported {result.imported}{result.failed ? ` · ${result.failed} failed` : ""}
            {result.next && !result.failed ? ` — ${result.next}` : ""}
          </p>
          <table><thead><tr>
            <th>Document</th><th>Held as</th><th>Text</th><th>Units</th>
          </tr></thead><tbody>
            {result.documents.map((d) => (
              <tr key={d.index}>
                <td>{d.error ? (d.title || d.filename) : (
                  open && d.stable_id
                    ? <DocLink id={d.stable_id} onOpen={() => open(d.stable_id!)}>{d.title || d.stable_id}</DocLink>
                    : (d.title || d.stable_id))}
                  {d.error && <div className="err">{d.error}</div>}</td>
                <td className="muted">{d.error ? "—" : `${docTypeLabel(d.doc_type)} · ${d.source}`}</td>
                <td className="muted">{d.error ? "—"
                  : d.needs_ocr ? <span className="err">no text layer — needs OCR</span>
                  : `${(d.chars ?? 0).toLocaleString()} chars`}</td>
                <td className="muted">{d.error ? "—" : `${d.segments ?? 0} (${d.structure})`}</td>
              </tr>
            ))}
          </tbody></table>
        </div>
      )}
    </div>
  );
}

// Import a hand-supplied Akoma Ntoso legislation file — for an act legislation.gov.uk
// won't serve (ukpga/2006/46 was absent), where you have the .akn/.xml. It gets the same
// structural parse as a live harvest: schedules, unapplied-effects edges, pinpoints. The
// id defaults to the AKN's own FRBRWork, so usually you just pick the file and go.
function LegislationAknPanel({ open }: { open?: (id: string) => void }) {
  const [file, setFile] = useState<File | null>(null);
  const [sid, setSid] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<any>(null);
  const go = async () => {
    if (!file) { setMsg({ error: "choose an .akn / .xml file" }); return; }
    setBusy(true); setMsg(null);
    try { setMsg(await api.importLegislationAkn(file, sid.trim() || undefined)); }
    catch (e: any) { setMsg({ error: String(e) }); }
    finally { setBusy(false); }
  };
  return (
    <div className="panel">
      <h3>Import legislation from an Akoma Ntoso file <span className="muted">— for an act legislation.gov.uk won’t serve</span></h3>
      <p className="muted">
        Drop the <b>.akn</b> (or <b>.xml</b>) file. It’s parsed exactly as a live harvest —
        sections, schedules (as <i>sch 1 para 1</i>), and amendment edges — and keyed under its
        own legislation id. Leave the id blank to take it from the file’s FRBRWork.
      </p>
      <div className="row">
        <input type="file" accept=".akn,.xml" onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
        <input value={sid} onChange={(e) => setSid(e.target.value)} placeholder="id (optional) — e.g. ukpga/2006/46" />
        <button className="primary" disabled={busy} style={{ flex: "0 0 auto" }} onClick={go}>
          {busy ? "importing…" : "Import"}</button>
      </div>
      {msg && (msg.error
        ? <p className="err" style={{ marginTop: 8 }}>{msg.error}</p>
        : <p className="ok" style={{ marginTop: 8 }}>✓ imported <b>{msg.title || msg.stable_id}</b> — {msg.segments} segments, {msg.resolved_edges} edges resolved{" "}
            {open && msg.stable_id && <DocLink id={msg.stable_id} onOpen={() => open(msg.stable_id)}>open ↗</DocLink>}</p>)}
    </div>
  );
}

// The LII fetch worklist: cases the corpus cites or lists but cannot show, paired with a
// constructed link to the institute that publishes each one. Deliberately a *manual*
// round-trip — you work down the list in a browser and save the pages — because these are
// small charity-run services that shouldn't be crawled, and because a real browser session
// is what gets past their bot-walls anyway. Saving each page under the `filename` column
// is what lets the importer recover a document's identity from the filename alone.
function LiiWorklistPanel() {
  const [scope, setScope] = useState<LIIScope>("unheld");
  const [rows, setRows] = useState<LIITarget[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const load = async (s: LIIScope) => {
    setBusy(true); setErr("");
    try { setRows((await api.liiLinkTargets(s, 200)).links); }
    catch (e: any) { setErr(String(e)); setRows(null); }
    finally { setBusy(false); }
  };
  return (
    <div className="panel">
      <h3>Missing full text — LII links</h3>
      <p className="muted">
        Cases the corpus cites but can’t display, with a link to the institute that publishes
        each. URLs are constructed locally from the citation — nothing is fetched from the
        LIIs on your behalf. Work down the list in a browser, saving each page under the
        <b> filename</b> shown, then import that folder.
      </p>
      <div className="row">
        <select value={scope} onChange={(e) => { const s = e.target.value as LIIScope; setScope(s); setRows(null); }}>
          <option value="unheld">Cited but not held</option>
          <option value="textless">Held, but no full text</option>
          <option value="both">Both</option>
        </select>
        <button onClick={() => load(scope)} disabled={busy} style={{ flex: "0 0 auto" }}>
          {busy ? "Loading…" : "Preview (top 200)"}
        </button>
        <button className="primary" style={{ flex: "0 0 auto" }}
          onClick={async () => {
            setErr("");
            try { await api.downloadLiiLinksCsv(scope); } catch (e: any) { setErr(String(e)); }
          }}>⭳ Download full list (CSV)</button>
      </div>
      {err && <p className="err">{err}</p>}
      {rows && rows.length === 0 && <p className="muted">Nothing to fetch for this scope.</p>}
      {rows && rows.length > 0 && (
        <>
          <p className="muted">Most-cited first — the cases the corpus leans on hardest.</p>
          <table className="lii-table"><thead><tr>
            <th>Citation</th><th>Cited by</th><th>Site</th><th>Link</th><th>Save as</th>
          </tr></thead><tbody>
            {rows.map((r, i) => (
              <tr key={i}>
                <td>{r.citation || r.stable_id}{r.title && <div className="muted">{r.title}</div>}</td>
                <td>{r.citing_count}</td>
                <td>{r.site_name}
                  {r.certainty === "probable" && <span className="lii-tag lii-probable" title={LII_CERTAINTY.probable}>probable</span>}</td>
                <td><a href={r.url} target="_blank" rel="noopener noreferrer">open ↗</a></td>
                <td><code>{r.filename}</code></td>
              </tr>
            ))}
          </tbody></table>
        </>
      )}
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
  const CASE_RE = /\.(html?|rtf|doc|zip)$/i;

  // Folder / multi-file / multi-zip upload. The browser hands us every picked file; we keep
  // the .html/.rtf/.doc (and .zip — unpacked server-side), stage them in batches (so no
  // single request is huge), then start ONE background import job that routes each by
  // extension. Uploading 11 Westlaw zips is thus a single job with a single post-import
  // roll-up, not 11 jobs each triggering a corpus-wide rebuild.
  async function uploadFiles(fileList: FileList) {
    const files = Array.from(fileList).filter((f) => CASE_RE.test(f.name));
    if (!files.length) { setMsg("no .html, .rtf, .doc or .zip files in that selection"); return; }
    const zips = files.filter((f) => /\.zip$/i.test(f.name)).length;
    const html = files.filter((f) => /\.html?$/i.test(f.name)).length;
    const rtf = files.length - html - zips;
    setBusy(true); setProg({ done: 0, total: files.length });
    const uploadId = (crypto.randomUUID?.() || Math.random().toString(36).slice(2)).replace(/-/g, "").slice(0, 24);
    // zips are large → few per request; loose files are small → many per request
    const BATCH = zips ? 3 : 200;
    try {
      for (let i = 0; i < files.length; i += BATCH) {
        const r = await api.importCaselawFilesBatch(uploadId, files.slice(i, i + BATCH));
        if (r.error) throw new Error(r.error);
        setProg({ done: Math.min(i + BATCH, files.length), total: files.length });
      }
      const kinds = [zips && `${zips} zip${zips > 1 ? "s" : ""}`, html && `${html} BAILII`,
        rtf && `${rtf} Westlaw`].filter(Boolean).join(" + ");
      setMsg(`staged ${files.length} item${files.length > 1 ? "s" : ""} (${kinds}) — starting one import job…`);
      const j = await api.importCaselawFilesStart(uploadId);
      setMsg(j.error ? "error: " + j.error : `✓ queued as ONE job ${j.job_id} (${files.length} item${files.length > 1 ? "s" : ""}) — watch the Jobs panel`);
    } catch (err: any) { setMsg("error: " + (err.message || err)); }
    finally { setBusy(false); setProg(null); }
  }

  return (
    <div className="panel">
      <h3>Case law &amp; legislation (folder, files, or one-or-many zips of BAILII .html + Westlaw .rtf/.doc)</h3>
      <p className="muted" style={{ fontSize: 13 }}>
        Pick a whole folder — no zipping needed — or select several zips at once (they import
        as a single job). Saved BAILII case pages
        (<code>.html</code>) and Westlaw exports (<code>.rtf</code>/<code>.doc</code>) can be mixed freely; each
        file is routed to its own parser. BAILII pages key by neutral citation and the “Cite as:” list;
        Westlaw <i>judgments</i> key by neutral citation → ECLI → Westlaw id (parties, court, judges, counsel
        and every parallel report citation extracted and aliased); Westlaw <i>legislation</i> keys by its
        legislation.gov.uk id (<code>ukpga/1889/63</code>), section-segmented so statute pinpoints resolve —
        the way to hold old Acts legislation.gov.uk only has as a scanned PDF. Runs in the background —
        watch the Jobs panel.
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
        <input type="file" multiple accept=".html,.htm,.rtf,.doc" disabled={busy}
          onChange={(e) => { if (e.target.files?.length) uploadFiles(e.target.files); e.currentTarget.value = ""; }} />
        <span className="muted" style={{ fontSize: 12 }}>or zip(s):</span>
        {/* multiple: select many Westlaw/BAILII zips at once → staged + imported as ONE job */}
        <input type="file" accept=".zip" multiple disabled={busy}
          onChange={(e) => { if (e.target.files?.length) uploadFiles(e.target.files); e.currentTarget.value = ""; }} />
      </div>
      {prog && <p className="muted" style={{ fontSize: 12 }}>uploading {prog.done}/{prog.total} files…</p>}
      {msg && <p className={msg.startsWith("error") ? "err" : "ok"} style={{ fontSize: 12 }}>{msg}</p>}
    </div>
  );
}

// --- Settings --------------------------------------------------------------
// --- Static exports: a set of statutes published as one small website ------
// Each row is one statute → one file (the operator's own filename, so links can be
// written by hand: gdpr.html). The shared preamble sits beneath every title; a row's own
// line sits directly under that, for this statute only. Building reads thousands of
// source texts per statute, so it runs as a job — one that skips the queue, because
// someone is waiting at the browser for the download.
const BUNDLE_PLACEHOLDERS = "<dateexported> · <datetimeexported> · <yearexported> · <count>";
const WEBHOOK_PLACEHOLDERS = "{documents} · {output_dir} · {bytes} · {titles} · {finished_at} · {zip}";

/** "Name: value" per line → the header map the API stores. Lines without a colon are
 *  still being typed, so they are simply not sent yet. */
function parseHeaders(text: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const line of (text || "").split("\n")) {
    const at = line.indexOf(":");
    if (at > 0 && line.slice(0, at).trim()) out[line.slice(0, at).trim()] = line.slice(at + 1).trim();
  }
  return out;
}

// The whole publishing surface, on its own admin section: the set, the schedule that
// republishes it, and the one request fired when a run lands (so a machine elsewhere can
// scp the folder on, or a phone can be told it happened).
export function StaticExportView() {
  const [settings, setSettings] = useState<Setting[]>([]);
  const load = () => api.getSettings().then((r) => setSettings(r.settings)).catch(() => { });
  useEffect(() => { load(); }, []);
  return (
    <StaticExportsPanel
      attribution={settings.find((s) => s.key === "RAGLEX_STATIC_EXPORT_ATTRIBUTION")}
      onSavedSettings={load} />
  );
}

function StaticExportsPanel({ attribution, onSavedSettings }:
  { attribution?: Setting; onSavedSettings: () => void }) {
  const [cfg, setCfg] = useState<StaticBundle | null>(null);
  const [attrib, setAttrib] = useState<string | null>(null);
  const [hookMsg, setHookMsg] = useState("");
  const [headerDraft, setHeaderDraft] = useState<string | null>(null);
  const [dirty, setDirty] = useState(false);
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);
  const [prog, setProg] = useState<{ message: string; fraction: number } | null>(null);
  const [adding, setAdding] = useState(false);
  const load = () => api.bundleConfig().then(setCfg).catch((e) => setMsg("error: " + e.message));
  useEffect(() => { load(); }, []);
  const items = cfg?.items || [];
  const patch = (next: Partial<StaticBundle>) => { setCfg({ ...(cfg as StaticBundle), ...next }); setDirty(true); };
  const setItem = (i: number, next: Partial<StaticBundleItem>) =>
    patch({ items: items.map((it, n) => (n === i ? { ...it, ...next } : it)) });
  const move = (i: number, delta: number) => {
    const to = i + delta;
    if (to < 0 || to >= items.length) return;
    const next = items.slice();
    [next[i], next[to]] = [next[to], next[i]];
    patch({ items: next });
  };

  const save = async (): Promise<boolean> => {
    if (!cfg) return false;
    setMsg("");
    try {
      const saved = await api.saveBundleConfig({
        items: cfg.items, index_title: cfg.index_title, index_text: cfg.index_text,
        max_snippets: cfg.max_snippets, output_dir: cfg.output_dir,
        index_wordart: cfg.index_wordart, webhook: cfg.webhook,
      });
      if (attrib !== null && attribution && attribution.source !== "env") {
        await api.saveSettings({ RAGLEX_STATIC_EXPORT_ATTRIBUTION: attrib });
        onSavedSettings();
      }
      setCfg(saved); setDirty(false); setMsg("Saved.");
      return true;
    } catch (e: any) { setMsg("error: " + e.message); return false; }
  };

  const build = async (opts: { zip: boolean; refresh: boolean }) => {
    if (!items.length) { setMsg("error: add at least one statute first"); return; }
    setBusy(true); setMsg(""); setProg({ message: "Starting the export…", fraction: 0 });
    try {
      // Export what's on screen: unsaved edits are saved first, and a failure to save
      // stops the build rather than quietly exporting the previous set.
      if (dirty && !(await save())) { setBusy(false); setProg(null); return; }
      const r = await api.buildBundle(opts, setProg);
      setMsg(`✓ ${r.documents} editions${opts.zip ? " downloaded and" : ""} written to ${r.output_dir}`);
      load();
    } catch (e: any) { setMsg("error: " + e.message); }
    finally { setBusy(false); setProg(null); }
  };

  const setSchedule = async (body: Record<string, unknown>) => {
    try { await api.setScheduledTask({ name: "static-bundle", ...body }); load(); }
    catch (e: any) { setMsg("error: " + e.message); }
  };

  if (!cfg) return <div className="panel"><p className="muted loading-pulse">Loading static exports…</p></div>;
  const run = cfg.last_run || {};
  const sched = cfg.schedule;
  const hook = cfg.webhook || { enabled: false, url: "", method: "POST", headers: {}, body: "" };
  const setHook = (next: Partial<StaticBundleWebhook>) =>
    patch({ webhook: { ...hook, ...next } as StaticBundleWebhook });
  // The textarea keeps its own draft: a half-typed line has no colon yet, and parsing on
  // every keystroke would delete it from under the cursor.
  const headerText = headerDraft ?? Object.entries(hook.headers || {})
    .map(([k, v]) => `${k}: ${v}`).join("\n");
  return (
    <div className="panel">
      <h3 style={{ marginTop: 0 }}>Static exports <span className="muted">— publish a set of statutes as one small website</span></h3>
      <p className="muted" style={{ fontSize: 13 }}>
        Each statute below is built into <b>one self-contained HTML file</b> — its text, everything in the corpus that cites it,
        excerpts and public source links — named as you choose, plus an <span className="kbd">index.html</span> linking them all.
        Every file sits at one level, so the links work from a folder on disk, a static host, or an unzipped download.
      </p>

      <label>Shared text beneath every statute's title <span className="kbd">RAGLEX_STATIC_EXPORT_ATTRIBUTION</span>
        {attribution?.source === "env" && <span className="muted"> · set via environment (overrides file)</span>}</label>
      <textarea rows={4} disabled={!attribution || attribution.source === "env"}
        placeholder={attribution?.placeholder}
        value={attrib ?? (attribution?.source === "file" ? attribution.display : "")}
        onChange={(e) => { setAttrib(e.target.value); setDirty(true); }} />
      <p className="muted" style={{ fontSize: 11, marginTop: 2 }}>
        Simple HTML only: <span className="kbd">&lt;a href&gt;</span> <span className="kbd">&lt;b&gt;</span> <span className="kbd">&lt;i&gt;</span> <span className="kbd">&lt;u&gt;</span> <span className="kbd">&lt;br&gt;</span>.
        Applied at download time, so editing it never means rebuilding anything.
      </p>

      <div className="row" style={{ gap: 10, alignItems: "flex-start", marginTop: 12, flexWrap: "wrap" }}>
        <label style={{ flex: "1 1 220px" }}>Index page title
          <input value={cfg.index_title} onChange={(e) => patch({ index_title: e.target.value })} /></label>
        <label style={{ flex: "0 0 150px" }} title="Excerpts kept per citing document in every edition. More excerpts make bigger files.">Excerpts per document
          <input type="number" min={1} max={12} value={cfg.max_snippets}
            onChange={(e) => patch({ max_snippets: Math.max(1, Math.min(12, +e.target.value || 4)) })} /></label>
      </div>
      <label style={{ display: "flex", gap: 6, alignItems: "center", fontSize: 13, marginTop: 4 }}
        title="Renders the index title as nostalgic rainbow WordArt, with a drop shadow. The index page only — inside an edition the title is the name of a legal instrument.">
        <input type="checkbox" checked={!!cfg.index_wordart}
          onChange={(e) => patch({ index_wordart: e.target.checked })} />
        <span>Index title as <span style={{
          background: "linear-gradient(to right,#b306a9,#ef2667,#f42e2c,#ffa509,#55ac2f,#0b13fd)",
          WebkitBackgroundClip: "text", backgroundClip: "text", color: "transparent",
          fontWeight: 700, fontFamily: "Arial, sans-serif",
        }}>WordArt</span> <span className="muted">— rainbow, with a shadow; index page only</span></span>
      </label>

      <label>Index page text</label>
      <textarea rows={3} value={cfg.index_text} onChange={(e) => patch({ index_text: e.target.value })}
        placeholder="Shown under the index title. Same simple HTML." />
      <p className="muted" style={{ fontSize: 11, marginTop: 2 }}>
        Placeholders (here and in any statute's line): <span className="kbd">{BUNDLE_PLACEHOLDERS}</span> —
        e.g. <span className="kbd">&lt;dateexported&gt;</span> becomes <b>{new Date().toLocaleDateString("en-GB", { day: "numeric", month: "long", year: "numeric" })}</b>.
        Every entry in the index also states its own export date.
      </p>

      <table className="grid break-cells" style={{ marginTop: 12 }}>
        <thead><tr>
          <th style={{ width: "30%" }}>statute</th><th style={{ width: "15%" }}>saves as</th>
          <th style={{ width: "12%" }}>short name</th>
          <th>its own line, under the shared text</th><th style={{ width: 78 }}></th>
        </tr></thead>
        <tbody>
          {items.map((it, i) => (
            <tr key={`${it.stable_id}-${i}`}>
              <td>
                <b>{it.title || it.stable_id}</b>
                <div className="muted" style={{ fontSize: 11 }}>{it.stable_id}</div>
              </td>
              <td>
                <div className="row" style={{ gap: 2, alignItems: "center", flexWrap: "nowrap" }}>
                  <input value={it.slug} placeholder="gdpr" style={{ minWidth: 0 }}
                    onChange={(e) => setItem(i, { slug: e.target.value })} />
                  <span className="muted" style={{ fontSize: 11 }}>.html</span>
                </div>
              </td>
              <td title="Optional. Printed in bold before the full name on the index page only — DSA: Regulation (EU) 2022/2065…">
                <input value={it.short || ""} placeholder="DSA" style={{ minWidth: 0 }}
                  onChange={(e) => setItem(i, { short: e.target.value })} />
              </td>
              <td>
                <textarea rows={2} value={it.note} onChange={(e) => setItem(i, { note: e.target.value })}
                  placeholder="optional — appears on a new line below the shared text, in this file only" />
              </td>
              <td style={{ whiteSpace: "nowrap" }}>
                <button className="mini" title="move up" disabled={i === 0} onClick={() => move(i, -1)}>↑</button>{" "}
                <button className="mini" title="move down" disabled={i === items.length - 1} onClick={() => move(i, 1)}>↓</button>{" "}
                <button className="mini" title="remove from the set"
                  onClick={() => patch({ items: items.filter((_x, n) => n !== i) })}>✕</button>
              </td>
            </tr>
          ))}
          {!items.length && <tr><td colSpan={5} className="muted">No statutes yet — add one below.</td></tr>}
        </tbody>
      </table>

      <div style={{ marginTop: 8 }}>
        {adding ? (
          <div className="row" style={{ alignItems: "center", gap: 6 }}>
            <div style={{ flex: 1 }}>
              <DocAutocomplete autoFocus instrumentOnly
                placeholder="find a statute by name — GDPR, Online Safety Act…"
                onPick={(id, title) => {
                  setAdding(false);
                  if (items.some((it) => it.stable_id === id)) { setMsg("error: already in the set"); return; }
                  patch({ items: [...items, { stable_id: id, title, slug: "", short: "", note: "" }] });
                }} />
            </div>
            <button className="mini" onClick={() => setAdding(false)}>cancel</button>
          </div>
        ) : <button onClick={() => setAdding(true)}>+ Add a statute</button>}
      </div>

      <label style={{ marginTop: 12 }}>Export folder <span className="muted">— on the server, beside the database and stores; rewritten in place each run</span></label>
      <input value={cfg.output_dir} placeholder={cfg.resolved_output_dir}
        onChange={(e) => patch({ output_dir: e.target.value })} />
      <p className="muted" style={{ fontSize: 11, marginTop: 2 }}>
        Currently <span className="kbd">{cfg.resolved_output_dir}</span>. Files with the same name are overwritten — no versioning, no confirmation.
      </p>

      <div className="row" style={{ marginTop: 10, flexWrap: "wrap", alignItems: "center" }}>
        <button className="primary" disabled={busy || !items.length}
          title="Rebuild every statute from the corpus as it stands, write the export folder, and download a zip of the whole set."
          onClick={() => build({ zip: true, refresh: true })}>
          {busy ? "Exporting…" : "⇩ Build and download ZIP"}</button>
        <button disabled={busy || !items.length}
          title="Same rebuild, but only into the export folder on the server — nothing is downloaded."
          onClick={() => build({ zip: false, refresh: true })}>⌸ Build to folder only</button>
        <button disabled={busy || !items.length}
          title="Re-render the pages from the last build's data — seconds, not hours. Use after editing the shared text, a statute's line, or the index text; it does NOT pick up new citing documents."
          onClick={() => build({ zip: true, refresh: false })}>↻ Quick re-render (cached data)</button>
        <button disabled={busy || !dirty} onClick={save}>Save without exporting</button>
      </div>

      {busy && prog && (
        <div style={{ marginTop: 8 }}>
          <div className="job-bar"><div style={{ width: `${Math.round(prog.fraction * 100)}%` }} /></div>
          <p className="muted" style={{ fontSize: 12, margin: 0 }}>
            {Math.round(prog.fraction * 100)}% · {prog.message}
          </p>
          <p className="muted" style={{ fontSize: 11, margin: "2px 0 0" }}>
            A heavily-cited statute reads every document that mentions it, which can take a long time. This runs ahead of the
            job queue, and the Jobs panel shows the same progress if you navigate away — but leave this tab open for the download.
          </p>
        </div>
      )}
      {msg && <p className={msg.startsWith("error") ? "err" : "ok"} style={{ fontSize: 12 }}>{msg}</p>}

      <fieldset style={{ marginTop: 14 }}>
        <legend>Automatic rebuild <span className="muted">— the scheduler republishes the folder on its own</span></legend>
        <div className="row" style={{ alignItems: "center", gap: 8, flexWrap: "wrap" }}>
          <label style={{ flex: "0 0 auto", display: "flex", gap: 6, alignItems: "center", fontSize: 13 }}
            title="Rebuild the export folder on a schedule. Scheduled runs write the folder only — there is no browser to hand a zip to.">
            <input type="checkbox" checked={!!sched?.enabled}
              onChange={(e) => setSchedule({ enabled: e.target.checked })} />
            Rebuild the export folder automatically
          </label>
          {sched?.enabled && <label style={{ flex: "0 0 auto" }}>every{" "}
            <FrequencySelect minutes={sched.every_minutes || 10080}
              onChange={(m) => setSchedule({ every_minutes: m })} /></label>}
          {/* A full rebuild reads every citing document, so it belongs in the quiet
              hours — cadence alone would fire it at whatever time the last run landed on. */}
          {sched?.enabled && <label style={{ flex: "0 0 auto" }}>at{" "}
            <select value={sched.at_hour == null ? "any" : String(sched.at_hour)}
              onChange={(e) => setSchedule({ at_hour: e.target.value })}>
              <option value="any">any hour</option>
              {Array.from({ length: 24 }, (_, h) => (
                <option key={h} value={h}>{String(h).padStart(2, "0")}:00 UTC</option>
              ))}
            </select></label>}
        </div>
        <p className="muted" style={{ fontSize: 11, margin: "6px 0 0" }}>
          Runs in the scheduler, not this browser, and rereads the corpus each time — the same work as
          <span className="kbd">Build to folder only</span>. Nothing is downloaded and no zip is written.
        </p>
        <p className="muted" style={{ fontSize: 12, margin: "6px 0 0" }}>
          {run.finished_at
            ? `Last export: ${new Date(run.finished_at).toLocaleString()} — ${run.documents || 0} editions → ${run.output_dir}`
            : "Not exported yet."}
          {run.webhook && (run.webhook.ok
            ? ` · webhook ${run.webhook.status ?? "sent"} ✓`
            : ` · webhook failed: ${run.webhook.error || run.webhook.status}`)}
        </p>
      </fieldset>

      <fieldset style={{ marginTop: 12 }}>
        <legend>When a run finishes <span className="muted">— call something (ntfy, a sync hook, an scp trigger)</span></legend>
        <div className="row" style={{ gap: 8, alignItems: "flex-end", flexWrap: "wrap" }}>
          <label style={{ flex: "0 0 auto", display: "flex", gap: 6, alignItems: "center", fontSize: 13 }}>
            <input type="checkbox" checked={!!hook.enabled}
              onChange={(e) => setHook({ enabled: e.target.checked })} />
            Send it
          </label>
          <label style={{ flex: "0 0 90px" }}>method
            <select value={hook.method || "POST"} onChange={(e) => setHook({ method: e.target.value })}>
              <option>POST</option><option>PUT</option><option>GET</option>
            </select></label>
          <label style={{ flex: "1 1 320px" }}>URL
            <input value={hook.url || ""} placeholder="https://ntfy.sh/my-raglex-topic"
              onChange={(e) => setHook({ url: e.target.value })} /></label>
        </div>
        <div className="row" style={{ gap: 8, alignItems: "flex-start", flexWrap: "wrap", marginTop: 6 }}>
          <label style={{ flex: "1 1 240px" }}>headers <span className="muted">— one <span className="kbd">Name: value</span> per line</span>
            <textarea rows={3} value={headerText}
              placeholder={"Title: RagLex export\nAuthorization: Bearer …"}
              onChange={(e) => { setHeaderDraft(e.target.value); setHook({ headers: parseHeaders(e.target.value) }); }} /></label>
          <label style={{ flex: "1 1 240px" }}>body
            <textarea rows={3} value={hook.body || ""}
              placeholder="{documents} editions written to {output_dir}"
              onChange={(e) => setHook({ body: e.target.value })} /></label>
        </div>
        <p className="muted" style={{ fontSize: 11, marginTop: 2 }}>
          Placeholders: <span className="kbd">{WEBHOOK_PLACEHOLDERS}</span>. Leave the body empty to send the whole
          run summary as JSON. A failed call is recorded on the run and never fails the export.
        </p>
        <div className="row" style={{ gap: 8, alignItems: "center", marginTop: 6 }}>
          <button className="mini" disabled={!hook.url} onClick={async () => {
            setHookMsg("sending…");
            try {
              if (dirty && !(await save())) { setHookMsg(""); return; }
              const r = await api.testBundleWebhook();
              setHookMsg(r.ok ? `✓ ${r.status} from ${r.url}` : `error: ${r.error || r.status || "no response"}`);
            } catch (e: any) { setHookMsg("error: " + e.message); }
          }}>Send a test now</button>
          {hookMsg && <span className={hookMsg.startsWith("error") ? "err" : "muted"}
            style={{ fontSize: 12 }}>{hookMsg}</span>}
        </div>
      </fieldset>
    </div>
  );
}

export function SettingsView() {
  const [settings, setSettings] = useState<Setting[]>([]);
  const [path, setPath] = useState("");
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [msg, setMsg] = useState("");
  const [loadErr, setLoadErr] = useState("");
  const [loading, setLoading] = useState(true);
  const [health] = useAsync(() => api.embeddingHealth(), [msg]);
  // Load with explicit error/loading state — a silent .then meant a failed/timed-out
  // fetch (e.g. under DB load) left the page blank with no hint and no way to retry.
  const load = () => {
    setLoading(true); setLoadErr("");
    api.getSettings().then((r) => { setSettings(r.settings); setPath(r.path); })
      .catch((e) => setLoadErr(String(e?.message || e))).finally(() => setLoading(false));
  };
  useEffect(() => { load(); }, []);
  // "Static exports" is edited on Admin ▸ Static export — publishing schedules work and
  // writes a folder, so it belongs with the operational surfaces, not with preferences.
  const groups = [...new Set(settings.map((s) => s.group))].filter((g) => g !== "Static exports");
  return (
    <div>
      <div className="panel">
        <p className="muted" style={{ margin: 0, fontSize: 13 }}>
          Publishing a set of statutes as a small website — including the text shown beneath every
          exported statute — lives on <b>Admin ▸ Static export</b>.
        </p>
      </div>
      <div className="panel">
        {health && <p className={health.healthy ? "ok" : "err"}>Embedding provider: {health.provider}/{health.model} ({health.dimensions}d) — {health.healthy ? "ready ✓" : "needs an API key ✗"}</p>}
        {loading && !settings.length && <p className="muted loading-pulse">Loading settings…</p>}
        {loadErr && !settings.length && <p className="err">Couldn't load settings: {loadErr}{" "}
          <button className="mini" onClick={load}>retry</button></p>}
        <p className="muted">Stored in <span className="kbd">{path}</span> (bind-mount the data dir to persist).
          An environment variable, if set, overrides the file value.</p>
        {groups.map((g) => (
          <div key={g}>
            <h3>{g}</h3>
            {settings.filter((s) => s.group === g).map((s) => (
              <div key={s.key}>
                <label>{s.label} <span className="kbd">{s.key}</span>
                  {s.source === "env" && <span className="muted"> · set via environment (overrides file)</span>}
                  {s.source === "file" && s.set && s.kind !== "bool" && <span className="ok"> · {s.secret ? s.display : "saved"}</span>}
                </label>
                {s.kind === "bool" ? (
                  <label className="toggle-row">
                    <input type="checkbox" disabled={s.source === "env"}
                      checked={(() => {
                        const raw = String(edits[s.key] ?? (s.set ? s.display : "")).toLowerCase();
                        return raw === "" ? true : !["0", "off", "false", "no"].includes(raw);
                      })()}
                      onChange={(e) => setEdits({ ...edits, [s.key]: e.target.checked ? "1" : "0" })} />
                    <span className="muted">{s.placeholder}</span>
                  </label>
                ) : s.kind === "html" ? (
                  <textarea rows={6} placeholder={s.placeholder || (s.set ? s.display : "")}
                    disabled={s.source === "env"}
                    value={edits[s.key] ?? (s.source === "file" ? s.display : "")}
                    onChange={(e) => setEdits({ ...edits, [s.key]: e.target.value })} />
                ) : (
                  <input type={s.secret ? "password" : "text"} placeholder={s.placeholder || (s.set ? s.display : "")}
                    disabled={s.source === "env"}
                    value={edits[s.key] ?? ""} onChange={(e) => setEdits({ ...edits, [s.key]: e.target.value })} />
                )}
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

// A cadence picker in human terms (daily/weekly/…) over the stored minutes. "Custom" keeps
// the raw minutes input for anything off the presets, so existing odd cadences still edit.
const FREQ_PRESETS: [string, number][] = [
  ["Hourly", 60], ["Every 6 hours", 360], ["Daily", 1440], ["Weekly", 10080],
  ["Fortnightly", 20160], ["Monthly", 43200],
];
function FrequencySelect({ minutes, onChange }: { minutes: number; onChange: (m: number) => void }) {
  const isPreset = FREQ_PRESETS.some(([, m]) => m === minutes);
  return (
    <span className="row" style={{ display: "inline-flex", gap: 4, alignItems: "center" }}>
      <select value={isPreset ? String(minutes) : "custom"}
              onChange={(e) => { if (e.target.value !== "custom") onChange(+e.target.value); }}>
        {FREQ_PRESETS.map(([label, m]) => <option key={m} value={m}>{label}</option>)}
        <option value="custom">Custom…</option>
      </select>
      {!isPreset && <><input type="number" min={5} value={minutes}
        onChange={(e) => onChange(+e.target.value || 1440)} style={{ width: 70 }} /> min</>}
    </span>
  );
}

// --- Keep current: sources, their update status, and the watches on them ----
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
  const auto = [
    ["Pull EU case names / subjects (EUR-Lex)", "daily (off by default)", "Fills missing CJEU case names so their OSCOLA citations read properly. Needs EUR-Lex credentials; enable the 'eu-case-names' task to run it daily."],
    ["Re-check outstanding legislation amendments", "hourly", "Re-pulls acts whose legislation.gov.uk effects re-check is due (bounded)."],
    ["Propagate changes an act makes", "hourly", "Flags held acts affected by a change for re-pull."],
    ["Rebuild citation-frequency roll-up", "hourly", "Keeps the worklist ranking fresh."],
    ["Top up the statute gazetteer", "weekly", "Pulls newly passed acts from legislation.gov.uk so name citations keep confirming."],
    ["Import EU dated consolidations", "weekly + on demand", "Walks Cellar's sector-0 catalogue directly, including future-effective snapshots. Opening an EU base act with a missing lineage also starts a targeted sync automatically."],
    ["Drain the harvest worklist", "per tick", "Fetches a bounded batch of routable references each tick (set auto-drain on the Unresolved tab)."],
    ["Run due watches", "per tick", "Every enabled watch whose cadence is due starts as a Job."],
  ];
  return (
    <div className="panel">
      <h3 style={{ marginTop: 0 }}>Keep current <span className="muted">— automatic upkeep the scheduler already runs</span></h3>
      <p className="muted" style={{ fontSize: 13 }}>
        You don't have to trigger any of this by hand — the background scheduler runs it on a cadence, and its
        work shows in the <b>Jobs</b> panel. To force one now, find it under <b>Maintenance actions</b> below.
      </p>
      <table className="grid"><thead><tr><th>task</th><th>runs</th></tr></thead>
        <tbody>
          {auto.map(([label, when, hint], i) => (
            <tr key={i}><td>{label}<div className="muted" style={{ fontSize: 11 }}>{hint}</div></td>
              <td className="muted" style={{ whiteSpace: "nowrap" }}>{when}</td></tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// --- Maintenance actions ----------------------------------------------------
// This was one row of fourteen equal-looking buttons, several of them migrations for
// defects fixed for good. Nothing distinguished "the nightly essential" from "run once in
// March and never again", so all fourteen read as standing obligations. Now each action
// declares its cadence and explains itself (see maintenance.ts), the page groups by
// cadence, and the spent migrations are folded away behind a disclosure.
function relRunTime(iso?: string | null): string {
  if (!iso) return "";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (isNaN(s)) return "";
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  if (s < 2592000) return `${Math.round(s / 86400)}d ago`;
  return `${Math.round(s / 2592000)}mo ago`;
}

function ActionRow({ a, last, onRun, busy }: {
  a: MaintAction; last?: { status: string; at: string | null; found: number | null };
  onRun: (a: MaintAction) => void; busy: boolean;
}) {
  // "Ran, found nothing" is the sentence that retires a migration. Say it plainly —
  // it is the only evidence an operator has that a one-off is genuinely done.
  const spent = last && last.status === "done" && last.found === 0;
  return (
    <div className="maint-action">
      <div className="maint-action-text">
        <div className="maint-action-head">
          <b>{a.label}</b>
          {a.heavy && <span className="tag warn" title="Walks the whole corpus — hours, not minutes. Not something to start casually on a busy box.">heavy</span>}
          {spent && <span className="tag" title="Its last run reported nothing left to do.">found nothing</span>}
          {last?.status === "error" && <span className="tag err" title="Its last run failed — see the Jobs panel.">last run failed</span>}
        </div>
        <p className="muted maint-what">{a.what}</p>
        <p className="muted maint-when"><b>When:</b> {a.when}</p>
      </div>
      <div className="maint-action-run">
        {last?.at && <span className="muted maint-last" title={`last finished ${last.at}`}>{relRunTime(last.at)}</span>}
        <button disabled={busy} onClick={() => onRun(a)}>{busy ? "…" : "Run"}</button>
      </div>
    </div>
  );
}

function MaintenanceActionsPanel() {
  const [lastRun, , reloadLast] = useAsync(() => api.jobsLastRun(), []);
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [showSpent, setShowSpent] = useState(false);
  const [ppSource, setPpSource] = useState("");
  const kinds = lastRun?.kinds ?? {};

  const run = async (a: MaintAction) => {
    const key = a.label;
    setBusy(key); setMsg("");
    // the one action that takes a free-text scope, kept beside it rather than in a
    // separate row that gave no clue which button it belonged to
    const params = a.kind === "finish-bulk-postprocess" && ppSource.trim()
      ? { ...(a.params || {}), source: ppSource.trim() } : (a.params || {});
    try {
      const r = await api.startAction(a.kind, params, a.endpoint);
      if (r.error) setMsg(`✗ ${a.label}: ${r.error}`);
      else if (r.already_running) setMsg(`• ${a.label} is already in flight`);
      else setMsg(`✓ ${a.label} — ${r.queued ? "queued" : "started"}; follow it in the Jobs panel`);
      reloadLast();
    } catch (e: any) { setMsg(`✗ ${a.label}: ${e}`); } finally { setBusy(null); }
  };

  const groups = GROUP_ORDER
    .map((g) => [g, ACTIONS.filter((a) => a.group === g)] as const)
    .filter(([, rows]) => rows.length > 0);

  return (
    <div className="panel">
      <h3 style={{ marginTop: 0 }}>Maintenance actions <span className="muted">— what you can ask the corpus to do</span></h3>
      <p className="muted" style={{ fontSize: 13 }}>
        Everything here runs as a background job, so you can start it and leave the page. Most of it
        you will never need: the scheduler already runs the upkeep, and the migrations at the bottom
        were for defects that have since been fixed at the source.
      </p>
      {groups.map(([group, rows]) => {
        const spentGroup = group === "One-off migrations";
        const open = !spentGroup || showSpent;
        return (
          <section key={group} className="maint-group">
            <h4 className="maint-group-head">
              {spentGroup ? (
                <button className="mini disclosure" aria-expanded={open}
                  onClick={() => setShowSpent((v) => !v)}>
                  {open ? "▾" : "▸"} {group} <span className="muted">({rows.length})</span>
                </button>
              ) : group}
              {!spentGroup && <span className="muted maint-group-hint">
                {CADENCE_META[rows[0].cadence].hint}
              </span>}
            </h4>
            {spentGroup && !open && <p className="muted" style={{ fontSize: 12, margin: "2px 0 0" }}>
              {CADENCE_META["one-off"].hint}
            </p>}
            {open && rows.map((a) => (
              <ActionRow key={a.label} a={a} last={kinds[a.kind]} busy={busy === a.label} onRun={run} />
            ))}
            {open && group === "Citation extraction" && (
              <label className="muted maint-scope">
                scope the tag pass of “finish an interrupted import” to one source
                <input value={ppSource} onChange={(e) => setPpSource(e.target.value)}
                  placeholder="blank = resolve only, whole graph" list="pp-bulk-sources" />
                <datalist id="pp-bulk-sources">
                  {["fr-dila", "de-rii", "de-gii", "nl-rechtspraak", "nl-legislation", "uk-caselaw", "in-caselaw"].map((s) => <option key={s} value={s} />)}
                </datalist>
              </label>
            )}
          </section>
        );
      })}
      {msg && <p className={msg.includes("✗") ? "err" : "ok"} style={{ fontSize: 12, marginTop: 8 }}>{msg}</p>}
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
        <button onClick={() => fireJob("canlii-enrich", { limit: 200 }, setMsg)}
          title="Decorate held Canadian decisions with what the CanLII API knows: the canlii.ca permalink (a verified 'view on CanLII' link), docket number, subject keywords, parallel-citation aliases, and the citator's edges (cited cases + legislation; citing cases capped). Metadata only — CanLII's API never returns text. Budget-metered (most-cited first) and resumable; needs RAGLEX_CANLII_API_KEY in Settings.">🍁 CanLII enrich (Canadian metadata + citator)</button>
      </div>
      {msg && <p className={msg.startsWith("✗") ? "err" : "ok"} style={{ fontSize: 12, marginTop: 6 }}>{msg}</p>}
    </div>
  );
}

// The consolidated "Maintain" page: keep-current upkeep, gap backfill, watches, and rules —
// the whole "grow + keep the corpus complete" surface in one place.
// "Get everything from this source" — the full-catalogue backfill, as a background job.
// Distinct from a Watch (which keeps a source *current* on a cadence): this is the
// one-off walk that fills the corpus from a register's back-catalogue.
function BackfillPanel() {
  const [cat] = useAsync(() => api.sourceCatalog(), []);
  const [source, setSource] = useState("");
  const [srcOpts, setSrcOpts] = useState<Record<string, string>>({});
  const [bounded, setBounded] = useState(false);
  const [maxPages, setMaxPages] = useState(5);
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);
  const info = (cat ?? []).find((s: any) => s.key === source);
  const sourceGroups = (cat ?? []).reduce((groups: Record<string, any[]>, item: any) => {
    (groups[item.group_label || "Other"] ||= []).push(item);
    return groups;
  }, {});

  async function run() {
    if (!source) { setMsg("pick a source"); return; }
    if (source === "eu-legislation"
        && /^(?:1|true|yes)$/i.test(srcOpts.include_consolidations?.trim() || "")
        && !srcOpts.celex?.trim()) {
      setMsg("✗ dated consolidations require explicit sector-3 CELEX ids (for the UCPD: 32005L0029)");
      return;
    }
    setBusy(true); setMsg("");
    const opts = Object.fromEntries(Object.entries(srcOpts).filter(([, v]) => v.trim()));
    try {
      const r = await api.harvestSource({
        source, backfill: true,
        max_pages: bounded ? maxPages : null,
        ...(Object.keys(opts).length ? { options: opts } : {}),
      });
      if (r.error) setMsg("✗ " + r.error);
      else if (r.already_running) setMsg("• a backfill of this source is already running");
      else setMsg("✓ queued — follow it in the Jobs panel (bottom-left)");
    } catch (e: any) { setMsg("✗ " + e); } finally { setBusy(false); }
  }

  return (
    <div className="panel">
      <h3 style={{ marginTop: 0 }}>Backfill a source <span className="muted">— pull its whole back-catalogue</span></h3>
      <p className="muted" style={{ fontSize: 13 }}>
        Runs the source with its <b>backfill</b> path: no incremental cursor and, unless you cap it, no page limit —
        so it walks the register as far as it goes. Registers with a real feed or API (UK &amp; EU legislation, the
        Australian Commonwealth) enumerate their whole catalogue; the Irish eISB walks every year; the Australian
        LawMaker states need <span className="kbd">enumerate=true</span> (set it in the options below) because their
        feed only carries recent changes. This can run for hours and is paced politely — it runs as a job, so you can
        leave the page.
      </p>
      <div className="row" style={{ flexWrap: "wrap" }}>
        <select value={source} onChange={(e) => { setSource(e.target.value); setSrcOpts({}); }} style={{ flex: "0 0 auto", minWidth: 260 }}>
          <option value="">— source —</option>
          {Object.entries(sourceGroups).map(([group, rows]: [string, any]) => (
            <optgroup key={group} label={group}>
              {rows.map((s: any) => <option key={s.key} value={s.key}>{s.kind_label} · {s.label}</option>)}
            </optgroup>
          ))}
        </select>
        <label style={{ flex: "0 0 auto" }} title="Uncapped walks the whole catalogue; capping is useful for a trial run.">
          <input type="checkbox" checked={bounded} onChange={(e) => setBounded(e.target.checked)} /> cap at
          <input type="number" min={1} max={500} value={maxPages} disabled={!bounded}
            onChange={(e) => setMaxPages(+e.target.value || 1)} style={{ width: 64, marginLeft: 6 }} /> pages
        </label>
        <button className="primary" disabled={busy || !source} style={{ flex: "0 0 auto" }} onClick={run}>
          {busy ? "queuing…" : "⤓ Backfill everything"}
        </button>
      </div>
      {info && <div style={{ marginTop: 6 }}>
        <p className="muted" style={{ fontSize: 12, marginBottom: 4 }}>{info.description}</p>
        <SourceCaps info={info} />
      </div>}
      {info && (info.options ?? []).length > 0 && <div className="row" style={{ flexWrap: "wrap", marginTop: 6 }}>
        {(info.options ?? []).map((o: any) => (
          <input key={o.name} value={srcOpts[o.name] ?? ""} title={o.label}
            onChange={(e) => setSrcOpts({ ...srcOpts, [o.name]: e.target.value })}
            placeholder={`${o.label}${o.placeholder ? ` — ${o.placeholder}` : ""}`} style={{ minWidth: 210 }} />
        ))}
      </div>}
      {source === "eu-legislation" && <p className="muted" style={{ fontSize: 12, marginTop: 7 }}>
        <b>Consolidations are a targeted pull, not part of the whole sector-3 walk.</b>
        {" "}For the UCPD enter <span className="kbd">32005L0029</span> in CELEX ids and
        <span className="kbd">true</span> in Fetch dated consolidations. Importing them
        automatically adds the base/version links and retrofits applicable-version links
        onto citations already in the database.
      </p>}
      {msg && <p className={msg.startsWith("✗") ? "err" : "ok"} style={{ wordBreak: "break-word" }}>{msg}</p>}
    </div>
  );
}

// --- Keep-current status by jurisdiction -----------------------------------
// The diagnosis view the audit asked for: for every source, HOW it stays current
// (server-side / early-stop / full-walk / targeted-only / bulk / closed), whether a
// watch is actually wired + when it next fires, held count + failure state, and the
// last few runs' pulled/new/deduped/errors. So dormant keep-current is visible, not folklore.
const MODE_META: Record<string, { label: string; tone: string; hint: string }> = {
  server: { label: "server-side", tone: "ok", hint: "The API filters by date — only new rows cross the wire. Ideal." },
  "early-stop": { label: "early-stop feed", tone: "ok", hint: "Newest-first feed; the crawl stops at the cursor, reading ~1 page. Efficient." },
  "full-walk": { label: "full re-walk", tone: "warn", hint: "Must re-read the whole source's index each run, then filter past the cursor. Correct but heavy." },
  targeted: { label: "targeted-only", tone: "err", hint: "Fetched by id only — there is NO feed to poll for new items. A keep-current gap." },
  bulk: { label: "bulk seed", tone: "muted", hint: "A local-file seed with no live path (usually has a live sibling). Re-seed manually." },
  closed: { label: "closed archive", tone: "muted", hint: "No new items ever exist — nothing to keep current." },
  none: { label: "—", tone: "muted", hint: "No incremental path." },
};

function relTime(iso?: string | null): string {
  if (!iso) return "never";
  const t = new Date(iso).getTime();
  if (isNaN(t)) return "—";
  const s = (Date.now() - t) / 1000;
  const past = s >= 0; const a = Math.abs(s);
  const n = a < 3600 ? `${Math.round(a / 60)}m` : a < 86400 ? `${Math.round(a / 3600)}h`
    : a < 2592000 ? `${Math.round(a / 86400)}d` : `${Math.round(a / 2592000)}mo`;
  return past ? `${n} ago` : `in ${n}`;
}

function RunDots({ runs }: { runs: any[] }) {
  // one dot per recent run, newest right; green = new docs, blue = all-deduped (steady),
  // red = errored / rate-limited. Hover for the counts.
  if (!runs || !runs.length) return <span className="muted" style={{ fontSize: 11 }}>no runs logged</span>;
  return (
    <span style={{ display: "inline-flex", gap: 3, alignItems: "center" }}>
      {[...runs].reverse().map((r, i) => {
        const err = r.errors > 0 || r.rate_limited;
        const color = err ? "var(--bad)" : r.stored > 0 ? "var(--ok)" : "var(--accent)";
        const when = relTime(r.started_at);
        return <span key={i} title={`${when} · ${r.trigger}${r.backfill ? " · backfill" : ""}\n` +
          `discovered ${r.discovered} · +${r.stored} new · ${r.deduped} seen · ${r.refreshed} refreshed` +
          `${r.errors ? ` · ${r.errors} errors` : ""}${r.rate_limited ? " · RATE-LIMITED" : ""}`}
          style={{ width: 9, height: 9, borderRadius: "50%", background: color, opacity: 0.55 + 0.45 * (i + 1) / runs.length }} />;
      })}
    </span>
  );
}

// One watch's plan, rendered inline: what it harvests / discovers / enriches / tags.
function WatchPlan({ w }: { w: any }) {
  return (
    <span className="muted" style={{ fontSize: 11 }}>
      {w.keywords?.length ? <>“{w.keywords.join(", ")}” </> : "all new items "}
      {w.discover ? <span className="ok">· follows cites of {w.discover} </span> : null}
      {w.enrich ? "· fetches cited authorities " : "· no enrichment "}
      {w.tag ? <>· →#{w.tag} </> : null}
      {w.last_result && <>· <i>{summariseRun(w.last_result)}</i></>}
    </span>
  );
}

// The inline "add a watch to this source" form, revealed inside a source's dropdown.
function AddWatchInline({ source, info, onCreated }: { source: string; info: any; onCreated: () => void }) {
  const [name, setName] = useState("");
  const [keywords, setKeywords] = useState("");
  const [enrich, setEnrich] = useState(true);
  const [tag, setTag] = useState("");
  const [cadence, setCadence] = useState(1440);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");
  async function create() {
    if (!name.trim()) { setMsg("give the watch a name"); return; }
    setBusy(true); setMsg("");
    const spec: any = { source, enrich, max_pages: 1 };
    if (keywords.trim()) spec.keywords = keywords.split(",").map((k) => k.trim()).filter(Boolean);
    if (tag.trim()) spec.tag = tag.trim();
    try {
      await api.createWatch({ name: name.trim(), spec, cadence_minutes: cadence });
      setName(""); setKeywords(""); setTag(""); onCreated();
    } catch (e: any) { setMsg("error: " + e); } finally { setBusy(false); }
  }
  return (
    <div className="add-watch">
      <div className="row" style={{ flexWrap: "wrap", alignItems: "center", gap: 8 }}>
        <input value={name} onChange={(e) => setName(e.target.value)} placeholder="new watch name" style={{ minWidth: 160 }} />
        <input value={keywords} onChange={(e) => setKeywords(e.target.value)} style={{ minWidth: 200 }}
          placeholder={info?.keyword_search ? "keywords (searched at source), comma-sep" : "keywords (post-filter), comma-sep — optional"} />
        <label style={{ flex: "0 0 auto" }} title="After harvesting, fetch the routable authorities each new case cites (one hop).">
          <input type="checkbox" checked={enrich} onChange={(e) => setEnrich(e.target.checked)} /> fetch cited authorities</label>
        <input value={tag} onChange={(e) => setTag(e.target.value)} placeholder="tag (optional)" style={{ maxWidth: 150 }} />
        <label style={{ flex: "0 0 auto" }}>every <FrequencySelect minutes={cadence} onChange={setCadence} /></label>
        <button className="primary" disabled={busy} onClick={create}>{busy ? "saving…" : "+ Add watch"}</button>
      </div>
      {msg && <p className="err" style={{ fontSize: 12, margin: "4px 0 0" }}>{msg}</p>}
    </div>
  );
}

// The unified keep-current dashboard: every source, grouped by jurisdiction, showing how it
// stays current and the watches on it. Expand a source to manage its watches inline (edit
// cadence, enable/disable, run, delete) and add a new one — status and settings in one place.
function KeepCurrentDashboard({ navigate }: { navigate?: (f: Record<string, string>) => void }) {
  const [data, err, reload, loading] = useAsync(() => api.keepCurrent(), []);
  const [cat] = useAsync(() => api.sourceCatalog(), []);
  const [allWatches, , reloadWatches] = useAsync(() => api.watches(), []);
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [msg, setMsg] = useState("");
  // 110 source rows across every jurisdiction made this a 9,616px block. Reveal by
  // jurisdiction — the unit someone actually looks for — rather than by row.
  const revealJuris = useRevealer(4);
  const reloadAll = () => { reload(); reloadWatches(); };
  const catByKey: Record<string, any> = {};
  for (const c of cat ?? []) catByKey[c.key] = c;

  const runNow = async (key: string) => {
    setBusy("h" + key); setMsg("");
    try {
      const r = await api.harvestSource({ source: key, backfill: false, max_pages: 3 });
      setMsg(r.error ? `✗ ${key}: ${r.error}` : `✓ harvest ${key} queued — watch it in Jobs`);
    } catch (e: any) { setMsg("✗ " + e); } finally { setBusy(null); }
  };
  const toggleWatch = async (id: number, enabled: boolean) => {
    setBusy("w" + id);
    try { await api.updateWatch(id, { enabled }); reloadAll(); } finally { setBusy(null); }
  };
  const setCadence = async (id: number, m: number) => { await api.updateWatch(id, { cadence_minutes: m }); reloadAll(); };
  const runWatch = async (id: number) => {
    setBusy("r" + id); setMsg("");
    try { const r = await api.runWatch(id); setMsg(r.error ? "✗ " + r.error : `✓ watch queued — see Jobs`); reloadAll(); }
    catch (e: any) { setMsg("✗ " + e); } finally { setBusy(null); }
  };
  const del = async (id: number) => { if (!confirm("Delete this watch?")) return; await api.deleteWatch(id); reloadAll(); };

  if (err) return <div className="panel"><h3>Keep current</h3><p className="err">{err}</p></div>;
  if (!data) return <div className="panel"><h3>Keep current</h3><p className="muted">loading…</p></div>;

  const groups: Record<string, any[]> = {};
  for (const s of data.sources) (groups[s.group_label || "Other"] ||= []).push(s);
  const order = Object.keys(groups).sort((a, b) => a.localeCompare(b));
  const jurisReveal = revealJuris(order);
  const capable = data.sources.filter((s: any) => s.can_incremental);
  const watched = capable.filter((s: any) => (s.watches || []).some((w: any) => w.enabled));
  const gaps = data.sources.filter((s: any) => s.incremental_mode === "targeted");
  // discover-citing watches aren't bound to a source — surface them in their own section
  const citingWatches = (allWatches ?? []).filter((w: any) => !w.spec?.source && w.spec?.discover?.citing);

  const th = (label: string) => <th key={label}>{label}</th>;
  return (
    <div className="panel keep-current">
      <div className="row" style={{ justifyContent: "space-between", alignItems: "baseline" }}>
        <h3 style={{ marginTop: 0 }}>Keep current <span className="muted">— sources, their status, and the watches on them</span></h3>
        <button onClick={reloadAll} disabled={loading} style={{ flex: "0 0 auto" }}>↻ Refresh</button>
      </div>
      <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>
        {watched.length}/{capable.length} pollable sources have a watch keeping them current. Incremental runs
        re-check {data.overlap_default_days} day(s) before their cursor (set in Settings), and never let a
        future-dated item skip the queue.{gaps.length > 0 && <> {gaps.length} source(s) can only be fetched by
        naming an item — no feed to poll.</>} Expand a source to add or manage its watches.
      </p>
      {jurisReveal.shown.map((j: any) => (
        <div key={j} style={{ marginBottom: 16 }}>
          <div className="kc-juris">{j} <span className="muted">({groups[j].length})</span></div>
          <table className="grid kc-table">
            <thead><tr>{["source", "how it updates", "held", "recent runs", "watches", ""].map(th)}</tr></thead>
            <tbody>
              {groups[j].sort((a: any, b: any) => a.kind.localeCompare(b.kind) || a.label.localeCompare(b.label)).map((s: any) => {
                const m = MODE_META[s.incremental_mode] || MODE_META.none;
                const ws: any[] = s.watches || [];
                const enabledN = ws.filter((w) => w.enabled).length;
                const isOpen = !!expanded[s.key];
                const canHarvest = s.incremental_mode !== "closed" && s.incremental_mode !== "bulk";
                const info = catByKey[s.key];
                return (
                  <>
                    <tr key={s.key} className={isOpen ? "kc-row open" : "kc-row"}>
                      <td>
                        <button className="kc-caret" title={isOpen ? "collapse" : "expand — manage watches"}
                          onClick={() => setExpanded((e) => ({ ...e, [s.key]: !e[s.key] }))}>{isOpen ? "▾" : "▸"}</button>
                        <span title={s.key}>{s.label}</span>
                        <div className="muted" style={{ fontSize: 10 }}>{s.kind} · {s.key}
                          {s.consecutive_failures > 0 && <span className="err"> · ⚠ {s.consecutive_failures} fails</span>}</div>
                      </td>
                      <td><span className="cap-chip" data-tone={m.tone} title={m.hint}>{m.label}</span>
                        {s.watermark && <div className="muted" style={{ fontSize: 10 }}>cursor {String(s.watermark).slice(0, 19)}</div>}</td>
                      <td style={{ textAlign: "right" }}>
                        {navigate && s.doc_count > 0
                          ? <a onClick={() => navigate({ source: s.key })} style={{ cursor: "pointer" }}
                              title={`Browse the ${s.doc_count.toLocaleString()} document(s) held from ${s.label} in Search`}>
                              {s.doc_count.toLocaleString()}</a>
                          : s.doc_count.toLocaleString()}</td>
                      <td><RunDots runs={s.recent_runs} /></td>
                      <td style={{ whiteSpace: "nowrap" }}>
                        {ws.length === 0
                          ? (s.can_incremental
                            ? <a className="kc-add" onClick={() => setExpanded((e) => ({ ...e, [s.key]: true }))}>+ add watch</a>
                            : <span className="muted">—</span>)
                          : <a onClick={() => setExpanded((e) => ({ ...e, [s.key]: !e[s.key] }))} style={{ cursor: "pointer" }}
                              title="expand to manage">{enabledN}/{ws.length} active{ws.length === 1 && ws[0].enabled
                                ? <span className="muted"> · next {relTime(ws[0].next_due)}</span> : null}</a>}
                      </td>
                      <td style={{ whiteSpace: "nowrap", textAlign: "right" }}>
                        {canHarvest && <button className="mini" disabled={busy === "h" + s.key}
                          title="Harvest new items now (bounded)" onClick={() => runNow(s.key)}>↻ harvest now</button>}
                      </td>
                    </tr>
                    {isOpen && (
                      <tr className="kc-detail"><td colSpan={6}>
                        {info && <div style={{ marginBottom: 8 }}><SourceCaps info={info} /></div>}
                        {ws.length > 0 && <table className="grid kc-watches"><tbody>
                          {ws.map((w) => (
                            <tr key={w.watch_id}>
                              <td style={{ width: 24 }}><input type="checkbox" checked={w.enabled} title="enabled"
                                onChange={() => toggleWatch(w.watch_id, !w.enabled)} /></td>
                              <td><b>{w.name}</b><div><WatchPlan w={w} /></div></td>
                              <td style={{ whiteSpace: "nowrap" }} className="muted">
                                every <FrequencySelect minutes={w.cadence_minutes} onChange={(mm) => setCadence(w.watch_id, mm)} />
                                <div style={{ fontSize: 10 }}>last {relTime(w.last_run_at)}{w.enabled && <> · next {relTime(w.next_due)}</>}</div></td>
                              <td style={{ whiteSpace: "nowrap", textAlign: "right" }}>
                                <button className="mini" disabled={busy === "r" + w.watch_id} onClick={() => runWatch(w.watch_id)}>▸ run</button>{" "}
                                <a className="kc-del" title="delete" onClick={() => del(w.watch_id)}>✗</a></td>
                            </tr>
                          ))}
                        </tbody></table>}
                        <AddWatchInline source={s.key} info={info} onCreated={reloadAll} />
                      </td></tr>
                    )}
                  </>
                );
              })}
            </tbody>
          </table>
        </div>
      ))}
      {jurisReveal.more}

      <div className="kc-juris" style={{ marginTop: 8 }}>Following citations
        <span className="muted"> — watches that pull NEW cases citing a target, as they appear</span></div>
      <CitingWatches watches={citingWatches} reload={reloadAll} />
      {msg && <p className={msg.includes("✗") ? "err" : "ok"} style={{ fontSize: 12 }}>{msg}</p>}
    </div>
  );
}

// Source-less watches that follow forward-citations of a target (find NEW cases citing X),
// plus an inline form to add one — the second, connected half of the keep-current surface.
function CitingWatches({ watches, reload }: { watches: any[]; reload: () => void }) {
  const [name, setName] = useState("");
  const [target, setTarget] = useState("");
  const [enrich, setEnrich] = useState(true);
  const [tag, setTag] = useState("");
  const [cadence, setCadence] = useState(1440);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");
  async function create() {
    if (!name.trim() || !target.trim()) { setMsg("give a name and a citation to follow"); return; }
    setBusy(true); setMsg("");
    const spec: any = { discover: { citing: target.trim(), via: "auto" }, enrich, max_pages: 1 };
    if (tag.trim()) spec.tag = tag.trim();
    try { await api.createWatch({ name: name.trim(), spec, cadence_minutes: cadence }); setName(""); setTarget(""); setTag(""); reload(); }
    catch (e: any) { setMsg("error: " + e); } finally { setBusy(false); }
  }
  return (
    <div style={{ marginBottom: 8 }}>
      {watches.length > 0 && <table className="grid kc-watches" style={{ marginBottom: 8 }}><tbody>
        {watches.map((w) => (
          <tr key={w.watch_id}>
            <td style={{ width: 24 }}><input type="checkbox" checked={w.enabled}
              onChange={async () => { await api.updateWatch(w.watch_id, { enabled: !w.enabled }); reload(); }} /></td>
            <td><b>{w.name}</b> <span className="ok" style={{ fontSize: 11 }}>follows cites of {w.spec.discover.citing}</span>
              <div><WatchPlan w={{ ...w.spec, enrich: w.spec.enrich !== false, last_result: w.last_result, discover: null }} /></div></td>
            <td className="muted" style={{ whiteSpace: "nowrap" }}>every <FrequencySelect minutes={w.cadence_minutes}
              onChange={async (m) => { await api.updateWatch(w.watch_id, { cadence_minutes: m }); reload(); }} /></td>
            <td style={{ textAlign: "right", whiteSpace: "nowrap" }}>
              <button className="mini" onClick={async () => { await api.runWatch(w.watch_id); reload(); }}>▸ run</button>{" "}
              <a className="kc-del" title="delete" onClick={async () => { if (confirm("Delete this watch?")) { await api.deleteWatch(w.watch_id); reload(); } }}>✗</a></td>
          </tr>
        ))}
      </tbody></table>}
      <div className="add-watch">
        <div className="row" style={{ flexWrap: "wrap", alignItems: "center", gap: 8 }}>
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder="watch name" style={{ minWidth: 150 }} />
          <input value={target} onChange={(e) => setTarget(e.target.value)} style={{ minWidth: 260, color: "var(--ok)" }}
            placeholder="follow NEW cases citing… e.g. 32016R0679 (GDPR) or [2014] UKSC 38" />
          <label style={{ flex: "0 0 auto" }} title="Fetch what each newly found case cites (one hop).">
            <input type="checkbox" checked={enrich} onChange={(e) => setEnrich(e.target.checked)} /> fetch cited authorities</label>
          <input value={tag} onChange={(e) => setTag(e.target.value)} placeholder="tag (optional)" style={{ maxWidth: 150 }} />
          <label style={{ flex: "0 0 auto" }}>every <FrequencySelect minutes={cadence} onChange={setCadence} /></label>
          <button className="primary" disabled={busy} onClick={create}>{busy ? "saving…" : "+ Follow citation"}</button>
        </div>
        {msg && <p className="err" style={{ fontSize: 12, margin: "4px 0 0" }}>{msg}</p>}
      </div>
    </div>
  );
}

export function MaintainView({ open, navigate }:
  { open: (id: string) => void; navigate?: (f: Record<string, string>) => void }) {
  return (
    <div>
      <div className="panel" style={{ background: "transparent", border: "none", padding: 0, marginBottom: 8 }}>
        <h2 style={{ margin: 0 }}>Maintain</h2>
        <p className="muted" style={{ marginTop: 4 }}>
          Grow the corpus and keep it current. Keep current shows every source, how it updates, and the
          watches on it — schedule new material there. Backfill a source pulls a register's whole
          back-catalogue; Backfill gaps chases completeness court-by-court; Expand coverage pulls in
          cited-but-missing authorities; Rules are optional shorthands.
        </p>
        <DbSizeStat />
      </div>
      <JobsQueuePanel />
      <KeepCurrentDashboard navigate={navigate} />
      <KeepCurrentPanel />
      <MaintenanceActionsPanel />
      <BackfillPanel />
      <GapFillPanel />
      <ExpandCoveragePanel />
      <FeedbackPanel open={open} />
      <RefinementFlagsPanel open={open} />
      <ShorthandsPanel open={open} />
      <RulesView open={open} />
    </div>
  );
}

// Job concurrency + scheduler controls: how many jobs run at once (extras queue), a live
// running/queued count, and a "pause all scheduled jobs" switch. The queue itself is
// automatic (overflow past the cap waits and promotes FIFO); this just surfaces + tunes it.
function JobsQueuePanel() {
  const [st, , reload] = useAsync(() => api.queueStatus(), []);
  const [busy, setBusy] = useState<string | null>(null);
  const [maxInput, setMaxInput] = useState<number | null>(null);
  useEffect(() => { const t = setInterval(reload, 5000); return () => clearInterval(t); }, [reload]);
  if (!st) return null;
  const max = maxInput ?? st.max_concurrent;
  const saveMax = async () => {
    setBusy("max");
    try { await api.setMaxConcurrent(max); setMaxInput(null); reload(); }
    finally { setBusy(null); }
  };
  const togglePause = async () => {
    setBusy("pause");
    try { await api.schedulerPause(!st.scheduler_paused); reload(); }
    finally { setBusy(null); }
  };
  return (
    <div className="panel">
      <h3 style={{ marginTop: 0 }}>Jobs &amp; queue</h3>
      <div className="row" style={{ flexWrap: "wrap", alignItems: "center", gap: 16 }}>
        <span title="Jobs running now vs the concurrency cap. Queue-exempt work (a static export, a consolidation import a reader is waiting on) runs beside the queue and is not counted against the cap.">
          <b style={{ color: "var(--ok)" }}>{st.running}</b> running
          {st.queued > 0 && <> · <b>{st.queued}</b> queued</>}
          <span className="muted"> / max {st.max_concurrent}</span>
          {st.over_cap > 0 && <span className="muted" title="Reader-triggered work that skips the queue by design">
            {" "}(+{st.over_cap} beside the queue)</span>}
        </span>
        {/* A queue that is not moving should say why. Silence here is what turned a stale
            setting into an afternoon of wondering why a free slot stayed empty. */}
        {st.queued > 0 && st.slots_used < st.max_concurrent && st.blocked.length > 0 &&
          <span className="muted" title="These queued jobs conflict with something already running — a whole-corpus singleton, an overlapping citation-extraction scope, or an identical job. They start as soon as it finishes.">
            {st.blocked.length} waiting on a conflicting job, not on a slot
          </span>}
        <label style={{ flex: "0 0 auto" }} title="How many jobs run at once; extras queue and start as slots free. Lower it on a busy box.">
          max concurrent{" "}
          <input type="number" min={1} max={32} value={max}
                 onChange={(e) => setMaxInput(+e.target.value || 1)} style={{ width: 56 }} />
          {maxInput !== null && maxInput !== st.max_concurrent &&
            <button className="primary" disabled={busy === "max"} style={{ marginLeft: 4 }} onClick={saveMax}>set</button>}
        </label>
        <button className={st.scheduler_paused ? "primary" : ""} disabled={busy === "pause"} onClick={togglePause}
                title="Pause the scheduler's recurring jobs + due watches. Manual and queued jobs keep running.">
          {st.scheduler_paused ? "▶ Resume scheduled jobs" : "⏸ Pause all scheduled jobs"}
        </button>
        {st.scheduler_paused && <span className="muted">scheduled jobs paused — manual &amp; queued still run</span>}
      </div>
    </div>
  );
}

// Current database disk footprint — total in GB plus the largest tables on hover.
function DbSizeStat() {
  const [s] = useAsync(() => api.systemStorage(), []);
  if (!s) return null;
  const gb = (b: number) => (b / 1024 ** 3).toFixed(b >= 100 * 1024 ** 3 ? 0 : 1);
  const top = (s.tables || []).slice(0, 6)
    .map((t) => `${t.name}: ${gb(t.bytes)} GB`).join("\n");
  return (
    <p className="muted" style={{ marginTop: 6, fontSize: 12 }}
       title={top ? `Largest tables:\n${top}` : undefined}>
      🖴 Database: <b>{gb(s.database_bytes)} GB</b>
      {s.tables?.length > 0 && <span> — largest: {s.tables[0].name} ({gb(s.tables[0].bytes)} GB)</span>}
    </p>
  );
}

// Reader passages flagged "for improved refinement" — the queue of linking mistakes a
// human noticed, with everything an LLM/engineer needs to reproduce each one: the doc,
// the passage, what it links to now, and what the user says it should do.
function RefinementFlagsPanel({ open }: { open: (id: string, a?: string) => void }) {
  const [flags, , reload] = useAsync(() => api.refinementFlags("open"), []);
  const reveal = useRevealer(10);
  const { shown, more } = reveal<any>(flags || []);
  if (!flags || flags.length === 0) return null;
  return (
    <div className="panel">
      <h3 style={{ marginTop: 0 }}>Flagged for refinement <span className="muted">
        — passages you marked as badly linked, for the next pass over the linking logic</span>
        <span className="tag" style={{ marginLeft: 8 }}>{flags.length}</span></h3>
      {/* table-layout: fixed + explicit widths. Without them the first column (a document
          id, which can be a long unbroken slug) took the whole width and squeezed the
          passage and the note into a few characters each. Long tokens break mid-string —
          a URL broken across lines beats a table nothing else fits in. */}
      <table className="grid break-cells" style={{ tableLayout: "fixed", width: "100%" }}>
        <thead><tr>
          <th style={{ width: "22%" }}>where</th><th style={{ width: "30%" }}>passage</th>
          <th style={{ width: "20%" }}>links now</th><th style={{ width: "20%" }}>should</th>
          <th style={{ width: "8%" }} />
        </tr></thead>
        <tbody>{shown.map((f: any) => {
          let links: any[] = [];
          try { links = JSON.parse(f.current_links || "[]"); } catch { /* legacy */ }
          return (
            <tr key={f.flag_id}>
              <td>
                <DocLink id={f.doc_id} anchor={f.anchor || undefined} onOpen={() => open(f.doc_id, f.anchor || undefined)}>{f.doc_id}</DocLink>
                {f.anchor && <span className="muted"> · {f.anchor}</span>}</td>
              <td><b>“{f.selected_text}”</b></td>
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
      {more}
    </div>
  );
}

// --- Long lists reveal rather than run on ------------------------------------
// A panel that renders every row it has is fine at twenty rows and unusable at a hundred:
// the Maintain page measured 109,683px tall on a phone, of which the feedback list alone
// was 104,433px. Everything below it — the shorthand rules, the linking rules — was
// effectively unreachable, and scrolling past it on a touch screen takes real seconds.
//
// So a list shows its first ``limit`` and offers the rest. Deliberately NOT applied to a
// document's own body: a statute or judgment is the thing you came to read, and hiding
// half of it behind a button would break both reading and in-page search.
// Two parts on purpose. The hook holds only the expanded/collapsed flag and is called
// unconditionally at the top of a component; the function it returns is pure and may be
// applied to a list derived AFTER an early return. Taking the list as a hook argument
// instead put a useState below `if (!data) return …` in three of these panels, which is a
// Rules-of-Hooks violation — the panel rendered blank the moment its data arrived.
function useRevealer(limit = 12) {
  const [all, setAll] = useState(false);
  return function reveal<T>(rows: T[]) {
    const hidden = Math.max(0, rows.length - limit);
    return {
      shown: all || hidden === 0 ? rows : rows.slice(0, limit),
      hidden,
      // rendered under the list; null when there is nothing to reveal
      more: hidden === 0 ? null : (
        <button className="mini reveal-more" onClick={() => setAll((v) => !v)}>
          {all ? "▴ show fewer" : `▾ show all ${rows.length} (${hidden} more)`}
        </button>
      ),
    };
  };
}

// Bug reports and feature requests submitted from the app's feedback box. They landed in a
// table nothing displayed, so the only way to read them was psql — which is a good way to
// never read them. Each row carries the page it was sent from, so a report about a document
// opens that document.
function FeedbackPanel({ open }: { open: (id: string, a?: string) => void }) {
  const [status, setStatus] = useState("open");
  const [items, err, reload] = useAsync(() => api.feedback(status), [status]);
  const [busy, setBusy] = useState<number | null>(null);
  // 98 open items rendered a 104,433px-tall panel, which is most of why the Maintain page
  // was 109,683px on a phone and everything under it unreachable.
  const reveal = useRevealer(10);
  if (err) return null;
  const rows: any[] = items || [];
  const { shown, more } = reveal(rows);
  const docOf = (page: string) => (page || "").startsWith("document:") ? page.slice(9) : null;
  return (
    <div className="panel">
      <h3 style={{ marginTop: 0 }}>Feedback <span className="muted">
        — bug reports and requests sent from the app</span>
        <span className="tag" style={{ marginLeft: 8 }}>{rows.length}</span>
        <select value={status} onChange={(e) => setStatus(e.target.value)}
          className="sort-select" style={{ marginLeft: 10 }} title="which feedback to show">
          <option value="open">open</option><option value="resolved">resolved</option>
        </select>
      </h3>
      {rows.length === 0
        ? <p className="muted" style={{ fontSize: 13, margin: 0 }}>Nothing {status}.</p>
        : (
        <table className="grid break-cells" style={{ tableLayout: "fixed", width: "100%" }}>
          <thead><tr>
            <th style={{ width: "9%" }}>kind</th><th style={{ width: "52%" }}>message</th>
            <th style={{ width: "23%" }}>where</th><th style={{ width: "16%" }} />
          </tr></thead>
          <tbody>{shown.map((f: any) => {
            const doc = docOf(f.page);
            return (
              <tr key={f.feedback_id}>
                <td><span className="tag">{f.kind}</span>
                  <div className="muted" style={{ fontSize: 10 }}>{String(f.created_at).slice(0, 16).replace("T", " ")}</div></td>
                <td style={{ whiteSpace: "pre-wrap" }}>{f.message}</td>
                <td className="muted" style={{ fontSize: 12 }}>
                  {doc
                    ? <DocLink id={doc} onOpen={() => open(doc)}>{doc}</DocLink>
                    : (f.page || "—")}
                  {f.url && <div style={{ fontSize: 10 }}>{f.url}</div>}</td>
                <td style={{ textAlign: "right" }}>
                  <button className="mini" disabled={busy === f.feedback_id}
                    title={status === "open" ? "mark handled" : "reopen"}
                    onClick={async () => {
                      setBusy(f.feedback_id);
                      try { await api.setFeedback(f.feedback_id, status === "open" ? "resolved" : "open"); reload(); }
                      finally { setBusy(null); }
                    }}>{status === "open" ? "✓ resolve" : "↺ reopen"}</button></td>
              </tr>
            );
          })}</tbody>
        </table>
      )}
      {more}
    </div>
  );
}

// The corpus-wide learned-shorthand store. A document that writes "… [Suncor]" teaches
// the whole corpus that "Suncor" means that case; the store is what carries it into the
// NEXT document. That leverage cuts both ways — a bad entry mislinks everywhere at once,
// which is how "Article 8", "appellant" and "may make" came to render as links to the
// Convention — so it needs somewhere to be looked at and switched off by hand.
//
// Blocking beats deleting: the store is insert-only, so a deleted row is re-learned the
// next time its defining document is rescanned. A blocked one stays blocked.
function ShorthandsPanel({ open }: { open: (id: string, a?: string) => void }) {
  const [state, setState] = useState("invalid");
  const [q, setQ] = useState("");
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(0);
  const PER = 50;
  const [data, err, reload] = useAsync(
    () => api.shorthands({ q: query, state, limit: PER, offset: page * PER }),
    [query, state, page]);
  const [busy, setBusy] = useState<string | null>(null);
  const [purge, setPurge] = useState<any>(null);
  if (err) return null;
  const rows: any[] = data?.rows || [];
  const total: number = data?.total ?? 0;
  const counts = data?.counts || {};
  const key = (r: any) => `${r.shorthand} ${r.candidate_id}`;

  async function act(r: any, fn: () => Promise<any>) {
    setBusy(key(r));
    try { await fn(); reload(); } finally { setBusy(null); }
  }

  return (
    <div className="panel">
      <h3 style={{ marginTop: 0 }}>Shorthands <span className="muted">
        — names the corpus has learned stand for an authority</span>
        <span className="tag" style={{ marginLeft: 8 }}>{counts.total ?? "—"} stored</span>
        {counts.corpus_wide != null && <span className="tag" style={{ marginLeft: 4 }}
          title={`established in ${counts.threshold ?? 3}+ documents, so they travel to other documents`}>
          {counts.corpus_wide} corpus-wide</span>}
        {counts.blocked > 0 && <span className="tag" style={{ marginLeft: 4 }}>{counts.blocked} blocked</span>}
      </h3>
      <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", marginBottom: 8 }}>
        <select value={state} onChange={(e) => { setState(e.target.value); setPage(0); }}
          className="sort-select" title="which shorthands to show">
          <option value="invalid">needs review</option>
          <option value="all">all</option>
          <option value="active">corpus-wide</option>
          <option value="local">document-local</option>
          <option value="blocked">blocked</option>
        </select>
        <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="name or target id…"
          onKeyDown={(e) => { if (e.key === "Enter") { setQuery(q); setPage(0); } }}
          style={{ flex: "1 1 220px", minWidth: 160 }} />
        <button className="mini" onClick={() => { setQuery(q); setPage(0); }}>search</button>
        <span className="muted" style={{ fontSize: 12 }}>{total} match</span>
        <span style={{ flex: 1 }} />
        <button className="mini" disabled={busy === "purge"}
          title="delete every stored shorthand that would not be learned today"
          onClick={async () => {
            setBusy("purge");
            try {
              const dry = await api.purgeShorthands(true);
              if (window.confirm(`Delete ${dry.invalid} of ${dry.scanned} stored shorthands that would not be learned today?`)) {
                setPurge(await api.purgeShorthands(false)); reload();
              } else setPurge(dry);
            } finally { setBusy(null); }
          }}>purge unlearnable…</button>
      </div>
      {purge && <p className="muted" style={{ fontSize: 12, margin: "0 0 8px" }}>
        scanned {purge.scanned}, unlearnable {purge.invalid}
        {purge.dry_run ? " (dry run — nothing deleted)" : `, deleted ${purge.deleted}`}</p>}
      {state === "invalid" && <p className="muted" style={{ fontSize: 12, margin: "0 0 8px" }}>
        Rows the current rules would reject — provision references ("Article 8"), generic
        role words ("appellant"), sentence fragments. These are already ignored when
        linking; listing them is how the store gets cleaned up.</p>}
      {rows.length === 0
        ? <p className="muted" style={{ fontSize: 13, margin: 0 }}>Nothing here.</p>
        : (
        <table className="grid break-cells" style={{ tableLayout: "fixed", width: "100%" }}>
          <thead><tr>
            <th style={{ width: "26%" }}>shorthand</th>
            <th style={{ width: "40%" }}>stands for</th>
            <th style={{ width: "13%" }}>links on</th>
            <th style={{ width: "21%" }} />
          </tr></thead>
          <tbody>{rows.map((r: any) => (
            <tr key={key(r)} style={r.blocked ? { opacity: 0.5 } : undefined}>
              <td>
                <b>{r.shorthand}</b>
                {!r.valid && <span className="tag" style={{ marginLeft: 6 }} title="would not be learned today">unlearnable</span>}
                {r.blocked && <span className="tag" style={{ marginLeft: 6 }}>blocked</span>}
                {r.valid && !r.blocked && !r.applies_corpus_wide &&
                  <span className="tag" style={{ marginLeft: 6 }}
                    title={`established in ${r.doc_count || 0} document(s) — too few to travel, so it links only where it was defined`}>
                    document-local</span>}
                {r.first_doc && <div className="muted" style={{ fontSize: 10 }}>
                  first seen in <DocLink id={r.first_doc} onOpen={() => open(r.first_doc)}>{r.first_doc}</DocLink></div>}
              </td>
              <td>
                <DocLink id={r.candidate_id} onOpen={() => open(r.candidate_id)}>
                  {r.target_title || r.candidate_id}</DocLink>
                {r.target_title && <div className="muted" style={{ fontSize: 10 }}>{r.candidate_id}</div>}
              </td>
              <td className="muted" style={{ fontSize: 12 }}>
                {/* a STORED shorthand always needs a pinpoint: applied only where the
                    document already cites the parent, a bare hit adds nothing but a
                    second, pincite-less edge to an authority already linked. */}
                with a pinpoint
                <div style={{ fontSize: 10 }}>{r.doc_count || 0} doc{r.doc_count === 1 ? "" : "s"}</div>
              </td>
              <td style={{ textAlign: "right" }}>
                <button className="mini" disabled={busy === key(r)}
                  title={r.blocked ? "let this link again" : "stop this linking anywhere"}
                  onClick={() => act(r, () => api.setShorthand({
                    shorthand: r.shorthand, candidate_id: r.candidate_id, blocked: !r.blocked }))}>
                  {r.blocked ? "↺ unblock" : "⊘ block"}</button>{" "}
                {/* Both forms require a pinpoint; this chooses WHICH KIND counts.
                    An instrument's abbreviation is pinpointed by provision
                    ("s. 3 of the FMIOA", "BPRs, reg 5"); a case short name by
                    paragraph ("Suncor, at para 30"). */}
                <button className="mini" disabled={busy === key(r)}
                  title={r.is_abbrev
                    ? "treat as a CASE short name — links on a paragraph pincite"
                    : "treat as an INSTRUMENT abbreviation — links on a provision pinpoint"}
                  onClick={() => act(r, () => api.setShorthand({
                    shorthand: r.shorthand, candidate_id: r.candidate_id, is_abbrev: !r.is_abbrev }))}>
                  {r.is_abbrev ? "provision" : "paragraph"}</button>{" "}
                <button className="mini" disabled={busy === key(r)} title="delete (may be re-learned on rescan)"
                  onClick={() => act(r, () => api.deleteShorthand(r.shorthand, r.candidate_id))}>✕</button>
              </td>
            </tr>
          ))}</tbody>
        </table>
      )}
      {total > PER && (
        <div style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 8 }}>
          <button className="mini" disabled={page === 0} onClick={() => setPage(page - 1)}>← prev</button>
          <span className="muted" style={{ fontSize: 12 }}>
            {page * PER + 1}–{Math.min((page + 1) * PER, total)} of {total}</span>
          <button className="mini" disabled={(page + 1) * PER >= total} onClick={() => setPage(page + 1)}>next →</button>
        </div>
      )}
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
  // default collapsed on a phone so it's just a small "Jobs ●" note in the corner,
  // not a panel eating the width; expanded by default on desktop
  const [collapsed, setCollapsed] = useState(() =>
    typeof window !== "undefined" && window.innerWidth <= 640);
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
  // Queued jobs are their own thing, not history. Lumped in with the finished ones they
  // were capped at four, sorted among "done"/"error" rows, and offered no cancel — so a
  // queue you no longer wanted could only be cleared from the API. They are the jobs most
  // worth cancelling: nothing has been spent on them yet.
  const waiting = jobs.filter((j) => j.status === "queued");
  const recent = jobs.filter((j) => j.status !== "running" && j.status !== "queued")
    .slice(0, 4);
  if (active.length === 0 && waiting.length === 0 && recent.length === 0) return null;
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
          // The current phase from the persisted checkpoint (extract → resolve → tag),
          // so a multi-phase bulk import shows WHICH pass its numbers count, not just a
          // bare fraction whose meaning silently changed.
          const phase = j.resume?.checkpoint?.phase;
          return (
            <div key={j.id} className={`job${j.stalled ? " job-stalled" : ""}${j.waiting ? " job-waiting" : ""}`}>
              <div className="row" style={{ alignItems: "center", gap: 6 }}>
                <a onClick={() => setOpenId(isOpen ? null : j.id)} style={{ flex: 1, cursor: "pointer", fontSize: 12 }}>
                  {isOpen ? "▾" : "▸"} {j.label || j.kind}
                  {phase && <span className="tag" style={{ marginLeft: 6, fontSize: 10 }} title="Current pipeline phase (from the job's saved checkpoint)">{phase}</span>}
                  {j.origin === "scheduler" && <span className="tag" style={{ marginLeft: 6, fontSize: 10 }} title="Started by the background scheduler, not from this UI">scheduler</span>}</a>
                {j.waiting && <span className="job-wait-tag" title={`The worker process is alive (lease heartbeat ${Math.round(j.lease_idle_s)}s ago), but this phase has not emitted an item-progress event for ${Math.round(j.idle_s)}s. It may be discovering, downloading, parsing, retrying, or doing database work; it is not classified as stopped.`}>working · quiet phase</span>}
                {j.stalled && <span className="job-stall-tag" title={`The worker's process lease expired ${Math.round(j.lease_idle_s)}s ago. This is genuinely stopped, rather than merely a long source/database phase. Restart resumes according to the job's saved policy/checkpoint.`}>worker stopped</span>}
                <button className="mini" title={j.stalled ? "Restart this stopped job from its durable checkpoint/state." : "Request a cooperative restart. The current live worker is cancelled first, so two writers never overlap."} onClick={() => api.restartJob(j.id)}>↻ restart</button>
                <button className="mini" onClick={() => api.cancelJob(j.id)}>cancel</button>
              </div>
              <div className="job-bar"><div style={{ width: `${pct}%` }} /></div>
              <div className="muted" style={{ fontSize: 11 }}>
                {j.last || (p.stage ? `${p.stage} ${p.done ?? 0}/${p.total ?? "?"}` : "starting…")}
                {j.eta_s != null && j.eta_s > 0 && <span style={{ marginLeft: 6 }} title={j.rate_per_s ? `${j.rate_per_s}/s` : undefined}>· ~{fmtEta(j.eta_s)} left</span>}
                {j.waiting && <span style={{ marginLeft: 6 }}>· worker alive; last item update {fmtEta(j.idle_s)} ago</span>}
              </div>
              {isOpen && detail?.log && (
                <pre className="job-log">{(detail.log || []).slice(-14).join("\n")}</pre>
              )}
            </div>
          );
        })}
        {waiting.length > 0 && <div className="muted" style={{ fontSize: 11, marginTop: 6 }}>
          Waiting for a slot — {waiting.length} queued
        </div>}
        {waiting.map((j, n) => (
          <div key={j.id} className="job-done row" title={j.last} style={{ alignItems: "center", gap: 6 }}>
            <span style={{ flex: 1 }}>
              <span className="muted">{n + 1}.</span> {j.label || j.kind}
              {j.origin === "scheduler" && <span className="tag" style={{ marginLeft: 6, fontSize: 10 }} title="Started by the background scheduler, not from this UI">scheduler</span>}
            </span>
            <button className="mini" title="Drop this job from the queue. Nothing has run yet, so nothing is lost."
              onClick={() => api.cancelJob(j.id)}>cancel</button>
          </div>
        ))}
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

// Compact remaining-time estimate for the jobs dock: "3d 4h", "2h 10m", "5m", "40s".
function fmtEta(s: number): string {
  if (s >= 86400) return `${Math.floor(s / 86400)}d ${Math.round((s % 86400) / 3600)}h`;
  if (s >= 3600) return `${Math.floor(s / 3600)}h ${Math.round((s % 3600) / 60)}m`;
  if (s >= 60) return `${Math.round(s / 60)}m`;
  return `${Math.round(s)}s`;
}

function summariseRun(r: any): string {
  if (!r) return "";
  const stored = r.harvest?.stored ?? 0;
  const disc = r.discover?.count ?? 0;
  const fetched = r.enrich?.fetched ?? 0;
  return [stored ? `harvested ${stored}` : "", disc ? `discovered ${disc}` : "",
          fetched ? `+${fetched} cited` : "", r.tagged ? `tagged ${r.tagged}` : ""]
    .filter(Boolean).join(" · ") || "no new material";
}

// --- Unresolved references -------------------------------------------------
// The hanging references the corpus cites but can't satisfy. Each can be resolved
// by supplying the missing identifier, linking to an existing item, scraping a
// URL, or uploading the source file (§5b).
export function UnresolvedView({ open, navigate }: { open: (id: string) => void; navigate?: (f: Record<string, string>) => void }) {
  // the queue is cached server-side (stale-while-revalidate); a cold load returns
  // {rows:[], _warming:true} and computes in the background — poll until it's ready
  const [data, err, reload] = useAsync<any>(() => api.unresolved(5000), []);
  const rows: any[] | null = data?.rows ?? null;
  const [cov, , reloadCov] = useAsync(() => api.coverage(), []);
  useEffect(() => {
    if (!data?._warming) return;
    const iv = setInterval(() => reload(), 2500);
    return () => clearInterval(iv);
  }, [data?._warming]);
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
  const [bucketFilter, setBucketFilter] = useState<"" | "pending" | "cooling" | "name_only">("");
  const [categoryFilter, setCategoryFilter] = useState("");
  const [wlPage, setWlPage] = useState(0);   // worklist pager (10 rows/page — keep the page short)
  // any filter change resets to the first page (else you land past the end of a shorter list)
  useEffect(() => { setWlPage(0); }, [srcFilter, bucketFilter, legFilter, categoryFilter]);
  const showWorklist = (bucket: "pending" | "cooling" | "name_only", category = "", subtype = "") => {
    setBucketFilter(bucket); setCategoryFilter(category);
    setSrcFilter(category && category !== "other" ? category : "");
    setLegFilter(category === "uk-legislation" ? subtype.split(":")[0] : "");
    requestAnimationFrame(() => document.getElementById("harvest-worklist")?.scrollIntoView({ behavior: "smooth" }));
  };
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
    (!categoryFilter || r.category === categoryFilter) &&
    (!legFilter || r.leg_kind === legFilter) &&
    (!bucketFilter || (bucketFilter === "name_only" ? (r.needs_identifier || r.confidence === "low" || !r.suggested_adapter)
      : bucketFilter === "cooling" ? r.cooling
      : (!r.cooling && !r.needs_identifier && r.confidence !== "low" && !!r.suggested_adapter))));
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
    <CorpusMap cov={cov} navigate={navigate} showWorklist={showWorklist} />
    <div className="panel" id="harvest-worklist">
      <div className="row worklist-head" style={{ justifyContent: "space-between", alignItems: "flex-start", flexWrap: "wrap" }}>
        <h3 style={{ margin: 0, flex: "1 1 100%", minWidth: 0 }}>Harvest worklist <span className="muted">— citations the corpus can’t find (yet), most-cited first. Resolve by harvest, identifier, existing item, scrape, or upload.</span></h3>
        <div className="row" style={{ flex: "1 1 auto", alignItems: "center", minWidth: 0 }}>
          <select className="theme-select" value={srcFilter}
            onChange={(e) => { setSrcFilter(e.target.value); setCategoryFilter(e.target.value); setLegFilter(""); }} title="Filter by source">
            <option value="">All sources</option>
            {sources.map((s) => <option key={s} value={s}>{s} ({byCat[s]})</option>)}
          </select>
          <select className="theme-select" value={bucketFilter}
            onChange={(e) => setBucketFilter(e.target.value as any)} title="Filter by queue state">
            <option value="">All states</option>
            <option value="pending">Pending / untried</option>
            <option value="cooling">Cooling</option>
            <option value="name_only">Name-only / manual</option>
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
          {list.slice(wlPage * 10, wlPage * 10 + 10).map((r) => (
            <ResolveRow key={r.ref} r={r} open={open}
              active={active === r.ref} toggle={() => setActive(active === r.ref ? null : r.ref)}
              onDone={reloadAll} />
          ))}
        </tbody>
      </table>
      <Pager page={wlPage} pageSize={10} total={list.length} onPage={setWlPage} noun="references" />
    </div>
    <UnfetchablePanel />
    <RetrievalExportPanel />
    <AllSuggestionsPanel />
    </div>
  );
}

// A compact multi-select: a button showing the current selection, opening a checklist
// popover. Used where a row of loose checkboxes would sprawl across the toolbar and
// still not say, at a glance, what is selected.
function MultiSelect({ options, value, onChange, placeholder = "none", title }: {
  options: { key: string; label: string }[];
  value: string[];
  onChange: (next: string[]) => void;
  placeholder?: string; title?: string;
}) {
  const [open, setOpen] = useState(false);
  const box = useRef<HTMLDivElement | null>(null);
  // click-away and Escape both close it — a popover that can only be dismissed by
  // re-clicking the trigger feels stuck
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (box.current && !box.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => { document.removeEventListener("mousedown", onDown); document.removeEventListener("keydown", onKey); };
  }, [open]);
  const chosen = options.filter((o) => value.includes(o.key));
  const summary = chosen.length === 0 ? placeholder
    : chosen.length === options.length ? "all"
    : chosen.length <= 2 ? chosen.map((o) => o.label).join(", ")
    : `${chosen.length} selected`;
  const toggle = (k: string) =>
    onChange(value.includes(k) ? value.filter((v) => v !== k) : [...value, k]);
  return (
    <div ref={box} className="multiselect" style={{ position: "relative", display: "inline-block" }}>
      <button type="button" className="multiselect-trigger" title={title}
        aria-haspopup="listbox" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
        {summary} <span className="muted" aria-hidden="true">▾</span>
      </button>
      {open && (
        <div className="multiselect-menu" role="listbox" aria-multiselectable="true">
          {options.map((o) => (
            <label key={o.key} className="multiselect-item">
              <input type="checkbox" checked={value.includes(o.key)} onChange={() => toggle(o.key)} />
              <span>{o.label}</span>
            </label>
          ))}
          <div className="multiselect-actions">
            <button type="button" className="mini" onClick={() => onChange(options.map((o) => o.key))}>all</button>
            <button type="button" className="mini" onClick={() => onChange([])}>none</button>
          </div>
        </div>
      )}
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
  // Westlaw UK / Lexis+ UK are UK subscriptions: a foreign report in the batch can't
  // retrieve and just burns one of the 100 slots — so default to UK only. The bucket
  // vocabulary comes from the server (it's what the filter resolves into) and is now
  // per-jurisdiction (Canada, Australia, NZ, Singapore, Hong Kong… separately, not one
  // "Commonwealth" lump), so a new bucket shows up here without a frontend change.
  const [facets] = useAsync(() => api.facetValues(), []);
  const JURS: { key: string; label: string }[] =
    facets?.retrieval_jurisdictions || [{ key: "uk", label: "United Kingdom" }];
  const [jurs, setJurs] = useState<string[]>(["uk"]);
  const jurCsv = jurs.join(",");
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
        <span style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 13 }}>
          <span className="muted">jurisdictions:</span>
          <MultiSelect options={JURS} value={jurs} onChange={setJurs} placeholder="all"
            title="Which jurisdictions to include, split per country (Canada, Australia, NZ, Singapore, Hong Kong… separately). A UK subscription can't retrieve a foreign report — it'd just burn a slot in the 100-citation batch." />
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
  // The floor is the main cost control: ~70% of hanging references are cited exactly
  // once, and classifying that tail is most of the build time. 2 by default, with the
  // long tail one click away rather than silently dropped.
  const [minCiting, setMinCiting] = useState(2);
  const [data, err, reload] = useAsync(() => api.unfetchable(200, minCiting), [minCiting]);
  useEffect(() => {
    if (!data?._warming) return;
    const iv = setInterval(() => reload(), 2500);
    return () => clearInterval(iv);
  }, [data?._warming]);
  const [upRef, setUpRef] = useState<string | null>(null);
  const [linkRef, setLinkRef] = useState<string | null>(null);
  const [jur, setJur] = useState<string | null>(null);
  const [ufPage, setUfPage] = useState(0);   // 10 rows/page — keep the panel short
  useEffect(() => { setUfPage(0); }, [minCiting, jur]);
  const all: any[] = data?.references || [];
  // Facet over the WHOLE set so the token counts stay honest while one is selected.
  // A reference with no jurisdiction of its own is bucketed by where it is cited FROM.
  const jurOf = (r: any): string | null => r.jurisdiction || r.cited_from?.[0] || null;
  const jurCounts = new Map<string, number>();
  for (const r of all) {
    const j = jurOf(r);
    if (j) jurCounts.set(j, (jurCounts.get(j) || 0) + 1);
  }
  const jurTokens = [...jurCounts.entries()].sort((a, b) => b[1] - a[1]);
  const refs = jur ? all.filter((r) => jurOf(r) === jur) : all;
  if (err) return null;
  return (
    <div className="panel">
      <div className="row" style={{ alignItems: "baseline" }}>
        <h3 style={{ marginTop: 0, flex: 1 }}>Cited but unfetchable
          <span className="muted"> — most-cited authorities the system can’t fetch (classic reporters, cases by name). Follow the link, then upload the file to resolve every citation to it at once.</span>
          {data?.total != null && <span className="tag" style={{ marginLeft: 8 }}>{data.total.toLocaleString()}</span>}
        </h3>
      </div>
      <div className="row" style={{ alignItems: "center", gap: 10, flexWrap: "wrap", marginBottom: 6 }}>
        <label style={{ fontSize: 13 }}
          title="How many documents must cite a reference for it to appear. Most hanging references are cited exactly once; including them makes this list much slower to build.">
          <span className="muted">cited by at least </span>
          <select value={minCiting} onChange={(e) => { setMinCiting(+e.target.value); setJur(null); }}>
            <option value={2}>2 documents</option>
            <option value={3}>3 documents</option>
            <option value={5}>5 documents</option>
            <option value={1}>1 — the whole tail (slow)</option>
          </select>
        </label>
        {jurTokens.length > 1 && (
          <span className="active-chips" style={{ display: "flex", gap: 6, flexWrap: "wrap" }}
            title="Filter by jurisdiction. Taken from the report series or neutral citation where it can be; otherwise from where the reference is cited.">
            {jurTokens.map(([k, n]) => (
              <button key={k} className={`tag tag-btn${jur === k ? " on" : ""}`}
                onClick={() => setJur(jur === k ? null : k)}>{k} <b>{n}</b></button>
            ))}
            {jur && <button className="tag tag-btn tag-clear" onClick={() => setJur(null)}>clear ✕</button>}
          </span>
        )}
      </div>
      {data?._warming && <p className="muted loading-pulse">⏳ ranking the unfetchable frontier…</p>}
      {!data?._warming && refs.length === 0 && <p className="muted">Nothing recognised as unfetchable. ✓</p>}
      {refs.length > 0 && (
        <table className="grid">
          <thead><tr><th>cites</th><th>reference</th><th>looks like</th><th>jurisdiction</th><th>source</th><th></th></tr></thead>
          <tbody>
            {refs.slice(ufPage * 10, ufPage * 10 + 10).map((r) => (
              <Fragment key={r.ref}>
                <tr>
                  <td className="num" style={{ whiteSpace: "nowrap" }}>{r.citing_count}</td>
                  <td style={{ fontFamily: "var(--mono, monospace)", fontSize: 12 }}>{r.raw || r.ref}</td>
                  <td className="muted">{r.form}</td>
                  {/* Its own jurisdiction where the citation states one; otherwise the
                      jurisdictions it is CITED FROM, marked as inference not fact. */}
                  <td className="muted" style={{ whiteSpace: "nowrap", fontSize: 12 }}>
                    {r.jurisdiction
                      ? r.jurisdiction
                      : r.cited_from?.length
                        ? <span title={`Not stated in the citation — inferred from the ${r.cited_from.length > 1 ? "documents that cite it" : "document that cites it"}: ${r.cited_from.join(", ")}`}>
                            <i>cited from</i> {r.cited_from.slice(0, 2).join(", ")}
                            {r.cited_from.length > 2 ? ` +${r.cited_from.length - 2}` : ""}</span>
                        : "—"}
                  </td>
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
                  <tr><td /><td colSpan={5} style={{ borderBottom: "none", paddingTop: 0 }}>
                    {r.suggestions.slice(0, 2).map((s: any, i: number) => <SuggestionRow key={i} s={s} onDone={reload} />)}
                  </td></tr>
                )}
                {upRef === r.ref && (
                  <tr><td colSpan={6}>
                    <UnfetchableUpload r={r} onDone={() => { setUpRef(null); reload(); }} />
                  </td></tr>
                )}
                {linkRef === r.ref && (
                  <tr><td colSpan={6}>
                    <LinkExisting refKey={r.ref} onDone={() => { setLinkRef(null); reload(); }} />
                  </td></tr>
                )}
              </Fragment>
            ))}
          </tbody>
        </table>
      )}
      {refs.length > 0 && <Pager page={ufPage} pageSize={10} total={refs.length} onPage={setUfPage} noun="authorities" />}
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
// The judgement evidence behind a near-miss: the actual passages where the corpus
// cites the hanging reference (citing doc + sentence neighbourhood). Fetched lazily —
// only when a human asks "show me the context" before deciding.
function RefContext({ refKey }: { refKey: string }) {
  const [data, err, , loading] = useAsync(() => api.referenceContext(refKey), [refKey]);
  const peek = usePeek();
  if (loading) return <div className="refctx muted">loading citing passages…</div>;
  if (err || !data?.occurrences?.length)
    return <div className="refctx muted">no stored context spans for this reference</div>;
  return (
    <div className="refctx">
      {data.occurrences.map((o: any, i: number) => (
        <div className="refctx-row" key={i}>
          <DocLink className="refctx-src" id={o.src_id} onOpen={() => peek.push({ kind: "doc", id: o.src_id })}>
            <Oscola c={o.src_oscola} fallback={o.src_title || o.src_id} /></DocLink>
          {o.snippet
            ? <div className="refctx-snippet">…{highlightSub(o.snippet, o.raw)}…</div>
            : <div className="refctx-snippet muted">(no snippet — cited as “{o.raw}”)</div>}
        </div>
      ))}
    </div>
  );
}

// mark the cited string inside its context snippet
function highlightSub(text: string, sub?: string) {
  if (!sub) return text;
  const i = text.toLowerCase().indexOf(sub.toLowerCase());
  if (i < 0) return text;
  return <>{text.slice(0, i)}<mark>{text.slice(i, i + sub.length)}</mark>{text.slice(i + sub.length)}</>;
}

function SuggestionRow({ s }: { s: any; onDone?: () => void }) {
  const [busy, setBusy] = useState(false);
  const [decided, setDecided] = useState<null | "accepted" | "rejected">(null);
  const [msg, setMsg] = useState("");
  const [showCtx, setShowCtx] = useState(false);
  // "…it's actually this OTHER thing" — link the reference to a document the suggester
  // never proposed. The search spans every jurisdiction (a UK case citing an Irish Act),
  // with a jurisdiction token in the dropdown so the right one is pickable with confidence.
  const [linkOther, setLinkOther] = useState(false);
  const linkTo = async (id: string, title: string) => {
    setBusy(true); setMsg("linking…");
    try {
      const r = await api.resolveReference({ ref: s.ref, existing_id: id });
      setDecided("accepted");
      setMsg(`✓ linked to ${title}${r.resolved_edges ? ` · resolved ${r.resolved_edges} edge(s)` : ""}`);
    } catch (e: any) { setMsg("error: " + e); }
    setBusy(false);
  };
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
      <a className="mini-link" title="show the passages where the corpus cites this reference"
        onClick={() => setShowCtx((v) => !v)}>{showCtx ? "hide context" : "◎ context"}</a>{" "}
      {!decided && <>
        <button className="mini sug-yes" disabled={busy} title="yes — link every citation of this reference to it"
          onClick={() => decide(true)}>✓</button>{" "}
        <button className="mini sug-no" disabled={busy} title="no — never suggest this again"
          onClick={() => decide(false)}>✗</button>{" "}
        <a className="mini-link" title="none of the above — link this reference to a different document you pick (searches every jurisdiction)"
          onClick={() => setLinkOther((v) => !v)}>{linkOther ? "cancel" : "↳ something else…"}</a>
      </>}
      {msg && <span className={msg.startsWith("error") ? "err" : "ok"} style={{ fontSize: 12 }}> {msg}</span>}
      {linkOther && !decided && (
        <div style={{ marginTop: 4 }}>
          <DocAutocomplete autoFocus placeholder="find the right case or act by name — any jurisdiction…"
            onPick={(id, title) => linkTo(id, title)} />
        </div>
      )}
      {showCtx && <RefContext refKey={s.ref} />}
    </div>
  );
}

// The full sweep-through list of every pending naming candidate, at the bottom of the
// page: tick/cross applies in place (no reload, no re-ranking), and "accept all" walks
// the whole list — deferring the resolver to ONE pass at the end.
// One flag chip with its explanation on hover. Red = strong evidence the match is
// wrong (cross-jurisdiction); amber = judge with care (year slip, initials-only name).
function FlagChip({ f }: { f: { id: string; level: string; note: string } }) {
  const LABEL: Record<string, string> = {
    "series-jurisdiction": "wrong jurisdiction?", "citing-jurisdiction": "wrong jurisdiction?",
    year: "year mismatch", "weak-name": "initials-only name",
  };
  return <span className={`sug-flag sug-flag-${f.level}`} title={f.note}>{LABEL[f.id] || f.id}</span>;
}

const SUG_KIND_LABEL: Record<string, string> = {
  "legislation-nested": "shorthand title", "legislation-year": "year slip",
  "case-name": "case name", "echr-name": "ECtHR name",
};

function AllSuggestionsPanel() {
  const [data, err] = useAsync(() => api.pendingSuggestions(500), []);
  // decision state lives HERE, keyed per suggestion, so deciding never re-fetches the list
  const [state, setState] = useState<Record<string, { s: string; note?: string }>>({});
  const [busy, setBusy] = useState(false);
  const [kindFilter, setKindFilter] = useState("");
  const [hideFlagged, setHideFlagged] = useState(false);
  const [ctxFor, setCtxFor] = useState<string | null>(null);   // row whose context is expanded
  // 500 groups rendered a 60,722px panel on a phone. The bulk "accept all safe" / "reject
  // all red-flagged" buttons still act on EVERY match, not just the shown ones — this is a
  // rendering limit, not a change of scope.
  const revealGroups = useRevealer(20);
  const rows: any[] = data?.suggestions || [];
  const key = (s: any) => `${s.ref} ${s.suggested_id}`;
  const isRed = (s: any) => (s.flags || []).some((f: any) => f.level === "red");
  const isPending = (s: any) => !state[key(s)] || state[key(s)].s === "pending";

  // group by suggested target: reviewing "37 refs, all → Income and Corporation
  // Taxes Act 1988" is one decision, not 37. Groups ranked by how much of the
  // corpus each would resolve (occurrences × best score).
  const groups = useMemo(() => {
    const by: Record<string, any[]> = {};
    for (const s of rows) {
      if (kindFilter && s.kind !== kindFilter) continue;
      if (hideFlagged && isRed(s)) continue;
      (by[s.suggested_id || s.ref] ||= []).push(s);
    }
    const gs = Object.entries(by).map(([gid, members]) => ({
      gid, members,
      impact: members.reduce((a, s) => a + (s.occurrences || 1) * (s.score ?? 0.5), 0),
      target: members.find((s) => s.target)?.target,
    }));
    gs.sort((a, b) => b.impact - a.impact);
    return gs;
  }, [rows, kindFilter, hideFlagged]);

  async function decideMany(items: any[], accept: boolean) {
    if (!items.length) return;
    setBusy(true);
    setState((st) => {
      const n = { ...st };
      for (const s of items) n[key(s)] = { s: "busy" };
      return n;
    });
    try {
      const r = await api.decideSuggestionsBulk(
        items.map((s) => ({ ref: s.ref, suggested_id: s.suggested_id, accept })));
      const note = accept
        ? `✓${r.resolved_edges ? ` (${r.resolved_edges} edges resolved)` : ""}`
        : "✗ dismissed";
      setState((st) => {
        const n = { ...st };
        for (const s of items) n[key(s)] = { s: accept ? "accepted" : "rejected", note };
        return n;
      });
    } catch (e: any) {
      setState((st) => {
        const n = { ...st };
        for (const s of items) n[key(s)] = { s: "pending", note: "error: " + (e.message || e) };
        return n;
      });
    }
    setBusy(false);
  }

  async function decideOne(s: any, accept: boolean) {
    const k = key(s);
    setState((st) => ({ ...st, [k]: { s: "busy" } }));
    try {
      const r = await api.decideSuggestion(s.ref, s.suggested_id, accept);
      const note = accept
        ? `✓${r.resolved_edges ? ` resolved ${r.resolved_edges}` : " linked"}` +
          (r.harvest ? (r.harvest.stored ? " · fetched" : r.harvest.error ? ` · fetch failed` : "") : "")
        : "✗ dismissed";
      setState((st) => ({ ...st, [k]: { s: accept ? "accepted" : "rejected", note } }));
    } catch (e: any) {
      setState((st) => ({ ...st, [k]: { s: "pending", note: "error: " + (e.message || e) } }));
    }
  }

  if (err || !rows.length) return null;
  const visible = groups.flatMap((g) => g.members);
  const safePending = visible.filter((s) => isPending(s) && !isRed(s));
  const redPending = rows.filter((s) => isPending(s) && isRed(s));
  const kinds = [...new Set(rows.map((s) => s.kind).filter(Boolean))] as string[];
  const groupReveal = revealGroups(groups);
  return (
    <div className="panel">
      <div className="row" style={{ alignItems: "baseline", flexWrap: "wrap" }}>
        <h3 style={{ marginTop: 0, flex: 1 }}>Naming candidates
          <span className="muted"> — every pending “Possibly: …?” suggestion, grouped by the document it would link to. Red chips mean the evidence points the other way; ◎ shows the citing passages.</span>
          {data?.total != null && <span className="tag" style={{ marginLeft: 8 }}>{data.total.toLocaleString()}</span>}
        </h3>
        <select className="theme-select" value={kindFilter} onChange={(e) => setKindFilter(e.target.value)}
          title="filter by suggestion kind">
          <option value="">all kinds</option>
          {kinds.map((k) => <option key={k} value={k}>{SUG_KIND_LABEL[k] || k}</option>)}
        </select>
        <label style={{ display: "flex", alignItems: "center", gap: 4, fontSize: 13, flex: "0 0 auto" }}>
          <input type="checkbox" style={{ width: "auto" }} checked={hideFlagged}
            onChange={(e) => setHideFlagged(e.target.checked)} /> hide red-flagged
        </label>
        <button className="mini sug-yes" style={{ flex: "0 0 auto" }} disabled={busy || !safePending.length}
          title="accept every visible suggestion WITHOUT a red flag — one call, one resolver pass"
          onClick={() => decideMany(safePending, true)}>✓ accept all safe ({safePending.length})</button>
        <button className="mini sug-no" style={{ flex: "0 0 auto" }} disabled={busy || !redPending.length}
          title="reject every red-flagged suggestion (cross-jurisdiction evidence) — never suggested again"
          onClick={() => decideMany(redPending, false)}>✗ reject all red-flagged ({redPending.length})</button>
      </div>
      {groupReveal.shown.map((g) => {
        const pend = g.members.filter(isPending);
        const t = g.target;
        const occ = g.members.reduce((a, s) => a + (s.occurrences || 0), 0);
        return (
          <div key={g.gid} className="sug-group">
            <div className="row sug-group-head" style={{ alignItems: "baseline", flexWrap: "wrap" }}>
              <b style={{ flex: 1 }}>
                {t?.title || g.members[0].context || g.gid}
                <span className="muted" style={{ fontWeight: 400 }}>
                  {t?.jurisdiction && <> · {t.jurisdiction}</>}
                  {t?.court_label && <> · {t.court_label}</>}
                  {t?.date && <> · {t.date}</>}
                  {t?.doc_type && <> · {t.doc_type}</>}
                  {!g.members[0].held && <> · <i>not held — accepting fetches it</i></>}
                </span>
              </b>
              <span className="muted" style={{ fontSize: 12, flex: "0 0 auto" }}>
                {g.members.length > 1 ? `${g.members.length} refs · ` : ""}{occ ? `cited ${occ}×` : ""}
              </span>
              {g.members.length > 1 && pend.length > 0 && <>
                <button className="mini sug-yes" disabled={busy} style={{ flex: "0 0 auto" }}
                  title="accept every reference in this group"
                  onClick={() => decideMany(pend, true)}>✓ all {pend.length}</button>
                <button className="mini sug-no" disabled={busy} style={{ flex: "0 0 auto" }}
                  title="reject every reference in this group"
                  onClick={() => decideMany(pend, false)}>✗ all</button>
              </>}
            </div>
            <table className="grid sug-table">
              <tbody>
                {g.members.map((s) => {
                  const k = key(s);
                  const st = state[k];
                  const done = st && (st.s === "accepted" || st.s === "rejected");
                  const jurs = Object.entries(s.citing_jurisdictions || {}) as [string, number][];
                  return (
                    <Fragment key={k}>
                    <tr style={st?.s === "rejected" ? { opacity: 0.5 } : undefined}>
                      <td style={{ fontFamily: "var(--mono, monospace)", fontSize: 12, width: "34%" }}>{s.ref}
                        {s.occurrences > 0 && <span className="muted"
                          title={`cited from: ${jurs.map(([j, n]) => `${j} ×${n}`).join(", ") || "?"}`}>
                          {" "}×{s.occurrences}</span>}
                      </td>
                      <td className="muted" style={{ fontSize: 12 }}>{s.reason}
                        {s.score != null && ` · ${Number(s.score).toFixed(2)}`}
                        {s.extracted_parties && <Info t={`auto-extracted parties: ${s.extracted_parties}`} />}
                        {" "}<a className="mini-link" title="the passages where the corpus cites this reference"
                          onClick={() => setCtxFor(ctxFor === k ? null : k)}>{ctxFor === k ? "hide" : "◎"}</a>
                      </td>
                      <td style={{ width: 1, whiteSpace: "nowrap" }}>
                        {(s.flags || []).map((f: any) => <FlagChip key={f.id} f={f} />)}
                      </td>
                      <td style={{ whiteSpace: "nowrap", width: 1 }}>
                        {!done && <>
                          <button className="mini sug-yes" disabled={st?.s === "busy" || busy}
                            title="yes — link every citation of this reference to it"
                            onClick={() => decideOne(s, true)}>✓</button>{" "}
                          <button className="mini sug-no" disabled={st?.s === "busy" || busy}
                            title="no — never suggest this again"
                            onClick={() => decideOne(s, false)}>✗</button>
                        </>}
                        {st?.s === "busy" && <span className="muted" style={{ fontSize: 12 }}> …</span>}
                        {st?.note && <span className={st.note.startsWith("error") ? "err" : "ok"} style={{ fontSize: 12 }}> {st.note}</span>}
                      </td>
                    </tr>
                    {ctxFor === k && <tr><td colSpan={4}><RefContext refKey={s.ref} /></td></tr>}
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
          </div>
        );
      })}
      {groupReveal.more}
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
function CorpusMap({ cov, navigate, showWorklist }: { cov: any; navigate?: (f: Record<string, string>) => void;
  showWorklist?: (bucket: "pending" | "cooling" | "name_only", category?: string, subtype?: string) => void }) {
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

  const refreshTable = async () => {
    setMsg("↻ refreshing the table…");
    try { await api.refreshCorpusMap(); reload(); setMsg(""); }
    catch (e: any) { setMsg("✗ " + e); }
  };

  return (
    <div className="panel corpus-map">
      <div className="row" style={{ alignItems: "baseline" }}>
        <h3 style={{ marginTop: 0, flex: 1 }}>Corpus map <span className="muted">— what we hold, what we’re missing, and what each part cites</span></h3>
        <button className="mini" style={{ flex: "0 0 auto" }} disabled={warming}
          title="Recompute the table in the background (it's cached and warmed on startup, so this is only needed to reflect a very recent harvest)."
          onClick={refreshTable}>↻ refresh table</button>
      </div>
      <div className="row stat-strip" style={{ flexWrap: "wrap", gap: 20 }}>
        <div><b>{(totals.held ?? s.total ?? 0).toLocaleString()}</b><div className="muted">held <Info t="Documents currently in the corpus." /></div></div>
        <div><b><a onClick={() => showWorklist?.("pending")}>{(totals.pending ?? cov?.routable_references ?? 0).toLocaleString()}</a></b><div className="muted">pending <Info t="Click for the routable untried references and identifiers planned for retrieval." /></div></div>
        <div><b><a onClick={() => showWorklist?.("cooling")}>{(totals.cooling ?? 0).toLocaleString()}</a></b><div className="muted">cooling <Info t="Click for references parked after a recent source miss or temporary retrieval failure." /></div></div>
        <div><b><a onClick={() => showWorklist?.("name_only")}>{(totals.name_only ?? cov?.needs_identifier ?? 0).toLocaleString()}</a></b><div className="muted">name-only <Info t="Click for recognised references that still need a canonical identifier or manual link." /></div></div>
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
            <th className="num">pending <Info t="Routable, cited-but-not-held, and untried — one click to harvest." /></th>
            <th className="num">cooling <Info t="Tried recently and parked on a retry/miss cool-down. ‘Harvest all’ re-attempts these." /></th>
            <th className="num">name-only <Info t="Recognised but not auto-fetchable; need a human." /></th>
            <th className="actions-h">actions</th>
          </tr></thead>
          <tbody>
            {(map.categories || []).map((c: any) => {
              const isOpen = open.has(c.key);
              return (
                <Fragment key={c.key}>
                  <tr className="cat-row">
                    <td>
                      <a className="caret" onClick={() => toggle(c.key)}>{isOpen ? "▾" : "▸"}</a>
                      <b>{c.label}</b>
                    </td>
                    <td className="num">{navigate && c.held ? <a onClick={() => see({ source: c.key })}>{c.held.toLocaleString()}</a> : c.held.toLocaleString()}</td>
                    <td className="num">{c.pending ? <a onClick={() => showWorklist?.("pending", c.key)}>{c.pending.toLocaleString()}</a> : <span className="muted">0</span>}</td>
                    <td className="num">{c.cooling ? <a onClick={() => showWorklist?.("cooling", c.key)}>{c.cooling.toLocaleString()}</a> : <span className="muted">0</span>}</td>
                    <td className="num">{c.name_only ? <a onClick={() => showWorklist?.("name_only", c.key)}>{c.name_only.toLocaleString()}</a> : <span className="muted">0</span>}</td>
                    <td className="actions">
                      {c.pending > 0 && c.key !== "other" && <button className="mini" title={`Harvest the ${c.pending} UNTRIED routable references in this category (runs in the background, cancellable, skips items that fail)`} onClick={() => fireJob("harvest-all", { adapter: c.key, limit: 1000000 }, setMsg)}>⤓ untried ({c.pending.toLocaleString()})</button>}
                      {(c.cooling > 0 || (c.pending > 0 && c.key !== "other")) && c.key !== "other" && <button className="mini" title={`Harvest ALL ${(c.pending + c.cooling).toLocaleString()} routable references in this category, including the ${c.cooling.toLocaleString()} on a retry/miss cool-down (re-attempts items previously parked as unavailable)`} onClick={() => fireJob("harvest-all", { adapter: c.key, limit: 1000000, retry_cooled: true }, setMsg)}>⤓ all ({(c.pending + c.cooling).toLocaleString()})</button>}
                    </td>
                  </tr>
                  {isOpen && (c.subtypes || []).map((st: any) => (
                    <tr key={c.key + ":" + st.key} className="sub-row">
                      <td className="sub-label">{st.label}</td>
                      <td className="num">{navigate && st.held && Object.keys(st.filter || {}).length ? <a onClick={() => see(st.filter)}>{st.held.toLocaleString()}</a> : st.held.toLocaleString()}</td>
                      <td className="num">{st.pending ? <a onClick={() => showWorklist?.("pending", c.key, st.key)}>{st.pending.toLocaleString()}</a> : <span className="muted">0</span>}</td>
                      <td className="num">{st.cooling ? <a onClick={() => showWorklist?.("cooling", c.key, st.key)}>{st.cooling.toLocaleString()}</a> : <span className="muted">0</span>}</td>
                      <td className="num">{st.name_only ? <a onClick={() => showWorklist?.("name_only", c.key, st.key)}>{st.name_only.toLocaleString()}</a> : <span className="muted">0</span>}</td>
                      <td className="actions">
                        {st.pending > 0 && c.key === "uk-legislation" &&
                          <button className="mini" title={`Harvest the ${st.pending} untried references of this type`} onClick={() => fireJob("harvest-all", { adapter: "uk-legislation", leg_kind: st.key.split(":")[0], limit: 1000000 }, setMsg)}>⤓ untried ({st.pending.toLocaleString()})</button>}
                        {st.cooling > 0 && c.key === "uk-legislation" &&
                          <button className="mini" title={`Harvest all ${(st.pending + st.cooling).toLocaleString()} references of this type, including the ${st.cooling.toLocaleString()} cooling off`} onClick={() => fireJob("harvest-all", { adapter: "uk-legislation", leg_kind: st.key.split(":")[0], limit: 1000000, retry_cooled: true }, setMsg)}>⤓ all ({(st.pending + st.cooling).toLocaleString()})</button>}
                      </td>
                    </tr>
                  ))}
                  {isOpen && (
                    <tr className="cites-row"><td colSpan={6}><CitesPanel category={c.key} /></td></tr>
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
        <td><code>{r.raw || r.ref}</code>{r.pinpoint && <span className="muted"> ◆ {r.pinpoint}</span>}
          {r.candidate && <div className="muted" style={{ fontSize: 11 }}>planned identifier: <code>{r.candidate}</code></div>}
          {r.cooling_reason && <div className="muted" style={{ fontSize: 11 }}>cooling: {r.cooling_reason}</div>}</td>
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
                  <DocAutocomplete autoFocus placeholder="find the case or act by name…"
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
              <DocLink key={d} id={d} onOpen={() => open(d)} style={{ marginRight: 8 }}>{d}</DocLink>
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
  const revealRules = useRevealer(15);
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
          {revealRules<any>(rules || []).shown.map((r: any) => (
            <tr key={r.phrase}>
              <td><b>{r.phrase}</b></td>
              <td>→ <DocLink id={r.target_id} onOpen={() => open(r.target_id)}>{r.target_id}</DocLink>
                {!r.target_present && <span className="err" title="target not yet in the corpus"> · not harvested</span>}</td>
              <td><a onClick={async () => { await api.deleteAlias(r.phrase); reload(); }} style={{ cursor: "pointer" }}>✗ delete</a></td>
            </tr>
          ))}
        </tbody></table>
        {revealRules<any>(rules || []).more}
      </div>
    </div>
  );
}

// --- Outstanding amendments (the legislation.gov.uk editorial lag) ----------
// Prominent currency banner for a piece of legislation, read from its change-graph edges
// (source-agnostic: UK amendments + EU repeals/corrigenda/consolidations). A user browsing
// an old act sees at a glance whether it's still good law and what changed it.
// Provision-level currency chip (article / section / § / artikel), with its in-force window
// and the instruments that changed it. Degrades: shows whatever the source pinpointed.
const PROV_TONE: Record<string, string> = {
  in_force: "leg-info", amended: "leg-amended", corrected: "leg-corrected",
  repealed: "leg-repealed", recast: "leg-repealed", expired: "leg-repealed",
  prospective: "leg-info", partially_in_force: "leg-amended", consolidated: "leg-info",
};
function ProvisionRow({ p, open, actId }:
  { p: any; open: (id: string, a?: string) => void; actId: string }) {
  const tone = PROV_TONE[p.status] || "leg-info";
  const window = [p.in_force_from, p.in_force_to].filter(Boolean).join(" → ");
  return (
    <div className="leg-prov">
      <DocLink className="leg-prov-anchor" id={actId} anchor={p.anchor} onOpen={() => open(actId, p.anchor)}
      title="jump to this provision">{p.anchor}</DocLink>
      {p.status && <span className={`leg-dot ${tone}`} title={p.native_status || p.status}>{(p.status as string).replace(/_/g, " ")}</span>}
      {window && <span className="muted leg-prov-win">{window}</span>}
      {(p.change_types || []).length > 0 && <span className="muted"> · {(p.change_types as string[]).join(", ")}</span>}
      {(p.changed_by || []).map((c: string) => (
        <DocLink key={c} className="leg-prov-by" id={c} onOpen={() => open(c)} title="the instrument that changed it">{c}</DocLink>))}
    </div>
  );
}

// Unified legislative-currency card (§CUR). One shape across UK / EU / FR / DE / NL / AU / NZ:
// a status chip (is this still good law?), the key dates, what amended/repealed/recast it, and
// — where the source pinpoints it — per-provision markers. Quiet for a plainly-in-force act
// with nothing to report; expands with detail when there's something a reader needs to know.
function LegStatusBanner({ id, open }: { id: string; open: (id: string, a?: string) => void }) {
  const [s, _statusError, reloadStatus] = useAsync(() => api.legislativeStatus(id), [id]);
  const [allProv, setAllProv] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [refreshNote, setRefreshNote] = useState("");
  if (!s) return null;
  const links = (ids: string[]) => ids.map((x, i) => (
    <Fragment key={x}>{i > 0 && ", "}<DocLink id={x} onOpen={() => open(x)}>{x}</DocLink></Fragment>));
  const lines: any[] = [];
  if (s.repealed_by?.length) lines.push(<span key="rep"><b>Repealed / recast</b> by {links(s.repealed_by)}</span>);
  if (s.amended_by?.length) lines.push(<span key="am"><b>Amended</b> by {links(s.amended_by)}</span>);
  if (s.corrected_by?.length) lines.push(<span key="corr">Corrected by {links(s.corrected_by)}</span>);
  if (s.repeals?.length) lines.push(<span key="rps" className="muted">Repeals / recasts {links(s.repeals)}</span>);
  if (s.legal_basis?.length) lines.push(<span key="lb" className="muted">Legal basis: {links(s.legal_basis)}</span>);

  let versionNotice: any;
  if (s.is_point_in_time) {
    versionNotice = <><b>Point-in-time text{s.point_in_time_date ? ` as at ${s.point_in_time_date}` : ""}.</b>
      {" "}This is not the undated/current record{s.point_in_time_of ? <>; base instrument: {links([s.point_in_time_of])}</> : "."}</>;
  } else if (s.version_state === "future_consolidation") {
    versionNotice = <><b>Future consolidated snapshot{s.as_at ? ` as at ${s.as_at}` : ""}.</b>
      {" "}It is not yet the latest applicable text
      {s.latest_applicable_consolidation ? <>; latest applicable consolidation held by RagLex: {links([s.latest_applicable_consolidation.stable_id])}</> : "."}</>;
  } else if (s.version_state === "latest_applicable_consolidation") {
    versionNotice = <><b>Latest applicable consolidation held by RagLex{s.as_at ? ` — ${s.as_at}` : ""}.</b>
      {s.latest_held_consolidation?.stable_id !== id && <> A newer future snapshot is also held: {links([s.latest_held_consolidation.stable_id])}.</>}</>;
  } else if (s.version_state === "historical_consolidation") {
    versionNotice = <><b>Historical consolidated snapshot{s.as_at ? ` — ${s.as_at}` : ""}.</b>
      {" "}A newer applicable consolidation is held: {links([s.latest_applicable_consolidation.stable_id])}.</>;
  } else if (s.version_state === "unverified_consolidation") {
    versionNotice = <><b>Consolidated snapshot{s.as_at ? ` as at ${s.as_at}` : ""}.</b>
      {" "}RagLex cannot confirm from its held version set that this is the latest.</>;
  } else if (s.version_state === "base_with_consolidation") {
    versionNotice = <><b>This is the base act, not a dated consolidated snapshot.</b>
      {" "}Latest applicable consolidation held by RagLex: {links([s.latest_applicable_consolidation.stable_id])}.</>;
  } else if (s.version_state === "revised_in_place") {
    // legislation.gov.uk revises the text in place, so this IS the consolidated text —
    // the question a reader has is not "which snapshot is this" but "how current is it".
    const pit = (s.point_in_time_versions || []) as any[];
    versionNotice = <><b>Revised text{s.as_at ? `, as it stood on ${s.as_at}` : ""}.</b>
      {" "}The publisher maintains this text in place, applying amendments as they are made;
      it is the current consolidated version, not the act as originally enacted.
      {!s.as_at && <> RagLex has not recorded the date this copy is current as at.</>}
      {pit.length > 0 && <> RagLex also holds {pit.length} point-in-time snapshot{pit.length > 1 ? "s" : ""}: {links(pit.map((v) => v.stable_id))}.</>}</>;
  } else if (s.source === "eu-legislation") {
    versionNotice = <><b>This is the undated/base EU act, not a dated consolidated snapshot.</b>
      {" "}RagLex has not yet imported a dated consolidation for this act; that does not mean none exists in EUR-Lex.
      {s.consolidation_sync
        ? <> A deduplicated Cellar lookup has started automatically.</>
        : s.consolidations_checked_at
          ? <> Cellar was last checked at {String(s.consolidations_checked_at).slice(0, 10)} and RagLex still holds none.</>
          : <> A Cellar lookup will start automatically.</>}</>;
  } else {
    versionNotice = <><b>This is an undated legislation record, not a dated snapshot.</b>
      {" "}RagLex has not imported a dated consolidation for this act; this is a corpus-coverage statement, not a claim that the official source has none.</>;
  }

  const provisions = (s.provisions || []) as any[];
  const dates = [
    s.in_force_from && `in force from ${s.in_force_from}`,
    s.in_force_to && `${s.status === "prospective" ? "until" : "ceased"} ${s.in_force_to}`,
    // the revised-text notice above already gives the as-at date in words
    s.as_at && !s.is_consolidation && s.version_state !== "revised_in_place"
      && `text as at ${s.as_at}`,
  ].filter(Boolean).join(" · ");
  const unapplied = s.up_to_date === false && (s.unapplied_count || 0) > 0;

  // Stay quiet only when there is genuinely nothing to say: plainly in force, no dates, no
  // amendments, no provisions, and a confirmed (non-degraded) status.
  const nothingToReport = !versionNotice && !lines.length && !provisions.length && !dates && !unapplied
    && (s.status === "in_force") && !s.degraded && !s.point_in_time_capable;
  if (nothingToReport) return null;

  const tone = s.status_tone || "leg-info";
  const icon = s.status_icon || "ℹ️";
  const label = s.status_label || "Status";
  const shown = allProv ? provisions : provisions.slice(0, 8);
  return (
    <div className={`leg-status ${tone}`}>
      <div className="leg-status-head">
        <span className={`leg-chip ${tone}`}>{icon} {label}</span>
        {s.native_status && <span className="leg-native" title={`source status (${s.scheme || ""})`}>{s.native_status}</span>}
        {unapplied && <span className="leg-chip leg-amended" title="changes known to the source but not yet written into this text">⚠ {s.unapplied_count} unapplied</span>}
        {/* Amended, with no consolidation to diff against: the count is unknown, not
            zero, and the held text may state the opposite of the operative law. */}
        {s.amendments_uncomparable && <span className="leg-chip leg-amended" title={s.currency_note || ""}>⚠ amendments not applied to this text</span>}
        {s.point_in_time_capable && <span className="muted leg-pit">point-in-time available</span>}
        {s.degraded && <span className="muted" title="status inferred from absence of recorded changes; not confirmed by the source">· unconfirmed</span>}
      </div>
      {versionNotice && <div className="leg-version-state">{versionNotice}</div>}
      {s.source === "uk-legislation" && <div className="leg-status-line muted">
        Official rendition last modified: {s.source_last_modified
          ? String(s.source_last_modified) : "not recorded on the last fetch"}
        {s.raglex_fetched_at && <> · fetched by RagLex {String(s.raglex_fetched_at).slice(0, 19).replace("T", " ")} UTC</>}
        {" · "}<button disabled={refreshing} onClick={async () => {
          setRefreshing(true); setRefreshNote("");
          try {
            const result = await api.refreshUkLegislation(id);
            setRefreshNote(result.error ? `refresh failed: ${result.error}`
              : result.changed ? "refreshed — source changed" : "refreshed — source unchanged");
            if (!result.error) reloadStatus();
          } catch (e: any) { setRefreshNote(`refresh failed: ${e?.message || e}`); }
          finally { setRefreshing(false); }
        }}>{refreshing ? "Refreshing…" : "↻ Refresh from legislation.gov.uk"}</button>
        {refreshNote && <> · {refreshNote}</>}
      </div>}
      {s.currency_note && <div className="leg-version-state">{s.currency_note}</div>}
      {dates && <div className="leg-status-line muted">{dates}</div>}
      {lines.map((b, i) => <div key={i} className="leg-status-line">{b}</div>)}
      {provisions.length > 0 && (
        <div className="leg-provisions">
          <div className="muted leg-prov-head">Provision-level status{provisions.length > 8 ? ` (${provisions.length})` : ""}</div>
          {shown.map((p) => <ProvisionRow key={p.anchor} p={p} open={open} actId={id} />)}
          {provisions.length > 8 && (
            <a className="muted" onClick={() => setAllProv(!allProv)}>{allProv ? "show fewer" : `show all ${provisions.length}`}</a>)}
        </div>
      )}
    </div>
  );
}

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
              ? <DocLink id={aff} onOpen={() => open(aff)}>{aff} ✓</DocLink>
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
  const [visibleChanges, changesMore] = useShowMore((changes || []) as any[]);
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
      {visibleChanges.map((c: any, i: number) => (
        <div key={i} style={{ fontSize: 13 }}>
          <DocLink id={c.affected_id} onOpen={() => open(c.affected_id)}>{c.affected_title || c.affected_id}</DocLink>
          {c.affected_provision && <span className="muted"> · {c.affected_provision}</span>}
          {c.effect_type && <span className="tag" style={{ marginLeft: 6 }}>{c.effect_type}</span>}
        </div>
      ))}
      {changesMore}
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
      <h3>Dated versions <span className="muted">— consolidations and point-in-time text linked to this instrument</span></h3>
      {data?.can_fetch_point_in_time && <div className="row" style={{ flexWrap: "wrap", alignItems: "center" }}>
        <input value={date} onChange={(e) => setDate(e.target.value)} placeholder="YYYY-MM-DD" style={{ maxWidth: 150 }} />
        <button onClick={fetchAt}>Show as at this date</button>
        {msg && <span className={msg.startsWith("error") ? "err" : "ok"} style={{ fontSize: 12 }}>{msg}</span>}
      </div>}
      {versions.length > 0 && <p className="muted" style={{ marginTop: 6 }}>held versions: {versions.map((v: any) => (
        <DocLink key={v.stable_id} id={v.stable_id} onOpen={() => open(v.stable_id)} style={{ marginRight: 10 }}>{v.date || v.stable_id}</DocLink>
      ))}</p>}
    </div>
  );
}

function ProvisionMappingPanel({ id, open }: { id: string; open: (id: string, a?: string) => void }) {
  const [data, _e, reload] = useAsync(() => api.provisionMappings(id), [id]);
  const [previous, setPrevious] = useState("");
  const [rows, setRows] = useState("");
  const [kind, setKind] = useState("functional_predecessor");
  const [msg, setMsg] = useState("");
  const mappings = data?.mappings || [];
  const [visibleMappings, mappingsMore] = useShowMore(mappings as any[]);
  const save = async () => {
    const parsed = rows.split(/\r?\n/).map((line) => {
      const parts = line.split(/\s*(?:=>|=|\t)\s*/, 2);
      return parts.length === 2
        ? { current_anchor: parts[0].trim(), previous_anchor: parts[1].trim() } : null;
    }).filter((x): x is any => !!x?.current_anchor && !!x?.previous_anchor);
    if (!previous.trim() || !parsed.length) {
      setMsg("error: give the previous law id and one mapping per line: Article 6 = Article 15");
      return;
    }
    setMsg("saving…");
    try {
      const result = await api.saveProvisionMappings({
        current_id: id, previous_id: previous.trim(), mappings: parsed,
        created_by: "manual", mapping_type: kind,
      });
      if (result.error) setMsg("error: " + result.error);
      else { setMsg(`✓ saved ${result.written} mapping(s)`); setRows(""); reload(); }
    } catch (e: any) { setMsg("error: " + e.message); }
  };
  return (
    <div className="panel">
      <h3>Provision lineage <span className="muted">— corresponding provisions in other laws</span></h3>
      <p className="muted" style={{ fontSize: 12 }}>
        Direction is this/current law → the other law. This does not rewrite old citations:
        it surfaces them separately as inherited context. Paste a whole correlation table,
        one <span className="kbd">current = other</span> pair per line. The kind is part of
        the claim: <b>earlier iteration</b> makes the other law's citers read as this
        provision's history; <b>parallel provision</b> is for a companion instrument in
        force alongside (GDPR / EUDPR / LED), which never became this one.
      </p>
      <div className="row" style={{ alignItems: "flex-start", flexWrap: "wrap" }}>
        <select value={kind} onChange={(e) => setKind(e.target.value)} style={{ flex: "0 0 auto" }}>
          <option value="functional_predecessor">earlier iteration (predecessor)</option>
          <option value="equivalent">parallel provision (companion instrument)</option>
        </select>
        <input value={previous} onChange={(e) => setPrevious(e.target.value)}
          placeholder="other law id, e.g. 31995L0046" style={{ minWidth: 250 }} />
        <textarea value={rows} onChange={(e) => setRows(e.target.value)}
          placeholder={"Article 16 = Article 12\nArticle 17 = Article 14"}
          rows={4} style={{ minWidth: 330, flex: 1 }} />
        <button className="primary" onClick={save}>Add mappings</button>
      </div>
      {msg && <p className={msg.startsWith("error") ? "err" : "ok"}>{msg}</p>}
      {mappings.length > 0 && <table className="grid"><thead><tr>
        <th>current provision</th><th>corresponds to</th><th>kind</th><th>inherited mentions</th><th></th>
      </tr></thead><tbody>{visibleMappings.map((m: any) => <tr key={m.mapping_id}>
        <td>{m.current_anchor}</td>
        <td><DocLink id={m.previous_doc_id} anchor={m.previous_anchor}
          onOpen={() => open(m.previous_doc_id, m.previous_anchor)}>
          {m.previous_title || m.previous_doc_id} · {m.previous_anchor}</DocLink>
          {m.note && <div className="muted">{m.note}</div>}</td>
        <td className="muted">{mappingKind(m.mapping_type).row}</td>
        <td>{m.mentioned_by_count || 0}</td>
        <td><a style={{ cursor: "pointer" }} onClick={async () => {
          await api.deleteProvisionMapping(m.mapping_id); reload();
        }}>✗</a></td>
      </tr>)}</tbody></table>}
      {mappingsMore}
    </div>
  );
}

// --- ⌘K citation jump palette ----------------------------------------------
// Paste or type ANY citation form — "[2020] UKSC 5", "C-311/18", "ECLI:EU:C:2020:559",
// "DPA 2018 s. 45" — or a case/act name. Citations are grammar-recognised and resolved
// server-side (the same ladder the reader uses); names go through the corpus
// autocomplete. Enter opens the document at the pinpoint. The fastest navigation
// primitive in the app.
export function CommandPalette({ open }: { open: (id: string, a?: string) => void }) {
  const [show, setShow] = useState(false);
  const [q, setQ] = useState("");
  const [cites, setCites] = useState<any[]>([]);
  const [docs, setDocs] = useState<any[]>([]);
  const [hi, setHi] = useState(0);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    const down = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault(); setShow((s) => !s); setQ(""); setCites([]); setDocs([]); setHi(0);
      } else if (e.key === "Escape") setShow(false);
    };
    window.addEventListener("keydown", down);
    return () => window.removeEventListener("keydown", down);
  }, []);
  useEffect(() => {
    if (!show) return;
    const text = q.trim();
    if (text.length < 3) { setCites([]); setDocs([]); return; }
    let live = true;
    setBusy(true);
    const t = setTimeout(async () => {
      try {
        const [scan, corpus] = await Promise.all([
          api.scanCitations(text).catch(() => ({ citations: [] })),
          api.searchCorpus({ query: text, limit: "6", facets: "false" }).catch(() => ({ items: [] })),
        ]);
        if (!live) return;
        // dedupe recognised citations by target; resolved first
        const seen = new Set<string>();
        const cs = (scan.citations || []).filter((c: any) => {
          const k = c.resolved_id || c.candidate_id || c.raw;
          if (!k || seen.has(k)) return false;
          seen.add(k); return true;
        }).sort((a: any, b: any) => (a.resolved_id ? 0 : 1) - (b.resolved_id ? 0 : 1)).slice(0, 5);
        setCites(cs); setDocs(corpus.items || []); setHi(0);
      } finally { if (live) setBusy(false); }
    }, 180);
    return () => { live = false; clearTimeout(t); };
  }, [q, show]);
  if (!show) return null;
  const options: { kind: "cite" | "doc"; c?: any; d?: any }[] = [
    ...cites.map((c) => ({ kind: "cite" as const, c })),
    ...docs.map((d) => ({ kind: "doc" as const, d })),
  ];
  const pick = (o: (typeof options)[number]) => {
    if (!o) return;
    if (o.kind === "doc") { open(o.d.stable_id); setShow(false); return; }
    const c = o.c;
    if (c.resolved_id) { open(c.resolved_id, c.pinpoint || undefined); setShow(false); }
  };
  return (
    <div className="palette-backdrop" onMouseDown={(e) => { if (e.target === e.currentTarget) setShow(false); }}>
      <div className="palette" role="dialog" aria-label="jump to citation">
        <input autoFocus value={q} placeholder="Jump to… a citation ([2020] UKSC 5, C-311/18, DPA 2018 s 45) or a name"
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "ArrowDown") { e.preventDefault(); setHi((h) => Math.min(h + 1, options.length - 1)); }
            else if (e.key === "ArrowUp") { e.preventDefault(); setHi((h) => Math.max(h - 1, 0)); }
            else if (e.key === "Enter") { e.preventDefault(); pick(options[hi]); }
          }} />
        {busy && <div className="palette-note muted">searching…</div>}
        {!busy && q.trim().length >= 3 && options.length === 0 && (
          <div className="palette-note muted">nothing recognised — try a citation or more of the name</div>)}
        {cites.length > 0 && <div className="palette-sect muted">citations recognised</div>}
        {cites.map((c, i) => (
          <div key={"c" + i} className={`palette-opt${hi === i ? " hi" : ""}`}
            onMouseEnter={() => setHi(i)} onMouseDown={(e) => { e.preventDefault(); pick(options[i]); }}>
            <b>{c.raw}</b>
            {c.pinpoint && <span className="muted"> · {c.pinpoint}</span>}
            {c.resolved_id
              ? <span className="palette-go"> → open{c.pinpoint ? " at pinpoint" : ""}</span>
              : <span className="muted"> · not held{c.candidate_id ? ` (${c.candidate_id})` : ""}</span>}
          </div>
        ))}
        {docs.length > 0 && <div className="palette-sect muted">documents</div>}
        {docs.map((d, i) => {
          const oi = cites.length + i;
          return (
            <div key={"d" + i} className={`palette-opt${hi === oi ? " hi" : ""}`}
              onMouseEnter={() => setHi(oi)} onMouseDown={(e) => { e.preventDefault(); pick(options[oi]); }}>
              <Oscola c={d.oscola} fallback={d.title || d.stable_id} />
              <span className="muted"> · {d.doc_type}{d.court ? " · " + d.court : ""}{String(d.decision_date || d.effective_date || "") ? " · " + String(d.decision_date || d.effective_date).slice(0, 4) : ""}</span>
            </div>
          );
        })}
        <div className="palette-hint muted">↑↓ choose · Enter open · Esc close</div>
      </div>
    </div>
  );
}

// --- Citation hover cards ---------------------------------------------------
// Event-delegated: ONE document-level listener serves every resolved citation link
// (a.cite[data-doc]) in any reader/peek/tray. 300ms intent delay, metadata cached
// per target, card follows the link. Answers "do I care about this authority?" in
// under a second, without opening anything.
const _hoverCache = new Map<string, any>();
export function CiteHoverLayer() {
  const [card, setCard] = useState<{ id: string; pin?: string; x: number; y: number; d?: any } | null>(null);
  const timer = useRef<number>(0);
  useEffect(() => {
    const over = (e: MouseEvent) => {
      const a = (e.target as HTMLElement)?.closest?.("a.cite[data-doc]") as HTMLElement | null;
      window.clearTimeout(timer.current);
      if (!a) { setCard(null); return; }
      const id = a.getAttribute("data-doc")!;
      const pin = a.getAttribute("data-pin") || undefined;
      const rect = a.getBoundingClientRect();
      timer.current = window.setTimeout(async () => {
        const x = Math.min(rect.left, window.innerWidth - 380);
        const y = rect.bottom + 6;
        setCard({ id, pin, x, y, d: _hoverCache.get(id) });
        if (!_hoverCache.has(id)) {
          try {
            const d = await api.document(id);
            _hoverCache.set(id, d);
            setCard((c) => (c && c.id === id ? { ...c, d } : c));
          } catch { /* leave the skeleton */ }
        }
      }, 280);
    };
    const out = () => { window.clearTimeout(timer.current); };
    const scroll = () => setCard(null);
    document.addEventListener("mouseover", over);
    document.addEventListener("mouseout", out);
    window.addEventListener("scroll", scroll, true);
    return () => {
      document.removeEventListener("mouseover", over);
      document.removeEventListener("mouseout", out);
      window.removeEventListener("scroll", scroll, true);
    };
  }, []);
  if (!card) return null;
  const d = card.d?.document;
  return (
    <div className="cite-card" style={{ left: card.x, top: Math.min(card.y, window.innerHeight - 150) }}>
      {!card.d && <div className="muted">…</div>}
      {card.d && (
        <>
          <div className="cite-card-title"><Oscola c={card.d.oscola} fallback={d?.title || card.id} /></div>
          <div className="muted" style={{ fontSize: 12 }}>
            {card.d.court_label || d?.court || card.d.source_label || d?.source}
            {card.d.jurisdiction ? ` · ${card.d.jurisdiction}` : ""}
            <DocDate d={d} />
            {d?.doc_type ? ` · ${d.doc_type}` : ""}
          </div>
          <div className="muted" style={{ fontSize: 12 }}>
            {card.d.cited_by_count ? `cited by ${card.d.cited_by_count.toLocaleString()}` : "not yet cited in the corpus"}
            {card.pin ? ` · pinpoint: ${card.pin}` : ""}
          </div>
          <div className="cite-card-hint muted">click the citation to preview</div>
        </>
      )}
    </div>
  );
}

// --- Reader minimap ----------------------------------------------------------
// A thin document-length strip beside the reader (VS Code's overview ruler for
// judgments): every recognised citation is a tick (coloured by state), headings are
// wider marks, and paragraphs other documents cite are accent marks. The viewport
// is a draggable window; click anywhere to jump. Indispensable at 400 paragraphs.
function Minimap({ containerRef, segs, cites, mentionAnchors, textLen }:
  { containerRef: any; segs: any[]; cites: any[]; mentionAnchors: Set<string>; textLen: number }) {
  const [view, setView] = useState<{ top: number; h: number }>({ top: 0, h: 100 });
  const stripRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    let raf = 0;
    const measure = () => {
      const el = containerRef.current;
      if (!el) return;
      const rect = el.getBoundingClientRect();
      const total = rect.height || 1;
      const visTop = Math.max(0, -rect.top);
      const visPx = Math.max(0, Math.min(window.innerHeight, rect.bottom) - Math.max(0, rect.top));
      setView({ top: (visTop / total) * 100, h: Math.max(4, (visPx / total) * 100) });
    };
    const onScroll = () => { cancelAnimationFrame(raf); raf = requestAnimationFrame(measure); };
    measure();
    window.addEventListener("scroll", onScroll, true);
    window.addEventListener("resize", onScroll);
    return () => { window.removeEventListener("scroll", onScroll, true); window.removeEventListener("resize", onScroll); cancelAnimationFrame(raf); };
  }, [containerRef, textLen]);
  if (!textLen || !segs?.length || segs.length < 25) return null;
  const pct = (chr: number) => Math.min(100, (chr / textLen) * 100);
  const jump = (e: any) => {
    const rect = stripRef.current!.getBoundingClientRect();
    const frac = (e.clientY - rect.top) / rect.height;
    const chr = frac * textLen;
    // nearest segment at/after that char offset
    let best = segs[0];
    for (const s of segs) { if (s.char_start <= chr) best = s; else break; }
    scrollToSeg(segId(best.label));
  };
  return (
    <div className="minimap" ref={stripRef} onMouseDown={jump} title="document overview — click to jump">
      {segs.filter((s) => isHeading(s)).map((s, i) => (
        <div key={"h" + i} className="mm-heading" style={{ top: `${pct(s.char_start)}%` }} />
      ))}
      {cites.map((c, i) => (
        <div key={"c" + i} className={`mm-cite mm-${c.state || (c.resolved_id ? "resolved" : "maybe")}`}
          style={{ top: `${pct(c.char_start)}%` }} />
      ))}
      {segs.filter((s) => mentionAnchors.has(s.label)).map((s, i) => (
        <div key={"m" + i} className="mm-mention" style={{ top: `${pct(s.char_start)}%` }}
          title="other documents cite this paragraph" />
      ))}
      <div className="mm-view" style={{ top: `${view.top}%`, height: `${view.h}%` }} />
    </div>
  );
}

// --- Citation-network panels (design §3) -------------------------------------
// The citator strip: how this authority stands in the network — citation volume,
// recency, PageRank percentile, and the most significant citing documents.
// Deliberately NO treatment claims (followed/overruled) — not reliable yet.
export function CitatorStrip({ id }: { id: string }) {
  const [c] = useAsync(() => api.citator(id), [id]);
  const peek = usePeek();
  if (!c || c.error) return null;
  const cb = c.cited_by || {};
  const auth = c.authority;
  if (!cb.documents && !auth) return null;
  return (
    <div className="citator-strip">
      {auth?.percentile != null && auth.percentile >= 50 && (
        <span className="cit-stat" title={`PageRank over the citation network — above ${auth.percentile.toFixed(0)}% of cited documents`}>
          ◆ authority top {Math.max(1, Math.round(100 - auth.percentile))}%</span>
      )}
      {cb.documents > 0 && (
        <span className="cit-stat" title="distinct documents citing this one (excluding heuristic carry-forwards)">
          cited by {cb.documents.toLocaleString()}
          {cb.recent_documents > 0 && <span className="muted"> · {cb.recent_documents.toLocaleString()} in the last {cb.recent_years}y</span>}
          {cb.documents > 3 && cb.recent_documents === 0 && <span className="cit-quiet" title="no citations from recent documents — check whether it is still relied on"> · quiet recently</span>}
        </span>
      )}
      {(c.most_significant_citors || []).length > 0 && (
        <span className="cit-stat">
          <span className="muted">most significant citor:</span>{" "}
          <DocLink id={c.most_significant_citors[0].id}
            onOpen={() => peek.push({ kind: "doc", id: c.most_significant_citors[0].id })}>
            <Oscola c={c.most_significant_citors[0].oscola} fallback={c.most_significant_citors[0].title || c.most_significant_citors[0].id} /></DocLink>
        </span>
      )}
    </div>
  );
}

// Related documents via the citation network — co-citation ("often cited together")
// and bibliographic coupling ("relies on the same authorities"). Honest labels: each
// list says WHY it's related; neither claims semantic similarity.
export function RelatedPanel({ id, open }: { id: string; open: (id: string, a?: string) => void }) {
  const [data] = useAsync(() => api.related(id), [id]);
  if (!data || (!data.co_cited?.length && !data.coupled?.length)) return null;
  const List = ({ rows, why }: { rows: any[]; why: (r: any) => string }) => (
    <ul className="related-list">
      {rows.slice(0, 8).map((r: any, i: number) => (
        <li key={i}>
          <DocLink id={r.id} onOpen={() => open(r.id)}><Oscola c={r.oscola} fallback={r.title || r.id} /></DocLink>
          <span className="muted"> · {why(r)}{r.date ? ` · ${r.date.slice(0, 4)}` : ""}</span>
        </li>
      ))}
    </ul>
  );
  return (
    <div className="panel">
      <h3>Related in the citation network <span className="muted">— by citation behaviour, not text similarity</span></h3>
      <div className="grid2">
        {data.co_cited?.length > 0 && (
          <div>
            <h4 className="related-h" title="documents that citing documents tend to cite in the same breath as this one">Often cited together</h4>
            <List rows={data.co_cited} why={(r) => `together in ${r.n} citing doc${r.n === 1 ? "" : "s"}`} />
          </div>
        )}
        {data.coupled?.length > 0 && (
          <div>
            <h4 className="related-h" title="documents whose own citations overlap this one's — they rely on the same authorities">Relies on the same authorities</h4>
            <List rows={data.coupled} why={(r) => `${r.n} shared authorit${r.n === 1 ? "y" : "ies"}`} />
          </div>
        )}
      </div>
    </div>
  );
}
