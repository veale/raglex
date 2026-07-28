import { useEffect, useRef, useState } from "react";
import { CiteHoverLayer, CommandPalette, Dashboard, DocumentView, EscapeCloser, ImportView, JobsPanel, MaintainView, PeekPanel, PeekProvider, SearchView, SettingsView, TrayProvider, TrayStack, UnresolvedView } from "./views";
import { ExploreView, SearchAdminView } from "./explore";
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
type AdminSection = "overview" | "unresolved" | "maintain" | "search" | "import";

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
      </div>
    </div>
  );
}

// Where the reader was before the current view — enough to put them back exactly
// where they left off, including how far down the page they had scrolled.
type ViewState = { tab: Tab; docId: string | null; graphId: string | null;
                   pinpoint: string | null; scrollY: number };

export function App() {
  const [tab, setTab] = useState<Tab>("explore");
  const [docId, setDocId] = useState<string | null>(null);
  const [graphId, setGraphId] = useState<string | null>(null);
  const [pinpoint, setPinpoint] = useState<string | null>(null);

  // A back stack, because navigation here is tab state rather than real routing:
  // opening a document from a search result would otherwise strand the reader with
  // no way back to the list they were working through.
  const [back, setBack] = useState<ViewState[]>([]);
  const [restoreTo, setRestoreTo] = useState<number | null>(null);
  const pushBack = () => setBack((b) =>
    [...b.slice(-19), { tab, docId, graphId, pinpoint, scrollY: window.scrollY }]);
  const goBack = () => {
    setBack((b) => {
      const prev = b[b.length - 1];
      if (!prev) return b;
      setTab(prev.tab); setDocId(prev.docId);
      setGraphId(prev.graphId); setPinpoint(prev.pinpoint);
      setRestoreTo(prev.scrollY);
      return b.slice(0, -1);
    });
  };
  // restore scroll only once the restored view has painted
  useEffect(() => {
    if (restoreTo === null) return;
    const y = restoreTo;
    setRestoreTo(null);
    requestAnimationFrame(() => requestAnimationFrame(() => window.scrollTo(0, y)));
  }, [restoreTo]);

  // open a document, optionally deep-linking to a pinpointed section (JADE-style)
  const open = (id: string, anchor?: string) => {
    if (!id) return;
    pushBack();
    setDocId(id); setPinpoint(anchor || null); setTab("document");
  };
  const openGraph = (id: string) => {
    if (!id) return;
    pushBack(); setGraphId(id); setTab("graph");
  };
  // jump to Search pre-filtered (Corpus Map "see this list") — nonce forces re-adopt
  const [corpusFilter, setCorpusFilter] = useState<Record<string, string>>({});
  const navigateCorpus = (f: Record<string, string>) => {
    pushBack();
    setCorpusFilter({ ...f, _n: String(Date.now()) }); setTab("search");
  };
  const goSearch = (q?: string) => navigateCorpus(q ? { query: q } : {});
  // A top-level nav click starts a fresh view → land at the TOP of the page. Without this,
  // switching from a scrolled-down Explore into Admin kept the old scrollY and dropped you
  // into the middle of the Admin page. (The back-arrow still restores its saved scroll.)
  const goTab = (t: Tab) => { setTab(t); window.scrollTo(0, 0); };

  // which browse surfaces have been opened at least once (see the render below)
  const visited = useRef<Set<Tab>>(new Set(["explore"]));
  visited.current.add(tab);

  // Shareable deep links — every view the app can be IN has a URL, so it survives a
  // reload, a copied link, and above all being opened in a NEW TAB (which is just a fresh
  // load of that URL): #/article/{id}[/section/{anchor}], #/graph/{id}, #/{tab}.
  useEffect(() => {
    const apply = () => {
      const h = decodeURIComponent(location.hash.replace(/^#\/?/, ""));
      const art = h.match(/^article\/(.+?)(?:\/section\/(.+))?$/);
      if (art) { setDocId(art[1]); setPinpoint(art[2] || null); setTab("document"); return; }
      const gr = h.match(/^graph\/(.+)$/);
      if (gr) { setGraphId(gr[1]); setTab("graph"); return; }
      if (["explore", "search", "admin", "settings"].includes(h)) setTab(h as Tab);
    };
    apply();
    window.addEventListener("hashchange", apply);
    return () => window.removeEventListener("hashchange", apply);
  }, []);
  useEffect(() => {
    // replaceState, not push: the app keeps its own back stack (which restores scroll
    // position), and one history entry per view would fight it.
    const want = tab === "document" && docId ? docHref(docId, pinpoint)
      : tab === "graph" && graphId ? graphHref(graphId)
      : (tab === "explore" || tab === "search" || tab === "admin" || tab === "settings")
        ? `#/${tab}` : null;
    if (want && location.hash !== want) history.replaceState(null, "", want);
    else if (!want && location.hash.startsWith("#/")) {
      history.replaceState(null, "", location.pathname + location.search);
    }
  }, [tab, docId, graphId, pinpoint]);

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
  return (
    <PeekProvider>
    <TrayProvider>
    <div className="app">
      <header>
        {back.length > 0 && (
          <button className="back-arrow" onClick={goBack}
            title={`Back to ${back[back.length - 1].tab === "document" ? "the previous document" : back[back.length - 1].tab}`}
            aria-label="Back">←</button>
        )}
        <h1 onClick={() => setTab("explore")} style={{ cursor: "pointer" }} title="Explore">RagLex</h1>
        <ApiStatus />
        <nav>
          {tabs.map(([t, label]) => (
            <button key={t} className={tab === t ? "active" : ""} onClick={() => goTab(t)}>{label}</button>
          ))}
          {docId && (tab === "document" || tab === "graph") &&
            <button className={tab === "document" ? "active" : ""} onClick={() => setTab("document")}>Document</button>}
          {graphId && (tab === "document" || tab === "graph") &&
            <button className={tab === "graph" ? "active" : ""} onClick={() => setTab("graph")}>Graph</button>}
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
          <SearchView open={open} initialFilter={corpusFilter} />
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
