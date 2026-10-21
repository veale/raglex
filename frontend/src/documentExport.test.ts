import { describe, expect, it } from "vitest";
import { buildExportModel, makeDocx, parseSelectionList, renderHtml, renderLatex, renderMarkdown, renderPlain } from "./documentExport";

const recitalText = "Recital 3 text.\n\nRecital 4 text.\n\nRecital 5 text.";
const lawText = [
  "Article 3 text.",
  "Article 5 text.",
  "(1) Article six first.\n(2) Article six second.\n(3) Article six third.",
  "Article 9 text.",
  "Article 22B text.",
  "(1) First.\n(2) Second.\n(za) Amended letter.",
].join("\n\n");

function span(text: string, needle: string) {
  const start = text.indexOf(needle);
  return { start, end: start + needle.length };
}

const rec3 = span(recitalText, "Recital 3 text.");
const rec4 = span(recitalText, "Recital 4 text.");
const rec5 = span(recitalText, "Recital 5 text.");
const art3 = span(lawText, "Article 3 text.");
const art5 = span(lawText, "Article 5 text.");
const art6 = span(lawText, "(1) Article six first.\n(2) Article six second.\n(3) Article six third.");
const art9 = span(lawText, "Article 9 text.");
const art22b = span(lawText, "Article 22B text.");
const art1 = span(lawText, "(1) First.\n(2) Second.\n(za) Amended letter.");

const lines = (whole: { start: number; end: number }, source: string, anchors: string[]) => {
  const value = source.slice(whole.start, whole.end);
  let cursor = 0;
  return value.split("\n").map((line, index) => {
    const start = whole.start + cursor;
    cursor += line.length + 1;
    return { start, end: start + line.length, depth: index ? 1 : 0, anchor: anchors[index] };
  });
};

function model() {
  return buildExportModel({
    text: lawText, doc_type: "legislation",
    inherited_recitals: {
      text: recitalText, segments: [
        { label: "Recital 3", kind: "recital", level: 0, char_start: rec3.start, char_end: rec3.end },
        { label: "Recital 4", kind: "recital", level: 0, char_start: rec4.start, char_end: rec4.end },
        { label: "Recital 5", kind: "recital", level: 0, char_start: rec5.start, char_end: rec5.end },
      ],
    },
    segments: [
      { label: "Article 3", kind: "article", level: 0, char_start: art3.start, char_end: art3.end },
      { label: "Article 5", kind: "article", level: 0, char_start: art5.start, char_end: art5.end },
      { label: "Article 6 Scope", kind: "article", level: 0, char_start: art6.start, char_end: art6.end,
        lines: lines(art6, lawText, ["(1)", "(2)", "(3)"]) },
      { label: "Article 9", kind: "article", level: 0, char_start: art9.start, char_end: art9.end },
      { label: "Article 22B", kind: "article", level: 0, char_start: art22b.start, char_end: art22b.end },
      { label: "Article 1", kind: "article", level: 0, char_start: art1.start, char_end: art1.end,
        lines: lines(art1, lawText, ["(1)", "(2)", "(2)(za)"]) },
    ],
  });
}

describe("pasted legal-reference selection", () => {
  it("understands plural abbreviations, commas, ranges and typed groups", () => {
    const m = model();
    const result = parseSelectionList("recitals 3, 4, 5; arts. 5–6, 9, 22B", m.items);
    expect(result.unmatched).toEqual([]);
    expect([...result.ids].map((id) => m.items.find((item) => item.id === id)?.reference)).toEqual([
      "Recital 3", "Recital 4", "Recital 5", "Article 5", "Article 6", "Article 9", "Article 22B",
    ]);
  });

  it("matches nested and amended alphanumeric pinpoints without numeric assumptions", () => {
    const m = model();
    const result = parseSelectionList("article 6(3); art 1(2)(za)", m.items);
    expect(result.unmatched).toEqual([]);
    expect([...result.ids].map((id) => m.items.find((item) => item.id === id)?.reference)).toEqual([
      "Article 6(3)", "Article 1(2)(za)",
    ]);
  });
});

describe("casebook rendering", () => {
  const options = { title: "Example Regulation", citation: "Regulation (EU) 2026/1",
    gapMarker: "[..]", includeTitle: true, includeCitation: true };

  it("adds one default marker wherever selected source spans omit text", () => {
    const m = model();
    const chosen = parseSelectionList("articles 3, 9", m.items).ids;
    const plain = renderPlain(m, chosen, options);
    expect(plain).toMatch(/Article 3 text\.\n\n\[\.\.\]\n\nArticle 9\n\nArticle 9 text\./);
    expect(plain.match(/\[\.\.\]/g)).toHaveLength(1);
  });

  it("keeps drafted sub-provision indentation and emits clean link-free formats", () => {
    const m = model();
    const chosen = parseSelectionList("article 6", m.items).ids;
    expect(renderMarkdown(m, chosen, options)).toContain("  (2) Article six second.");
    expect(renderHtml(m, chosen, options)).toContain("margin-left:18pt");
    expect(renderHtml(m, chosen, options)).not.toContain("<a ");
    expect(renderLatex(m, chosen, options)).toContain("\\begin{quote}(2) Article six second.\\end{quote}");
  });

  it("produces a real OOXML package for Word", async () => {
    const m = model();
    const chosen = parseSelectionList("articles 5-6", m.items).ids;
    const blob = await makeDocx(m, chosen, options);
    expect(blob.type).toBe("application/vnd.openxmlformats-officedocument.wordprocessingml.document");
    expect(Array.from(new Uint8Array(await blob.slice(0, 2).arrayBuffer()))).toEqual([0x50, 0x4b]);
  });
});
