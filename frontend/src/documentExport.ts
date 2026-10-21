export type ExportFormat = "rich" | "markdown" | "plain" | "latex" | "rtf";

export type SourceBody = {
  text?: string | null;
  doc_type?: string | null;
  segments?: ExportSegment[];
  title?: string | null;
};

export type ExportSegment = {
  label: string;
  kind: string;
  level: number;
  char_start: number;
  char_end: number;
  lines?: ExportLine[];
  formatting?: { char_start: number; char_end: number; kind: string }[];
};

export type ExportLine = {
  start: number;
  end: number;
  depth?: number;
  anchor?: string;
};

export type ExportItem = {
  id: string;
  label: string;
  reference: string;
  kind: string;
  level: number;
  source: number;
  segment: number;
  line?: number;
  start: number;
  end: number;
  depth: number;
  text: string;
  formatting: ExportSegment["formatting"];
  lines?: ExportLine[];
  parentId?: string;
};

export type ExportModel = {
  sources: SourceBody[];
  items: ExportItem[];
};

export type ExportOptions = {
  title: string;
  citation: string;
  gapMarker: string;
  includeTitle: boolean;
  includeCitation: boolean;
};

type ExportBlock = {
  kind: "content" | "gap";
  item?: ExportItem;
  heading?: string;
  paragraphs?: { text: string; depth: number; start: number; end: number }[];
};

const KIND_WORDS: Record<string, string> = {
  article: "article", art: "article", arts: "article", articles: "article",
  recital: "recital", rec: "recital", recs: "recital", recitals: "recital",
  section: "section", sec: "section", secs: "section", sections: "section", s: "section", ss: "section",
  paragraph: "paragraph", para: "paragraph", paras: "paragraph", paragraphs: "paragraph",
  regulation: "regulation", reg: "regulation", regs: "regulation", regulations: "regulation",
  rule: "rule", rules: "rule", schedule: "schedule", sch: "schedule", schs: "schedule", schedules: "schedule",
  annex: "annex", annexes: "annex", point: "point", points: "point", pt: "point", pts: "point",
};

const TYPE_PREFIX: Record<string, string> = {
  article: "Article", recital: "Recital", section: "Section", paragraph: "Paragraph",
  regulation: "Regulation", rule: "Rule", schedule: "Schedule", annex: "Annex", point: "Point",
};

function cleanKind(kind: string): string {
  return KIND_WORDS[kind.toLowerCase()] || kind.toLowerCase();
}

function numberedReference(segment: ExportSegment): string {
  const label = String(segment.label || "").trim();
  const typed = /^(articles?|arts?|recitals?|recs?|sections?|secs?|paragraphs?|paras?|regulations?|regs?|rules?|schedules?|schs?|annex(?:es)?|points?|pts?)\.?\s+([0-9]+(?:\.[0-9]+)*[a-z]*(?:\([^\s()]+\))*)/i.exec(label);
  if (typed) {
    const kind = cleanKind(typed[1].replace(/\.$/, ""));
    return `${TYPE_PREFIX[kind] || typed[1]} ${typed[2]}`;
  }
  const bare = /^\[?([0-9]+(?:\.[0-9]+)*[a-z]*)[.\]\)]?(?:\s|$)/i.exec(label);
  if (bare) {
    if (cleanKind(segment.kind) === "paragraph") return `Paragraph ${bare[1]}`;
    const prefix = TYPE_PREFIX[cleanKind(segment.kind)];
    return prefix ? `${prefix} ${bare[1]}` : bare[1];
  }
  return label;
}

