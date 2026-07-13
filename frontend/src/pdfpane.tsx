// PDF pane — pdf.js (via react-pdf) with a grammar-linkified text layer.
// The point over a plain <iframe>: pdf.js renders a selectable text layer over the
// canvas, and we post-process it — each rendered page's text is reconstructed and
// sent to POST /citations/scan (the same grammars + resolver behind the extracted-
// text reader), and every matched span is wrapped as a live citation link. So the
// ORIGINAL document (an EDPB guideline, a styled judgment PDF) is as navigable as
// the extracted text: click "Article 17 of Regulation (EU) 2016/679" on the page
// image and the cited authority peeks open. Plus zoom/fit, outline jumps, internal
// PDF links, lazy page rendering, and download — all self-hosted (no CDN).
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Document, Outline, Page, pdfjs } from "react-pdf";
import "react-pdf/dist/Page/TextLayer.css";
import "react-pdf/dist/Page/AnnotationLayer.css";
import { api } from "./api";

pdfjs.GlobalWorkerOptions.workerSrc = new URL(
  "pdfjs-dist/build/pdf.worker.min.mjs", import.meta.url).toString();

const esc = (s: string) =>
  s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

// per-page: the text reconstructed from the text layer's items (offsets[i] = where
// item i starts in it) and the citation spans the scanner found in that text
type PageScan = { offsets: number[]; text: string; cites: any[] };

