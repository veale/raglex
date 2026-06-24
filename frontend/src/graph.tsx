import { useEffect, useRef, useState } from "react";
import cytoscape, { Core, ElementDefinition } from "cytoscape";
import { api } from "./api";

// Interactive citation-graph explorer (§8 signature view). Renders a server-
// computed neighbourhood (never the whole graph); click a node to expand its
// 1-hop neighbours, double-click to open the document. Edges are coloured/labelled
// by relationship_type; direction is encoded by arrow.
const STYLE: any[] = [
  { selector: "node", style: {
      "background-color": "#2a3550", "label": "data(label)", "color": "#e6e9ef",
      "font-size": 9, "text-wrap": "wrap", "text-max-width": "120px", "width": 16, "height": 16,
      "text-valign": "center", "text-halign": "right", "text-margin-x": 3 } },
  { selector: "node.focus", style: { "background-color": "#5b9dff", "width": 22, "height": 22 } },
  { selector: "node.legislation", style: { "background-color": "#7ee787", "shape": "round-rectangle" } },
  { selector: "edge", style: {
      "width": 1.2, "line-color": "#3a4250", "target-arrow-color": "#566677",
      "target-arrow-shape": "triangle", "curve-style": "bezier",
      "label": "data(label)", "font-size": 7, "color": "#9aa4b2" } },
  { selector: "edge.overrules", style: { "line-color": "#ff6b6b", "target-arrow-color": "#ff6b6b", "width": 2 } },
  { selector: "edge.distinguishes", style: { "line-color": "#ffb454", "target-arrow-color": "#ffb454", "width": 1.8 } },
  { selector: "edge.applies, edge.follows", style: { "line-color": "#7ee787", "target-arrow-color": "#7ee787", "width": 1.8 } },
  // pinpoint edges (a fragment → an article/section) drawn dashed
  { selector: "edge.pinpoint", style: { "line-style": "dashed" } },
  // LLM-inferred treatment marked with a dotted underlay so provenance is visible
  { selector: "edge.via-llm", style: { "line-style": "dotted" } },
];

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
    const cy = cytoscape({ container: ref.current, style: STYLE, elements: [], minZoom: 0.2, maxZoom: 3 });
    cyRef.current = cy;
    loaded.current = new Set();
    cy.on("tap", "node", (e) => expand(e.target.id()));
    cy.on("dbltap", "node", (e) => open(e.target.id()));
    expand(focusId, true);
    return () => cy.destroy();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focusId]);

  return (
    <div className="panel">
      <p className="muted">Graph from <b>{focusId}</b> — click a node to expand its citations, double-click to open. {status}</p>
      <div ref={ref} style={{ height: 560, background: "#0c0e13", border: "1px solid var(--line)", borderRadius: 8 }} />
      <p className="nbr">● case · <span style={{ color: "#7ee787" }}>▭ legislation</span> · <span style={{ color: "#7ee787" }}>applies/follows</span> · <span style={{ color: "#ffb454" }}>distinguishes</span> · <span style={{ color: "#ff6b6b" }}>overrules</span> · ◆ pinpoint · dotted = LLM-inferred</p>
    </div>
  );
}