function lineReference(parent: string, anchor: string): string {
  if (!anchor) return parent;
  if (/^\(/.test(anchor)) return parent + anchor;
  return `${parent} ${anchor}`;
}

export function buildExportModel(body: any): ExportModel {
  const inherited = body?.inherited_recitals;
  const sources: SourceBody[] = [];
  if (inherited?.text && Array.isArray(inherited.segments) && inherited.segments.length) {
    sources.push({ ...inherited, doc_type: "legislation" });
  }
  sources.push(body || { text: "", segments: [] });
  const items: ExportItem[] = [];
  sources.forEach((source, sourceIndex) => {
    const text = String(source.text || "");
    const segments = Array.isArray(source.segments) ? source.segments : [];
    if (!segments.length && text.trim()) {
      items.push({ id: `${sourceIndex}:whole`, label: source.title || "Full text", reference: "Full text",
        kind: "document", level: 0, source: sourceIndex, segment: 0, start: 0, end: text.length,
        depth: 0, text, formatting: [] });
      return;
    }
    segments.forEach((segment, segmentIndex) => {
      const parentId = `${sourceIndex}:${segmentIndex}`;
      const reference = numberedReference(segment);
      items.push({ id: parentId, label: segment.label || reference, reference,
        kind: cleanKind(segment.kind), level: segment.level || 0, source: sourceIndex,
        segment: segmentIndex, start: segment.char_start, end: segment.char_end, depth: 0,
        text, formatting: segment.formatting || [], lines: segment.lines || [] });
      const lines = (segment.lines || []).filter((line) => line.anchor);
      lines.forEach((line, lineIndex) => items.push({
        id: `${parentId}:${lineIndex}`, label: lineReference(reference, String(line.anchor)),
        reference: lineReference(reference, String(line.anchor)), kind: cleanKind(segment.kind),
        level: (segment.level || 0) + (line.depth || 0) + 1, source: sourceIndex,
        segment: segmentIndex, line: lineIndex, start: line.start, end: line.end,
        depth: line.depth || 0, text, formatting: segment.formatting || [], parentId,
      }));
    });
  });
  return { sources, items };
}

// Compare identifiers without assuming they are numeric. Parentheses and amended-letter
// suffixes are retained, so 1(2)(za), 22B and 3.1 remain distinct addressable units.
function normalRef(value: string): string {
  let out = value.toLowerCase().replace(/[.]/g, "").replace(/[\[\]]/g, "").trim();
  out = out.replace(/^(?:the\s+)?(articles?|arts?|recitals?|recs?|sections?|secs?|paragraphs?|paras?|regulations?|regs?|rules?|schedules?|schs?|annex(?:es)?|points?|pts?|ss?|s)\s*/i, "");
  return out.replace(/\s+/g, "").replace(/[),;:]+$/, (m) => m.includes(")") ? ")" : "");
}

function itemToken(item: ExportItem): string {
  return normalRef(item.reference);
}

function matchesKind(item: ExportItem, requested?: string): boolean {
  if (!requested) return true;
  const got = cleanKind(item.kind);
  if (requested === "paragraph" && got === "document") return true;
  return got === requested;
}

function tokens(value: string): string[] {
  return value.split(/\s*(?:,|;|\band\b|&)\s*/i).map((v) => v.trim()).filter(Boolean);
}

function exactItem(items: ExportItem[], raw: string, kind?: string): ExportItem | undefined {
  const wanted = normalRef(raw);
  const found = items.filter((item) => matchesKind(item, kind) && itemToken(item) === wanted);
  if (!found.length) return undefined;
  // A parent and its first line can occasionally share a visible number. An explicit
  // parenthesised pinpoint always means the line; otherwise prefer the complete unit.
  return raw.includes("(") ? (found.find((item) => item.line != null) || found[0])
    : (found.find((item) => item.line == null) || found[0]);
}

function addRange(out: Set<string>, items: ExportItem[], left: string, right: string, kind?: string): boolean {
  const a = exactItem(items, left, kind), b = exactItem(items, right, kind);
  if (!a || !b || a.source !== b.source) return false;
  const level = a.line == null && b.line == null ? "parent" : "line";
  const candidates = items.filter((item) => matchesKind(item, kind) && item.source === a.source
    && (level === "parent" ? item.line == null : item.line != null));
  const ai = candidates.findIndex((item) => item.id === a.id);
  const bi = candidates.findIndex((item) => item.id === b.id);
  if (ai < 0 || bi < 0) return false;
  candidates.slice(Math.min(ai, bi), Math.max(ai, bi) + 1).forEach((item) => out.add(item.id));
  return true;
}

export function parseSelectionList(input: string, items: ExportItem[]): { ids: Set<string>; unmatched: string[] } {
  const ids = new Set<string>();
  const unmatched: string[] = [];
  const marker = /\b(articles?|arts?|recitals?|recs?|sections?|secs?|paragraphs?|paras?|regulations?|regs?|rules?|schedules?|schs?|annex(?:es)?|points?|pts?|ss?)\.?\s*/gi;
  const matches = Array.from(input.matchAll(marker));
  const groups: { kind?: string; value: string }[] = [];
  if (!matches.length) groups.push({ value: input });
  else {
    if ((matches[0].index || 0) > 0 && input.slice(0, matches[0].index).trim()) {
      groups.push({ value: input.slice(0, matches[0].index) });
    }
    matches.forEach((match, i) => {
      const start = (match.index || 0) + match[0].length;
      const end = i + 1 < matches.length ? matches[i + 1].index! : input.length;
      groups.push({ kind: cleanKind(match[1].replace(/\.$/, "")), value: input.slice(start, end) });
    });
  }
  for (const group of groups) {
    for (const raw of tokens(group.value)) {
      const range = /^(.+?)\s*(?:-|–|—|\bto\b)\s*(.+)$/i.exec(raw);
      if (range && addRange(ids, items, range[1], range[2], group.kind)) continue;
      const item = exactItem(items, raw, group.kind);
      if (item) ids.add(item.id);
      else unmatched.push(raw);
    }
  }
  return { ids, unmatched };
}

