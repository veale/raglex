import { useEffect, useRef, useState } from "react";
import { CiteHoverLayer, CommandPalette, Dashboard, DocumentView, EscapeCloser, ImportView, JobsPanel, MaintainView, PeekPanel, PeekProvider, SettingsView, StaticExportView, TrayProvider, TrayStack, UnresolvedView } from "./views";
import { ExploreView, SearchAdminView, SearchPage } from "./explore";
import { GraphView } from "./graph";
import { useState as useReactState } from "react";
import { api } from "./api";
import { useAuth } from "./auth";
import { docHref, graphHref } from "./links";

// A compact role badge + sign-out in the header, shown only when auth is enforced. Admins
// see nothing to elevate; a reader can sign out and back in as admin.
function AuthBadge() {
  const { enforced, role, logout } = useAuth();
  if (!enforced) return null;
  return (
    <span className="auth-badge" title={`Signed in as ${role}`}>
      <span className={`auth-role auth-role-${role}`}>{role}</span>
      <button className="auth-signout" onClick={() => logout()} title="Sign out">sign out</button>
    </span>
  );
}

// Tiny live connection indicator in the header — so a slow first query (cold DB after a
// restart) reads as "connecting", never a frozen blank app.
function ApiStatus() {
  const [up, setUp] = useReactState<boolean | null>(null);
  useEffect(() => {
    let live = true;
    const ping = async () => {
      try { await api.health(); if (live) setUp(true); } catch { if (live) setUp(false); }
    };
    ping();
    const iv = setInterval(ping, 5000);
    return () => { live = false; clearInterval(iv); };
  }, []);
  const label = up === null ? "connecting…" : up ? "connected" : "offline";
  const cls = up === null ? "api-status connecting" : up ? "api-status up" : "api-status down";
  return <span className={cls} title="API connection">● {label}</span>;
}

// Feedback widget — a top-right button that expands to submit a Bug / Feature request.
// Records the current page's context (route, open document, pinpoint, URL, viewport,
// user-agent) so an admin reviewing the feedback table knows exactly where it came from.
// Available to every role (readers included); the review queue itself is admin-only.
function FeedbackBox({ context }: { context: () => Record<string, unknown> }) {
  const [open, setOpen] = useReactState(false);
  const [kind, setKind] = useReactState<"bug" | "feature">("bug");
  const [msg, setMsg] = useReactState("");
  const [status, setStatus] = useReactState("");
  const [busy, setBusy] = useReactState(false);
  async function submit() {
    if (!msg.trim()) { setStatus("write something first"); return; }
    setBusy(true); setStatus("");
    try {
      const meta = context();
      const r = await api.submitFeedback({
        kind, message: msg.trim(),
        page: String(meta.page ?? ""),
        url: String(meta.url ?? (location.hash || location.pathname)),
        metadata: meta,
      });
      if (r.error) setStatus("error: " + r.error);
      else { setStatus("✓ thanks — recorded"); setMsg(""); setTimeout(() => { setOpen(false); setStatus(""); }, 1400); }
    } catch (e: any) { setStatus("error: " + (e.message || e)); }
    finally { setBusy(false); }
  }
  return (
    <div className="feedback-box">
      <button className="feedback-toggle" onClick={() => setOpen((o) => !o)}
        title="Report a bug or request a feature" aria-expanded={open}>
        {open ? "×" : "Feedback"}
      </button>
      {open && (
        <div className="feedback-panel" role="dialog" aria-label="Send feedback">
          <div className="feedback-kind">
            <button className={kind === "bug" ? "active" : ""} onClick={() => setKind("bug")}>🐛 Bug</button>
            <button className={kind === "feature" ? "active" : ""} onClick={() => setKind("feature")}>✨ Feature</button>
          </div>
          <textarea value={msg} onChange={(e) => setMsg(e.target.value)} rows={5} autoFocus
            placeholder={kind === "bug"
              ? "What went wrong, and what did you expect? (e.g. a citation linked to the wrong case)"
              : "What would help your research?"} />
          <div className="feedback-meta">on <code>{String(context().page ?? "")}</code> — page context is attached automatically</div>
          <div className="feedback-actions">
            <button className="primary" disabled={busy} onClick={submit}>{busy ? "Sending…" : "Send"}</button>
            {status && <span className={status.startsWith("error") ? "err" : "ok"} style={{ fontSize: 12 }}>{status}</span>}
          </div>
        </div>
      )}
    </div>
  );
}

