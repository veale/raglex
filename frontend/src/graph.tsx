import { useEffect, useRef, useState } from "react";
import cytoscape, { Core, ElementDefinition } from "cytoscape";
import { api } from "./api";

// Interactive citation-graph explorer (§8 signature view). Renders a server-
// computed neighbourhood (never the whole graph); click a node to expand its
// 1-hop neighbours, double-click to open the document. Edges are coloured/labelled
// by relationship_type; direction is encoded by arrow.

// Cytoscape can't read CSS custom properties, so resolve the current Catppuccin
// tokens to literal colours; re-resolved whenever the data-theme attribute changes.
function tok(name: string, fallback: string): string {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || fallback;
}
function buildStyle(): any[] {
  const text = tok("--ctp-text", "#e6e9ef"), sub = tok("--ctp-subtext0", "#9aa4b2"),
    line = tok("--ctp-surface2", "#3a4250"), node = tok("--ctp-overlay0", "#6e738d"),
    accent = tok("--ctp-blue", "#5b9dff"), ok = tok("--ctp-green", "#7ee787"),
    warn = tok("--ctp-peach", "#ffb454"), bad = tok("--ctp-red", "#ff6b6b");
  return [
    { selector: "node", style: {
        "background-color": node, "label": "data(label)", "color": text,
        "font-size": 9, "text-wrap": "wrap", "text-max-width": "120px", "width": 16, "height": 16,
        "text-valign": "center", "text-halign": "right", "text-margin-x": 3 } },
    { selector: "node.focus", style: { "background-color": accent, "width": 22, "height": 22 } },
    { selector: "node.legislation", style: { "background-color": ok, "shape": "round-rectangle" } },
    { selector: "edge", style: {
        "width": 1.2, "line-color": line, "target-arrow-color": sub,
        "target-arrow-shape": "triangle", "curve-style": "bezier",
        "label": "data(label)", "font-size": 7, "color": sub } },
    { selector: "edge.overrules", style: { "line-color": bad, "target-arrow-color": bad, "width": 2 } },
    { selector: "edge.distinguishes", style: { "line-color": warn, "target-arrow-color": warn, "width": 1.8 } },
    { selector: "edge.applies, edge.follows", style: { "line-color": ok, "target-arrow-color": ok, "width": 1.8 } },
    // pinpoint edges (a fragment → an article/section) drawn dashed
    { selector: "edge.pinpoint", style: { "line-style": "dashed" } },
    // LLM-inferred treatment marked with a dotted underlay so provenance is visible
    { selector: "edge.via-llm", style: { "line-style": "dotted" } },
  ];
}

export function GraphView({ focusId, open }: { focusId: string; open: (id: string) => void }) {
  const ref = useRef<HTMLDivElement>(null);
  const cyRef = useRef<Core | null>(null);
  const loaded = useRef<Set<string>>(new Set());
  const [status, setStatus] = useState("");

  async function expand(id: string, isFocus = false) {
    const cy = cyRef.current;
    if (!cy || loaded.current.has(id)) return;
    loaded.current.add(id);
    setStatus(`expanding ${id}…`);
    try {
      const g = await api.graph(id);
      const add: ElementDefinition[] = [];
      if (cy.getElementById(id).empty())
        add.push({ data: { id, label: id }, classes: isFocus ? "focus" : "" });
      for (const n of g.neighbours) {
        if (cy.getElementById(n.id).empty())
          add.push({ data: { id: n.id, label: n.title || n.id }, classes: n.id.match(/R\d|L\d|ukpga|uksi/) ? "legislation" : "" });
        const [s, t] = n.direction === "out" ? [id, n.id] : [n.id, id];
        const eid = `${s}->${t}:${n.relationship_type}`;
        if (cy.getElementById(eid).empty()) {
          // edge label carries the pinpoint anchor when present ("analyses ◆ Article 17")
          const anchor = n.dst_anchor || n.src_anchor;
          const label = anchor ? `${n.relationship_type} ◆ ${anchor}` : n.relationship_type;
          const classes = [n.relationship_type];
          if (n.dst_anchor || n.src_anchor) classes.push("pinpoint");
          if (n.extracted_via === "llm") classes.push("via-llm");
          add.push({ data: { id: eid, source: s, target: t, rel: n.relationship_type, label,
                             src_anchor: n.src_anchor, dst_anchor: n.dst_anchor,
                             via: n.extracted_via }, classes: classes.join(" ") });
        }
      }
      cy.add(add);
      cy.layout({ name: "cose", animate: false, padding: 30 } as any).run();
      setStatus(`${cy.nodes().length} nodes`);
    } catch (e) { setStatus("error: " + e); }
  }

  useEffect(() => {
    if (!ref.current) return;
    const cy = cytoscape({ container: ref.current, style: buildStyle(), elements: [], minZoom: 0.2, maxZoom: 3 });
    cyRef.current = cy;
    loaded.current = new Set();
    cy.on("tap", "node", (e) => expand(e.target.id()));
    cy.on("dbltap", "node", (e) => open(e.target.id()));
    expand(focusId, true);
    // follow theme switches live (the ThemeSwitch stamps data-theme on <html>)
    const mo = new MutationObserver(() => { try { (cy as any).style(buildStyle()); } catch { /* destroyed */ } });
    mo.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    return () => { mo.disconnect(); cy.destroy(); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focusId]);

  const key = (color: string, label: string) => <span style={{ color }}>{label}</span>;
  return (
    <div className="panel">
      <p className="muted">Graph from <b>{focusId}</b> — click a node to expand its citations, double-click to open. {status}</p>
      <div ref={ref} style={{ height: "clamp(420px, calc(100vh - 300px), 760px)",
        background: "var(--inset)", border: "1px solid var(--line)" }} />
      <p className="nbr">● case · {key("var(--ok)", "▭ legislation")} · {key("var(--ok)", "applies/follows")} · {key("var(--warn)", "distinguishes")} · {key("var(--bad)", "overrules")} · ◆ pinpoint · dotted = LLM-inferred</p>
    </div>
  );
}