function selectedItems(model: ExportModel, ids: Set<string>): ExportItem[] {
  const chosen = model.items.filter((item) => ids.has(item.id));
  const parents = new Set(chosen.filter((item) => item.line == null).map((item) => item.id));
  return chosen.filter((item) => !item.parentId || !parents.has(item.parentId))
    .sort((a, b) => a.source - b.source || a.start - b.start || a.end - b.end);
}

function paragraphs(item: ExportItem): { text: string; depth: number; start: number; end: number }[] {
  const text = item.text;
  if (item.line != null) return [{ text: text.slice(item.start, item.end).trim(), depth: item.depth, start: item.start, end: item.end }];
  if (item.lines && item.lines.length > 1) return item.lines
    .filter((line) => line.start >= item.start && line.end <= item.end && text.slice(line.start, line.end).trim())
    .map((line) => ({ text: text.slice(line.start, line.end).trim(), depth: line.depth || 0,
      start: line.start, end: line.end }));
  const out: { text: string; depth: number; start: number; end: number }[] = [];
  const slice = text.slice(item.start, item.end);
  const re = /\n[ \t]*\n/g;
  let from = item.start, match: RegExpExecArray | null;
  while ((match = re.exec(slice))) {
    const end = item.start + match.index;
    if (text.slice(from, end).trim()) out.push({ text: text.slice(from, end).trim(), depth: 0, start: from, end });
    from = item.start + re.lastIndex;
  }
  if (text.slice(from, item.end).trim()) out.push({ text: text.slice(from, item.end).trim(), depth: 0, start: from, end: item.end });
  return out;
}

function exportBlocks(model: ExportModel, ids: Set<string>, gapMarker: string): ExportBlock[] {
  const selected = selectedItems(model, ids);
  const blocks: ExportBlock[] = [];
  selected.forEach((item, index) => {
    const previous = selected[index - 1];
    if (previous) {
      const omitted = item.source !== previous.source
        || item.text.slice(previous.end, item.start).trim().length > 0;
      if (omitted && gapMarker) blocks.push({ kind: "gap", paragraphs: [{ text: gapMarker, depth: 0, start: 0, end: 0 }] });
    }
    const raw = item.text.slice(item.start, item.end);
    const inline = /^\s*\[?[0-9]+(?:\.[0-9]+)*[a-z]*[.)\]]?\s/i.test(raw);
    blocks.push({ kind: "content", item, heading: inline ? "" : item.label, paragraphs: paragraphs(item) });
  });
  return blocks;
}