const THEMES: [string, string][] = [
  ["latte", "Catppuccin Latte"], ["frappe", "Catppuccin Frappé"],
  ["macchiato", "Catppuccin Macchiato"], ["mocha", "Catppuccin Mocha"],
];

// Theme switcher — Catppuccin Latte (light) by default, with the three dark flavours.
// Persists to localStorage; index.html applies it before first paint to avoid a flash.
function ThemeSwitch() {
  const [theme, setTheme] = useReactState<string>(
    () => document.documentElement.getAttribute("data-theme") || "latte");
  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
    try { localStorage.setItem("raglex-theme", theme); } catch { /* ignore */ }
  }, [theme]);
  return (
    <select className="theme-select" value={theme} onChange={(e) => setTheme(e.target.value)}
      title="Colour theme" aria-label="Colour theme">
      {THEMES.map(([v, label]) => <option key={v} value={v}>{label}</option>)}
    </select>
  );
}

type Tab = "explore" | "search" | "admin" | "settings" | "document" | "graph";
type AdminSection = "overview" | "unresolved" | "maintain" | "search" | "import" | "export";

// slug/docHref live in links.tsx — the address bar this file writes and the href every
// link carries must come from the SAME function, or they disagree.

// One dense surface for everything operational: ops overview (source health,
// queues, corpus map), the unresolved queue, maintenance controls, and imports —
// previously four scattered tabs. A slim section rail keeps it compact; the
// admin-dense class tightens spacing for power use.
function AdminView({ open, navigate }:
  { open: (id: string, a?: string) => void; navigate: (f: Record<string, string>) => void }) {
  const [section, setSection] = useState<AdminSection>(
    () => (localStorage.getItem("raglex-admin-section") as AdminSection) || "overview");
  const pick = (s: AdminSection) => {
    setSection(s);
    window.scrollTo(0, 0);   // switching section starts a fresh page → top, not mid-scroll
    try { localStorage.setItem("raglex-admin-section", s); } catch { /* ignore */ }
  };
  const SECTIONS: [AdminSection, string, string][] = [
    ["overview", "Overview", "source health · queues · corpus map · jobs"],
    ["unresolved", "Unresolved", "hanging references · suggestions · frontiers"],
    ["maintain", "Maintain", "rescans · roll-ups · repairs · watches"],
    ["search", "Search", "free-text index · embeddings · scope · the note readers see"],
    ["import", "Import", "files · corpora · Zotero · seeds"],
    // Publishing is an operational surface, not a preference: it schedules work, runs
    // jobs and writes a folder. It lives here rather than in Settings for that reason.
    ["export", "Static export", "the published set · schedule · webhook"],
  ];
  return (
    <div className="admin admin-dense">
      <nav className="admin-rail" aria-label="admin sections">
        {SECTIONS.map(([key, label, hint]) => (
          <button key={key} className={section === key ? "on" : ""} title={hint}
            onClick={() => pick(key)}>{label}<span className="rail-hint">{hint}</span></button>
        ))}
      </nav>
      <div className="admin-body">
        {section === "overview" && <Dashboard open={open} navigate={navigate} />}
        {section === "unresolved" && <UnresolvedView open={open} navigate={navigate} />}
        {section === "maintain" && <MaintainView open={open} navigate={navigate} />}
        {section === "search" && <SearchAdminView />}
        {section === "import" && <ImportView open={open} />}
        {section === "export" && <StaticExportView />}
      </div>
    </div>
  );
}

// Where the reader was before the current view — enough to put them back exactly
// where they left off, including how far down the page they had scrolled.
type ViewState = { tab: Tab; docId: string | null; graphId: string | null;
                   pinpoint: string | null; scrollY: number };