export function PdfPane({ id, onCite }: { id: string; onCite: (c: any) => void }) {
  const [blob, setBlob] = useState<Blob | null>(null);
  const [err, setErr] = useState("");
  const [numPages, setNumPages] = useState(0);
  const [zoom, setZoom] = useState(1);
  const [showOutline, setShowOutline] = useState(false);
  const wrapRef = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(0);
  const scansRef = useRef<Record<number, PageScan>>({});
  const [linked, setLinked] = useState(0);
  const pageRefs = useRef<Record<number, HTMLDivElement | null>>({});
  const heights = useRef<Record<number, number>>({});
  // windowed rendering: only pages near the viewport get a canvas + text layer
  const [visible, setVisible] = useState<Set<number>>(new Set([1, 2, 3]));

  useEffect(() => {
    let live = true;
    setBlob(null); setErr(""); setNumPages(0); setLinked(0);
    scansRef.current = {}; heights.current = {};
    setVisible(new Set([1, 2, 3]));
    api.fetchRaw(id).then((b) => { if (live) setBlob(b); })
      .catch((e) => { if (live) setErr("could not load the original: " + (e.message || e)); });
    return () => { live = false; };
  }, [id]);

  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setWidth(el.clientWidth - 20));
    ro.observe(el);
    setWidth(el.clientWidth - 20);
    return () => ro.disconnect();
  }, [blob]);

  useEffect(() => {
    if (!numPages) return;
    const io = new IntersectionObserver((entries) => {
      setVisible((old) => {
        const next = new Set(old);
        for (const e of entries) {
          if (!e.isIntersecting) continue;
          const n = Number((e.target as HTMLElement).dataset.page);
          for (let d = -2; d <= 2; d++) if (n + d >= 1 && n + d <= numPages) next.add(n + d);
        }
        return next.size !== old.size ? next : old;
      });
    }, { rootMargin: "700px 0px" });
    Object.values(pageRefs.current).forEach((el) => el && io.observe(el));
    return () => io.disconnect();
  }, [numPages, blob]);

  const file = useMemo(() => blob, [blob]);
  const pageWidth = width ? Math.min(width, 1000) * zoom : undefined;

  // Reconstruct the page's text from its text-layer items, then scan it server-side.
  // Items are concatenated without separators (pdf.js items usually keep their own
  // trailing spaces, and a mid-word item split must not break a citation token).
  const onText = useCallback((pageNo: number, tc: any) => {
    if (scansRef.current[pageNo]) return;
    let t = "";
    const offsets: number[] = [];
    for (const it of tc.items || []) {
      offsets.push(t.length);
      t += it.str || "";
      if (it.hasEOL) t += "\n";
    }
    scansRef.current[pageNo] = { offsets, text: t, cites: [] };
    if (!t.trim()) return;
    api.scanCitations(t).then((r) => {
      const s = scansRef.current[pageNo];
      if (!s) return;
      s.cites = (r.citations || []).sort((a: any, b: any) => a.char_start - b.char_start);
      if (s.cites.length) setLinked((n) => n + s.cites.length);
    }).catch(() => { /* linkification is progressive enhancement */ });
  }, []);

  // customTextRenderer emits each text-layer item as an HTML string; slice the
  // item against the page's citation spans and wrap the overlaps in <mark>s.
  const renderItem = useCallback((pageNo: number, ti: { str: string; itemIndex: number }) => {
    const s = scansRef.current[pageNo];
    const str = ti.str || "";
    if (!s || !s.cites.length) return esc(str);
    const start = s.offsets[ti.itemIndex] ?? -1;
    if (start < 0) return esc(str);
    const end = start + str.length;
    let out = "";
    let cur = start;
    for (let ci = 0; ci < s.cites.length; ci++) {
      const c = s.cites[ci];
      if (c.char_end <= cur || c.char_start >= end) continue;
      const a = Math.max(c.char_start, cur), b = Math.min(c.char_end, end);
      out += esc(s.text.slice(cur, a));
      out += `<mark class="pdfcite cite-${c.state}" data-page="${pageNo}" data-ci="${ci}"` +
        ` title="${esc(c.raw)}">${esc(s.text.slice(a, b))}</mark>`;
      cur = b;
    }
    out += esc(s.text.slice(cur, end));
    return out;
  }, []);

  // the marks are injected HTML, so clicks are delegated from the scroll container
  const clickCapture = (e: React.MouseEvent) => {
    const m = (e.target as HTMLElement).closest?.("mark.pdfcite") as HTMLElement | null;
    if (!m) return;
    e.preventDefault();
    e.stopPropagation();
    const s = scansRef.current[Number(m.dataset.page)];
    const c = s?.cites[Number(m.dataset.ci)];
    if (c) onCite(c);
  };

  const jumpTo = (n?: number) =>
    n && pageRefs.current[n]?.scrollIntoView({ behavior: "smooth", block: "start" });

  if (err) return <p className="err">{err}</p>;
  if (!blob) return <p className="muted loading-pulse">loading original…</p>;
  return (
    <div className="pdfpane" ref={wrapRef}>
      <div className="pdfbar">
        <button className="mini" title="table of contents" onClick={() => setShowOutline((v) => !v)}>☰</button>
        <button className="mini" title="zoom out" onClick={() => setZoom((z) => Math.max(0.5, +(z - 0.15).toFixed(2)))}>−</button>
        <span className="muted" style={{ fontSize: 12, minWidth: 42, textAlign: "center" }}>{Math.round(zoom * 100)}%</span>
        <button className="mini" title="zoom in" onClick={() => setZoom((z) => Math.min(3, +(z + 0.15).toFixed(2)))}>+</button>
        <button className="mini" title="fit width" onClick={() => setZoom(1)}>fit</button>
        <span className="muted" style={{ fontSize: 12, flex: 1 }}>
          {numPages ? `${numPages} page${numPages === 1 ? "" : "s"}` : ""}
          {linked > 0 && ` · ${linked} citation${linked === 1 ? "" : "s"} linked`}
        </span>
        <button className="mini" title="download the original file" onClick={() => {
          const u = URL.createObjectURL(blob);
          const a = document.createElement("a");
          a.href = u; a.download = id.replace(/[/:]/g, "_") + ".pdf"; a.click();
          URL.revokeObjectURL(u);
        }}>⬇</button>
      </div>
      <div className="pdfscroll" onClickCapture={clickCapture}>
        <Document file={file}
          onLoadSuccess={(d) => setNumPages(d.numPages)}
          onLoadError={(e: any) => setErr("could not render the PDF: " + (e?.message || e))}
          onItemClick={({ pageNumber }) => jumpTo(pageNumber)}
          loading={<p className="muted loading-pulse">rendering…</p>}>
          {showOutline && (
            <div className="pdfoutline">
              <Outline onItemClick={({ pageNumber }) => jumpTo(pageNumber)} />
            </div>
          )}
          {Array.from({ length: numPages }, (_, i) => i + 1).map((n) => (
            <div key={n} data-page={n} className="pdfpage"
              ref={(el) => { pageRefs.current[n] = el; }}>
              {visible.has(n)
                ? <Page pageNumber={n} width={pageWidth}
                    renderAnnotationLayer renderTextLayer
                    onGetTextSuccess={(tc) => onText(n, tc)}
                    onRenderSuccess={() => {
                      const el = pageRefs.current[n];
                      if (el?.clientHeight) heights.current[n] = el.clientHeight;
                    }}
                    customTextRenderer={(ti: any) => renderItem(n, ti)} />
                : <div className="pdfph"
                    style={{ height: heights.current[n] || (pageWidth ? pageWidth * 1.35 : 900) }} />}
            </div>
          ))}
        </Document>
      </div>
    </div>
  );
}

// The stored original when it's an HTML page (a styled BAILII judgment): rendered
// in a sandboxed iframe — no scripts, no same-origin — because an uploaded page's
// JS must never run against the app's origin (it could read the API token).
export function HtmlPane({ id }: { id: string }) {
  const [url, setUrl] = useState("");
  const [err, setErr] = useState("");
  useEffect(() => {
    let obj = "";
    let live = true;
    setUrl(""); setErr("");
    api.fetchRaw(id).then((b) => {
      if (!live) return;
      obj = URL.createObjectURL(b.type ? b : new Blob([b], { type: "text/html" }));
      setUrl(obj);
    }).catch((e) => { if (live) setErr("could not load the original: " + (e.message || e)); });
    return () => { live = false; if (obj) URL.revokeObjectURL(obj); };
  }, [id]);
  if (err) return <p className="err">{err}</p>;
  if (!url) return <p className="muted loading-pulse">loading original…</p>;
  return <iframe className="origframe" sandbox="" title="original document" src={url} />;
}