const escapeHtml = (value: string) => value.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
const escapeMd = (value: string) => value.replace(/([\\`*_[\]<>#|])/g, "\\$1");
const escapeLatex = (value: string) => value.replace(/[\\#$%&_{}~^]/g, (char) => ({
  "\\": "\\textbackslash{}", "#": "\\#", "$": "\\$", "%": "\\%", "&": "\\&",
  "_": "\\_", "{": "\\{", "}": "\\}", "~": "\\textasciitilde{}", "^": "\\textasciicircum{}",
}[char] || char));
const escapeRtf = (value: string) => value.replace(/\\/g, "\\\\").replace(/{/g, "\\{").replace(/}/g, "\\}")
  .replace(/[^\x00-\x7F]/g, (char) => `\\u${char.charCodeAt(0)}?`);

function headerLines(options: ExportOptions): string[] {
  return [options.includeTitle ? options.title : "",
    options.includeCitation && options.citation.trim() !== options.title.trim() ? options.citation : ""].filter(Boolean);
}

const showCitation = (options: ExportOptions) => options.includeCitation
  && !!options.citation && (!options.includeTitle || options.citation.trim() !== options.title.trim());

type StyledPiece = { text: string; bold: boolean; italic: boolean; underline: boolean };
function styledPieces(item: ExportItem, start: number, end: number): StyledPiece[] {
  const marks = (item.formatting || []).filter((mark) => mark.char_start < end && mark.char_end > start);
  if (!marks.length) return [{ text: item.text.slice(start, end), bold: false, italic: false, underline: false }];
  const cuts = Array.from(new Set([start, end, ...marks.flatMap((mark) =>
    [Math.max(start, mark.char_start), Math.min(end, mark.char_end)])])).sort((a, b) => a - b);
  return cuts.slice(0, -1).map((at, index) => {
    const next = cuts[index + 1];
    const active = new Set(marks.filter((mark) => mark.char_start <= at && mark.char_end >= next).map((mark) => mark.kind));
    return { text: item.text.slice(at, next), bold: active.has("bold"), italic: active.has("italic"), underline: active.has("underline") };
  });
}

function mdStyled(item: ExportItem, start: number, end: number): string {
  return styledPieces(item, start, end).map((piece) => {
    let value = escapeMd(piece.text);
    const match = /^(\s*)([\s\S]*?)(\s*)$/.exec(value);
    if (!match || !match[2]) return value;
    let core = match[2];
    if (piece.italic) core = `*${core}*`;
    if (piece.bold) core = `**${core}**`;
    return match[1] + core + match[3];
  }).join("");
}

function htmlStyled(item: ExportItem, start: number, end: number): string {
  return styledPieces(item, start, end).map((piece) => {
    let value = escapeHtml(piece.text);
    if (piece.underline) value = `<u>${value}</u>`;
    if (piece.italic) value = `<em>${value}</em>`;
    if (piece.bold) value = `<strong>${value}</strong>`;
    return value;
  }).join("");
}

function latexStyled(item: ExportItem, start: number, end: number): string {
  return styledPieces(item, start, end).map((piece) => {
    let value = escapeLatex(piece.text);
    if (piece.underline) value = `\\underline{${value}}`;
    if (piece.italic) value = `\\emph{${value}}`;
    if (piece.bold) value = `\\textbf{${value}}`;
    return value;
  }).join("");
}

function rtfStyled(item: ExportItem, start: number, end: number): string {
  return styledPieces(item, start, end).map((piece) => {
    let value = escapeRtf(piece.text);
    if (piece.underline) value = `\\ul ${value}\\ul0 `;
    if (piece.italic) value = `\\i ${value}\\i0 `;
    if (piece.bold) value = `\\b ${value}\\b0 `;
    return value;
  }).join("");
}

export function renderPlain(model: ExportModel, ids: Set<string>, options: ExportOptions): string {
  const out = headerLines(options);
  for (const block of exportBlocks(model, ids, options.gapMarker)) {
    if (block.heading) out.push(block.heading);
    for (const paragraph of block.paragraphs || []) out.push("  ".repeat(paragraph.depth) + paragraph.text);
  }
  return out.join("\n\n");
}

export function renderMarkdown(model: ExportModel, ids: Set<string>, options: ExportOptions): string {
  const out: string[] = [];
  if (options.includeTitle && options.title) out.push(`# ${escapeMd(options.title)}`);
  if (showCitation(options)) out.push(`_${escapeMd(options.citation)}_`);
  for (const block of exportBlocks(model, ids, options.gapMarker)) {
    if (block.kind === "gap") { out.push(options.gapMarker); continue; }
    if (block.heading) out.push(`**${escapeMd(block.heading)}**`);
    for (const paragraph of block.paragraphs || []) out.push("  ".repeat(paragraph.depth)
      + (block.item ? mdStyled(block.item, paragraph.start, paragraph.end).trim() : escapeMd(paragraph.text)));
  }
  return out.join("\n\n");
}

export function renderHtml(model: ExportModel, ids: Set<string>, options: ExportOptions): string {
  const out = ['<div style="font-family:Georgia,serif;line-height:1.45">'];
  if (options.includeTitle && options.title) out.push(`<h1 style="font-size:18pt;margin:0 0 8pt">${escapeHtml(options.title)}</h1>`);
  if (showCitation(options)) out.push(`<p style="font-size:10pt;margin:0 0 14pt"><em>${escapeHtml(options.citation)}</em></p>`);
  for (const block of exportBlocks(model, ids, options.gapMarker)) {
    if (block.kind === "gap") { out.push(`<p style="margin:10pt 0;text-align:center">${escapeHtml(options.gapMarker)}</p>`); continue; }
    if (block.heading) out.push(`<p style="margin:10pt 0 5pt"><strong>${escapeHtml(block.heading)}</strong></p>`);
    for (const paragraph of block.paragraphs || []) out.push(`<p style="margin:0 0 6pt;${paragraph.depth ? `margin-left:${paragraph.depth * 18}pt;` : ""}">${(block.item ? htmlStyled(block.item, paragraph.start, paragraph.end) : escapeHtml(paragraph.text)).replace(/\n/g, "<br>")}</p>`);
  }
  out.push("</div>");
  return out.join("");
}

export function renderLatex(model: ExportModel, ids: Set<string>, options: ExportOptions): string {
  const out: string[] = [];
  if (options.includeTitle && options.title) out.push(`\\section*{${escapeLatex(options.title)}}`);
  if (showCitation(options)) out.push(`\\textit{${escapeLatex(options.citation)}}`);
  for (const block of exportBlocks(model, ids, options.gapMarker)) {
    if (block.kind === "gap") { out.push(`\\begin{center}${escapeLatex(options.gapMarker)}\\end{center}`); continue; }
    if (block.heading) out.push(`\\paragraph{${escapeLatex(block.heading)}}`);
    for (const paragraph of block.paragraphs || []) {
      const value = (block.item ? latexStyled(block.item, paragraph.start, paragraph.end) : escapeLatex(paragraph.text)).replace(/\n/g, "\\\\\n");
      out.push(paragraph.depth ? `\\begin{quote}${value}\\end{quote}` : value);
    }
  }
  return out.join("\n\n");
}

export function renderRtf(model: ExportModel, ids: Set<string>, options: ExportOptions): string {
  const out = ["{\\rtf1\\ansi\\deff0{\\fonttbl{\\f0 Georgia;}}\\f0\\fs22"];
  if (options.includeTitle && options.title) out.push(`\\fs32\\b ${escapeRtf(options.title)}\\b0\\fs22\\par`);
  if (showCitation(options)) out.push(`\\i ${escapeRtf(options.citation)}\\i0\\par\\par`);
  for (const block of exportBlocks(model, ids, options.gapMarker)) {
    if (block.kind === "gap") { out.push(`\\qc ${escapeRtf(options.gapMarker)}\\par\\ql`); continue; }
    if (block.heading) out.push(`\\sb180\\b ${escapeRtf(block.heading)}\\b0\\par`);
    for (const paragraph of block.paragraphs || []) out.push(`\\li${paragraph.depth * 360} ${(block.item ? rtfStyled(block.item, paragraph.start, paragraph.end) : escapeRtf(paragraph.text)).replace(/\n/g, "\\line ")}\\par`);
  }
  out.push("}");
  return out.join("\n");
}

export function renderExport(format: ExportFormat, model: ExportModel, ids: Set<string>, options: ExportOptions): { text: string; html?: string; rtf?: string } {
  if (format === "rich") return { text: renderPlain(model, ids, options), html: renderHtml(model, ids, options) };
  if (format === "markdown") return { text: renderMarkdown(model, ids, options) };
  if (format === "latex") return { text: renderLatex(model, ids, options) };
  if (format === "rtf") return { text: renderPlain(model, ids, options), rtf: renderRtf(model, ids, options) };
  return { text: renderPlain(model, ids, options) };
}

export async function makeDocx(model: ExportModel, ids: Set<string>, options: ExportOptions): Promise<Blob> {
  const { AlignmentType, Document, HeadingLevel, Packer, Paragraph, TextRun } = await import("docx");
  const children: InstanceType<typeof Paragraph>[] = [];
  if (options.includeTitle && options.title) children.push(new Paragraph({ text: options.title, heading: HeadingLevel.TITLE }));
  if (showCitation(options)) children.push(new Paragraph({ children: [new TextRun({ text: options.citation, italics: true })], spacing: { after: 240 } }));
  for (const block of exportBlocks(model, ids, options.gapMarker)) {
    if (block.kind === "gap") {
      children.push(new Paragraph({ text: options.gapMarker, alignment: AlignmentType.CENTER, spacing: { before: 160, after: 160 } }));
      continue;
    }
    if (block.heading) children.push(new Paragraph({ text: block.heading, heading: HeadingLevel.HEADING_2, spacing: { before: 180, after: 80 } }));
    for (const paragraph of block.paragraphs || []) children.push(new Paragraph({
      children: block.item ? styledPieces(block.item, paragraph.start, paragraph.end).map((piece) => new TextRun({
        text: piece.text, bold: piece.bold, italics: piece.italic, underline: piece.underline ? {} : undefined,
      })) : [new TextRun(paragraph.text)], indent: paragraph.depth ? { left: paragraph.depth * 360 } : undefined,
      spacing: { after: 100 },
    }));
  }
  const document = new Document({ sections: [{ properties: {}, children }] });
  return Packer.toBlob(document);
}