type TrailEntry = { label: string; href: string };
type RaglexHistory = { index: number; view: ViewState; label: string };

function viewFromHash(): ViewState {
  const h = decodeURIComponent(location.hash.replace(/^#\/?/, ""));
  const art = h.match(/^article\/(.+?)(?:\/section\/(.+))?$/);
  if (art) return { tab: "document", docId: art[1], graphId: null,
                    pinpoint: art[2] || null, scrollY: 0 };
  const gr = h.match(/^graph\/(.+)$/);
  if (gr) return { tab: "graph", docId: null, graphId: gr[1], pinpoint: null, scrollY: 0 };
  const tab = (["explore", "search", "admin", "settings"].includes(h) ? h : "explore") as Tab;
  return { tab, docId: null, graphId: null, pinpoint: null, scrollY: 0 };
}

function viewHref(v: ViewState): string {
  if (v.tab === "document" && v.docId) return docHref(v.docId, v.pinpoint);
  if (v.tab === "graph" && v.graphId) return graphHref(v.graphId);
  return `#/${v.tab}`;
}

function viewLabel(v: ViewState): string {
  if (v.tab === "document") return `${v.docId || "document"}${v.pinpoint ? ` · ${v.pinpoint}` : ""}`;
  if (v.tab === "graph") return `graph · ${v.graphId || "document"}`;
  return v.tab.charAt(0).toUpperCase() + v.tab.slice(1);
}

export function App() {
  const [tab, setTab] = useState<Tab>("explore");
  const [docId, setDocId] = useState<string | null>(null);
  const [graphId, setGraphId] = useState<string | null>(null);
  const [pinpoint, setPinpoint] = useState<string | null>(null);

  // One history entry per view, with the exact page position stored on that entry.
  // Browser Back/Forward and the edge chevrons therefore use the same source of truth.
  const [trail, setTrail] = useState<TrailEntry[]>([]);
  const [trailIndex, setTrailIndex] = useState(0);
  const [restoreTo, setRestoreTo] = useState<number | null>(null);
  const currentRef = useRef<ViewState>({ tab, docId, graphId, pinpoint, scrollY: 0 });
  currentRef.current = { tab, docId, graphId, pinpoint, scrollY: window.scrollY };
  const trailRef = useRef<TrailEntry[]>(trail); trailRef.current = trail;
  const indexRef = useRef(trailIndex); indexRef.current = trailIndex;

  const adopt = (v: ViewState, restore = v.scrollY) => {
    setTab(v.tab); setDocId(v.docId); setGraphId(v.graphId); setPinpoint(v.pinpoint);
    setRestoreTo(restore);
  };
  const saveCurrentPosition = () => {
    const state = history.state?.raglex as RaglexHistory | undefined;
    if (!state) return;
    const view = { ...currentRef.current, scrollY: window.scrollY };
    history.replaceState({ ...history.state, raglex: { ...state, view } }, "", location.href);
  };
  const visit = (next: ViewState, replace = false) => {
    saveCurrentPosition();
    const href = viewHref(next), label = viewLabel(next);
    if (replace) {
      const i = indexRef.current;
      const nextTrail = trailRef.current.map((entry, n) => n === i ? { label, href } : entry);
      setTrail(nextTrail);
      history.replaceState({ ...history.state, raglex: { index: i, view: next, label } }, "", href);
    } else {
      const i = indexRef.current + 1;
      const nextTrail = [...trailRef.current.slice(0, i), { label, href }];
      setTrail(nextTrail); setTrailIndex(i);
      history.pushState({ raglex: { index: i, view: next, label } }, "", href);
    }
    adopt(next, 0);
  };
  // Restore after the target view has painted *and*, for async readers, after enough
  // body has arrived for that Y position to exist. Two animation frames was not enough:
  // a judgment briefly renders its header, clamps scrollTo(42000) near the top, then the
  // body arrives and leaves the reader at that arbitrary clamp point. Retry for up to
  // five seconds, stopping immediately once the exact position is attainable.
  useEffect(() => {
    if (restoreTo === null) return;
    const y = restoreTo;
    let frame = 0, attempts = 0, cancelled = false;
    const finish = () => { if (!cancelled) setRestoreTo(null); };
    const tick = () => {
      if (cancelled) return;
      window.scrollTo(0, y);
      if (Math.abs(window.scrollY - y) <= 2 || y === 0 || attempts++ >= 300) {
        finish();
        return;
      }
      frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(() => { frame = requestAnimationFrame(tick); });
    return () => { cancelled = true; cancelAnimationFrame(frame); };
  }, [restoreTo]);

  // open a document, optionally deep-linking to a pinpointed section (JADE-style)
  const open = (id: string, anchor?: string, replace = false) => {
    if (!id) return;
    visit({ tab: "document", docId: id, graphId: currentRef.current.graphId,
            pinpoint: anchor || null, scrollY: 0 }, replace);
  };
  const openGraph = (id: string) => {
    if (!id) return;
    visit({ tab: "graph", docId: currentRef.current.docId,
            graphId: id, pinpoint: null, scrollY: 0 });
  };
  // jump to Search pre-filtered (Corpus Map "see this list") — nonce forces re-adopt
  const [corpusFilter, setCorpusFilter] = useState<Record<string, string>>({});
  const navigateCorpus = (f: Record<string, string>) => {
    setCorpusFilter({ ...f, _n: String(Date.now()) });
    visit({ tab: "search", docId: null, graphId: null, pinpoint: null, scrollY: 0 });
  };
  const goSearch = (q?: string) => navigateCorpus(q ? { query: q } : {});
  // A top-level nav click starts a fresh view → land at the TOP of the page. Without this,
  // switching from a scrolled-down Explore into Admin kept the old scrollY and dropped you
  // into the middle of the Admin page. (The back-arrow still restores its saved scroll.)
  const goTab = (t: Tab) => {
    if (currentRef.current.tab === t && ["explore", "search", "admin", "settings"].includes(t)) {
      window.scrollTo(0, 0);
      return;
    }
    visit({ tab: t, docId: null, graphId: null, pinpoint: null, scrollY: 0 });
  };

  // which browse surfaces have been opened at least once (see the render below)
  const visited = useRef<Set<Tab>>(new Set(["explore"]));
  visited.current.add(tab);

  // Initialise the current browser entry, save its scroll continuously, and adopt
  // Back/Forward entries only after their view has painted. ``manual`` prevents the
  // browser racing React with its own restoration against a temporarily different DOM.
  useEffect(() => {
    history.scrollRestoration = "manual";
    const initial = viewFromHash(), label = viewLabel(initial);
    history.replaceState({ ...history.state,
      raglex: { index: 0, view: initial, label } satisfies RaglexHistory }, "", viewHref(initial));
    setTrail([{ label, href: viewHref(initial) }]);
    adopt(initial, 0);

    let frame = 0;
    const onScroll = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(saveCurrentPosition);
    };
    const onPop = (event: PopStateEvent) => {
      const state = event.state?.raglex as RaglexHistory | undefined;
      const next = state?.view || viewFromHash();
      if (state) setTrailIndex(state.index);
      adopt(next, next.scrollY || 0);
    };
    // A real <a href="#/…"> without an in-app click handler asks the browser to make
    // the entry. Adopt it into our trail after hashchange so Back/Forward still has a
    // destination label and a stored scroll position.
    const onHash = () => {
      if (history.state?.raglex) return;
      const next = viewFromHash(), i = indexRef.current + 1;
      const entry = { label: viewLabel(next), href: viewHref(next) };
      setTrail([...trailRef.current.slice(0, i), entry]);
      setTrailIndex(i);
      history.replaceState({ ...history.state,
        raglex: { index: i, view: next, label: entry.label } satisfies RaglexHistory },
        "", entry.href);
      adopt(next, 0);
    };
    window.addEventListener("scroll", onScroll, { passive: true });
    window.addEventListener("popstate", onPop);
    window.addEventListener("hashchange", onHash);
    return () => {
      cancelAnimationFrame(frame);
      window.removeEventListener("scroll", onScroll);
      window.removeEventListener("popstate", onPop);
      window.removeEventListener("hashchange", onHash);
      history.scrollRestoration = "auto";
    };
  }, []); // history wiring is intentionally installed once; refs carry current values

  // A reader gets a read-only research interface: no admin/maintain, no settings. These are
  // also enforced server-side (src/raglex/web/auth.py) — hiding them is only affordance.
  const { isAdmin, role } = useAuth();
  // page metadata attached to any feedback submitted from the current view
  const feedbackContext = () => ({
    page: tab === "document" && docId ? `document:${docId}` : tab,
    tab, docId, pinpoint,
    url: location.hash || location.pathname,
    role,
    viewport: `${window.innerWidth}x${window.innerHeight}`,
    userAgent: navigator.userAgent,
    ts: new Date().toISOString(),
  });
  const tabs: [Tab, string][] = isAdmin
    ? [["explore", "Explore"], ["search", "Search"], ["admin", "Admin"], ["settings", "Settings"]]
    : [["explore", "Explore"], ["search", "Search"]];
  const backDestination = trailIndex > 0 ? trail[trailIndex - 1] : null;
  const forwardDestination = trailIndex + 1 < trail.length ? trail[trailIndex + 1] : null;
  return (
    <PeekProvider>
    <TrayProvider>
    {backDestination && <button className="history-edge history-edge-back"
      onClick={() => { saveCurrentPosition(); history.back(); }}
      title={`Back to ${backDestination.label}`} aria-label={`Back to ${backDestination.label}`}>‹</button>}
    {forwardDestination && <button className="history-edge history-edge-forward"
      onClick={() => { saveCurrentPosition(); history.forward(); }}
      title={`Forward to ${forwardDestination.label}`} aria-label={`Forward to ${forwardDestination.label}`}>›</button>}
    <div className="app">
      <header>
        <h1 onClick={() => goTab("explore")} style={{ cursor: "pointer" }} title="Explore">RagLex</h1>
        <ApiStatus />
        <nav>
          {tabs.map(([t, label]) => (
            <button key={t} className={tab === t ? "active" : ""} onClick={() => goTab(t)}>{label}</button>
          ))}
          {docId && (tab === "document" || tab === "graph") &&
            <button className={tab === "document" ? "active" : ""}
              onClick={() => tab !== "document" && open(docId, pinpoint || undefined)}>Document</button>}
          {graphId && (tab === "document" || tab === "graph") &&
            <button className={tab === "graph" ? "active" : ""}
              onClick={() => tab !== "graph" && openGraph(graphId)}>Graph</button>}
        </nav>
        <ThemeSwitch />
        <AuthBadge />
        <FeedbackBox context={feedbackContext} />
      </header>
      {/* Explore and Search stay MOUNTED once visited, merely hidden — their
          results and facet state are local, so unmounting them would mean "back"
          returned to an empty search box instead of the list you were reading. */}
      {(tab === "explore" || visited.current.has("explore")) && (
        <div style={{ display: tab === "explore" ? undefined : "none" }}>
          <ExploreView open={open} goSearch={goSearch} />
        </div>
      )}
      {(tab === "search" || visited.current.has("search")) && (
        <div style={{ display: tab === "search" ? undefined : "none" }}>
          <SearchPage open={open} initialQuery={corpusFilter?.query} />
        </div>
      )}
      {tab === "admin" && isAdmin && <AdminView open={open} navigate={navigateCorpus} />}
      {tab === "settings" && isAdmin && <SettingsView />}
      {tab === "document" && docId && <DocumentView id={docId} open={open} openGraph={openGraph} pinpoint={pinpoint} />}
      {tab === "graph" && graphId && <GraphView focusId={graphId} open={open} />}
    </div>
    <PeekPanel open={open} />
    <TrayStack open={open} />
    <EscapeCloser />
    {isAdmin && <JobsPanel />}
    <CommandPalette open={open} />
    <CiteHoverLayer />
    </TrayProvider>
    </PeekProvider>
  );
}
