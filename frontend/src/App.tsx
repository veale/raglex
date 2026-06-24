import { useEffect, useState } from "react";
import { CorpusView, Dashboard, DocumentView, ImportView, JobsPanel, PeekPanel, PeekProvider, RulesView, SearchView, SettingsView, UnresolvedView, WatchesView } from "./views";
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

type Tab = "dashboard" | "search" | "corpus" | "unresolved" | "rules" | "watches" | "import" | "settings" | "document" | "graph";

const slug = (s: string) => (s || "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");

export function App() {
  const [tab, setTab] = useState<Tab>("dashboard");
  const [docId, setDocId] = useState<string | null>(null);
  const [graphId, setGraphId] = useState<string | null>(null);
  const [pinpoint, setPinpoint] = useState<string | null>(null);
  // open a document, optionally deep-linking to a pinpointed section (JADE-style)
  const open = (id: string, anchor?: string) => {
    if (!id) return; setDocId(id); setPinpoint(anchor || null); setTab("document");
  };
  const openGraph = (id: string) => { if (!id) return; setGraphId(id); setTab("graph"); };
  // jump to the Corpus browser pre-filtered (Corpus Map "see this list" action)
  const [corpusFilter, setCorpusFilter] = useState<Record<string, string>>({});
  const navigateCorpus = (f: Record<string, string>) => { setCorpusFilter(f); setTab("corpus"); };

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
    }
  }, [tab, docId, pinpoint]);

  const tabs: [Tab, string][] = [
    ["dashboard", "Dashboard"], ["search", "Search"], ["corpus", "Corpus"],
    ["unresolved", "Unresolved"], ["rules", "Rules"], ["watches", "Watches"], ["import", "Import"], ["settings", "Settings"],
  ];
  return (
    <PeekProvider>
    <div className="app">
      <header>
        <h1>RagLex</h1>
        <ApiStatus />
        <nav>
          {tabs.map(([t, label]) => (
            <button key={t} className={tab === t ? "active" : ""} onClick={() => setTab(t)}>{label}</button>
          ))}
          {tab === "document" && <button className="active">Document</button>}
          {tab === "graph" && <button className="active">Graph</button>}
        </nav>
        <ThemeSwitch />
      </header>
      {tab === "dashboard" && <Dashboard open={open} />}
      {tab === "search" && <SearchView open={open} />}
      {tab === "corpus" && <CorpusView open={open} initialFilter={corpusFilter} />}
      {tab === "unresolved" && <UnresolvedView open={open} navigate={navigateCorpus} />}
      {tab === "rules" && <RulesView open={open} />}
      {tab === "watches" && <WatchesView />}
      {tab === "import" && <ImportView open={open} />}
      {tab === "settings" && <SettingsView />}
      {tab === "document" && docId && <DocumentView id={docId} open={open} openGraph={openGraph} pinpoint={pinpoint} />}
      {tab === "graph" && graphId && <GraphView focusId={graphId} open={open} />}
    </div>
    <PeekPanel open={open} />
    <JobsPanel />
    </PeekProvider>
  );
}
