import { useEffect, useState } from "react";
import { CiteHoverLayer, CommandPalette, Dashboard, DocumentView, EscapeCloser, ImportView, JobsPanel, MaintainView, PeekPanel, PeekProvider, SearchView, SettingsView, TrayProvider, TrayStack, UnresolvedView } from "./views";
import { ExploreView } from "./explore";
import { GraphView } from "./graph";
import { useState as useReactState } from "react";
import { api } from "./api";

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
type AdminSection = "overview" | "unresolved" | "maintain" | "import";

const slug = (s: string) => (s || "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");

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
    try { localStorage.setItem("raglex-admin-section", s); } catch { /* ignore */ }
  };
  const SECTIONS: [AdminSection, string, string][] = [
    ["overview", "Overview", "source health · queues · corpus map · jobs"],
    ["unresolved", "Unresolved", "hanging references · suggestions · frontiers"],
    ["maintain", "Maintain", "rescans · roll-ups · repairs · watches"],
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
        {section === "maintain" && <MaintainView open={open} />}
        {section === "import" && <ImportView open={open} />}
      </div>
    </div>
  );
}

export function App() {
  const [tab, setTab] = useState<Tab>("explore");
  const [docId, setDocId] = useState<string | null>(null);
  const [graphId, setGraphId] = useState<string | null>(null);
  const [pinpoint, setPinpoint] = useState<string | null>(null);
  // open a document, optionally deep-linking to a pinpointed section (JADE-style)
  const open = (id: string, anchor?: string) => {
    if (!id) return; setDocId(id); setPinpoint(anchor || null); setTab("document");
  };
  const openGraph = (id: string) => { if (!id) return; setGraphId(id); setTab("graph"); };
  // jump to Search pre-filtered (Corpus Map "see this list") — nonce forces re-adopt
  const [corpusFilter, setCorpusFilter] = useState<Record<string, string>>({});
  const navigateCorpus = (f: Record<string, string>) => { setCorpusFilter({ ...f, _n: String(Date.now()) }); setTab("search"); };
  const goSearch = (q?: string) => navigateCorpus(q ? { query: q } : {});

  // Shareable deep links: #/article/{id}[/section/{anchor}] ↔ the open document.
  useEffect(() => {
    const apply = () => {
      const h = decodeURIComponent(location.hash.replace(/^#\/?/, ""));
      const m = h.match(/^article\/(.+?)(?:\/section\/(.+))?$/);
      if (m) { setDocId(m[1]); setPinpoint(m[2] || null); setTab("document"); }
    };
    apply();
    window.addEventListener("hashchange", apply);
    return () => window.removeEventListener("hashchange", apply);
  }, []);
  useEffect(() => {
    if (tab === "document" && docId) {
      const want = `#/article/${encodeURIComponent(docId)}` + (pinpoint ? `/section/${slug(pinpoint)}` : "");
      if (location.hash !== want) history.replaceState(null, "", want);
    } else if (location.hash.startsWith("#/article")) {
      // leaving the document: drop the stale deep link so a reload lands where you are
      history.replaceState(null, "", location.pathname + location.search);
    }
  }, [tab, docId, pinpoint]);

  const tabs: [Tab, string][] = [
    ["explore", "Explore"], ["search", "Search"], ["admin", "Admin"], ["settings", "Settings"],
  ];
  return (
    <PeekProvider>
    <TrayProvider>
    <div className="app">
      <header>
        <h1 onClick={() => setTab("explore")} style={{ cursor: "pointer" }} title="Explore">RagLex</h1>
        <ApiStatus />
        <nav>
          {tabs.map(([t, label]) => (
            <button key={t} className={tab === t ? "active" : ""} onClick={() => setTab(t)}>{label}</button>
          ))}
          {docId && (tab === "document" || tab === "graph") &&
            <button className={tab === "document" ? "active" : ""} onClick={() => setTab("document")}>Document</button>}
          {graphId && (tab === "document" || tab === "graph") &&
            <button className={tab === "graph" ? "active" : ""} onClick={() => setTab("graph")}>Graph</button>}
        </nav>
        <ThemeSwitch />
      </header>
      {tab === "explore" && <ExploreView open={open} goSearch={goSearch} />}
      {tab === "search" && <SearchView open={open} initialFilter={corpusFilter} />}
      {tab === "admin" && <AdminView open={open} navigate={navigateCorpus} />}
      {tab === "settings" && <SettingsView />}
      {tab === "document" && docId && <DocumentView id={docId} open={open} openGraph={openGraph} pinpoint={pinpoint} />}
      {tab === "graph" && graphId && <GraphView focusId={graphId} open={open} />}
    </div>
    <PeekPanel open={open} />
    <TrayStack open={open} />
    <EscapeCloser />
    <JobsPanel />
    <CommandPalette open={open} />
    <CiteHoverLayer />
    </TrayProvider>
    </PeekProvider>
  );
}
