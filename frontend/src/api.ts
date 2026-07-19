// Typed client for the RagLex API. The base URL is "/api" in dev (Vite proxies it
// to the FastAPI backend) and configurable via VITE_API_BASE for other deploys.
const BASE = (import.meta.env.VITE_API_BASE as string | undefined) ?? "/api";

// When the API requires a bearer token (RAGLEX_API_TOKEN), the UI reads it from a build
// env var or, failing that, localStorage — so a token-protected deploy is still usable
// from the browser without hardcoding a secret in the bundle.
function apiToken(): string | null {
  const env = import.meta.env.VITE_API_TOKEN as string | undefined;
  if (env) return env;
  try { return localStorage.getItem("raglex-api-token"); } catch { return null; }
}

function authHeaders(extra: Record<string, string> = {}): Record<string, string> {
  const token = apiToken();
  return { ...(token ? { Authorization: `Bearer ${token}` } : {}), ...extra };
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    ...init,
    headers: authHeaders({ "Content-Type": "application/json", ...(init?.headers as Record<string, string> || {}) }),
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json() as Promise<T>;
}

// Multipart POST (file upload) — same auth, but let the browser set the multipart
// Content-Type + boundary, so don't pass one.
async function postForm(path: string, fd: FormData): Promise<any> {
  const res = await fetch(`${BASE}${path}`, { method: "POST", body: fd, headers: authHeaders() });
  if (!res.ok) throw new Error(`${res.status}`);
  return res.json();
}

export interface Hit {
  doc_id: string; ecli: string | null; title: string | null; court: string | null;
  source: string | null; doc_type?: string | null; decision_date?: string | null;
  score: number; structural_unit: string | null;
  char_start: number | null; char_end: number | null; chunk_text: string;
  oscola?: any;
  // why-ranked: 1-based rank in each signal (null = didn't appear in that list)
  signals?: { semantic_rank: number | null; lexical_rank: number | null;
    authority_rank: number | null; authority_percentile: number | null } | null;
  neighbours: { id: string; relationship_type: string; direction: string;
    title?: string | null; authority?: number }[];
}
export interface SourceHealth {
  key: string; documents: number; consecutive_failures: number;
  watermark: string | null; last_yield_at: string | null;
}
export interface Alert { code: string; severity: string; subject: string; message: string; }
// A constructed link to the institute that publishes a case. `certainty` is "recorded"
// when the URL is one the importer actually stored, "derived" when every path segment was
// built from the citation, and "probable" where the institute assigns its own numbering.
export interface LIILink {
  site: string; site_name: string; url: string; certainty: "recorded" | "derived" | "probable";
}
export type LIIScope = "unheld" | "textless" | "both";
export interface LIITarget extends LIILink {
  stable_id: string; title: string | null; citation: string | null;
  status: "unheld" | "held-no-text"; citing_count: number; filename: string;
}
export interface Setting {
  key: string; label: string; secret: boolean; group: string; placeholder: string;
  set: boolean; source: string; display: string;
}

export const api = {
  health: () => req<{ status: string }>("/health"),
  search: (q: string, k = 8, filters: Record<string, string> = {}) => {
    const p = new URLSearchParams({ q, k: String(k), ...filters });
    return req<Hit[]>(`/search?${p}`);
  },
  document: (id: string) => req<any>(`/documents/${encodeURIComponent(id)}`),
  // the stored ORIGINAL file (guidance PDF, styled BAILII page) as a Blob — fetched
  // with auth headers (an <iframe src> can't send them), then shown via an object URL
  fetchRaw: async (id: string) => {
    const res = await fetch(`${BASE}/documents/${encodeURIComponent(id)}/raw`, { headers: authHeaders() });
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    return res.blob();
  },
  // grammar-recognise + resolve citations in arbitrary text (the PDF text layer)
  scanCitations: (text: string) =>
    req<{ citations: any[] }>("/citations/scan", { method: "POST", body: JSON.stringify({ text }) }),
  // Zotero connection + guidance classification
  zoteroStatus: () => req<any>("/zotero/status"),
  guidanceRules: () => req<any>("/guidance/rules"),
  saveGuidanceRules: (rules: any) =>
    req<any>("/guidance/rules", { method: "POST", body: JSON.stringify(rules) }),
  classifyGuidance: (body: Record<string, unknown>) =>
    req<any>("/guidance/classify", { method: "POST", body: JSON.stringify(body) }),
  setGuidanceField: (stable_id: string, field: string, value: string | null) =>
    req<any>("/guidance/field", { method: "POST", body: JSON.stringify({ stable_id, field, value }) }),
  classifyGuidanceJob: () => req<any>("/jobs/classify-guidance", { method: "POST", body: "{}" }),
  documentBody: (id: string) => req<any>(`/document-body?id=${encodeURIComponent(id)}`),
  // Outbound links to the LII that publishes a case we can't show in full.
  liiLinks: (id: string) =>
    req<{ stable_id: string; links: LIILink[] }>(`/document-lii-links?id=${encodeURIComponent(id)}`),
  liiLinkTargets: (scope: LIIScope, limit = 500, sites?: string) =>
    req<{ scope: string; count: number; links: LIITarget[] }>(
      `/lii-links?scope=${scope}&limit=${limit}${sites ? `&sites=${encodeURIComponent(sites)}` : ""}`),
  // Download the CSV through fetch (so it carries the auth header — a plain <a download>
  // can't, and putting the token in the URL would leak it into logs and history), then
  // hand the browser a blob URL to save.
  downloadLiiLinksCsv: async (scope: LIIScope, limit = 20000, sites?: string) => {
    const res = await fetch(
      `${BASE}/lii-links.csv?scope=${scope}&limit=${limit}${sites ? `&sites=${encodeURIComponent(sites)}` : ""}`,
      { headers: authHeaders() });
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    const url = URL.createObjectURL(await res.blob());
    const a = document.createElement("a");
    a.href = url; a.download = `lii-links-${scope}.csv`;
    document.body.appendChild(a); a.click(); a.remove();
    URL.revokeObjectURL(url);
  },
  mentions: (id: string, anchor?: string) =>
    req<any>(`/mentions?id=${encodeURIComponent(id)}${anchor ? `&anchor=${encodeURIComponent(anchor)}` : ""}`),
  citationsOut: (id: string, family: "cases" | "statute") =>
    req<any>(`/citations-out?id=${encodeURIComponent(id)}&family=${family}`),
  countDocuments: (filters: Record<string, string> = {}) =>
    req<{ total: number }>(`/documents/count?${new URLSearchParams(filters)}`),
  listDocuments: (filters: Record<string, string> = {}) =>
    req<any[]>(`/documents?${new URLSearchParams(filters)}`),
  searchCorpus: (params: Record<string, string> = {}) =>
    req<any>(`/search-corpus?${new URLSearchParams(params)}`),
  facetValues: () => req<any>("/facet-values"),
  graph: (id: string) => req<any>(`/graph/${encodeURIComponent(id)}`),
  stats: () => req<any>("/stats"),
  sources: () => req<SourceHealth[]>("/sources"),
  queues: () => req<Record<string, number>>("/queues"),
  alerts: () => req<Alert[]>("/alerts"),
  worklist: (limit = 30) => req<any[]>(`/worklist?limit=${limit}`),
  snowball: (needsAdapter = false, limit = 50) =>
    req<any[]>(`/snowball?limit=${limit}&only_unharvestable=${needsAdapter}`),
  unresolved: (limit = 100) => req<any[]>(`/unresolved?limit=${limit}`),
  coverage: () => req<any>("/coverage"),
  corpusMap: () => req<any>("/corpus-map"),
  corpusMapCites: (category: string) => req<any>(`/corpus-map/cites?category=${encodeURIComponent(category)}`),
  updateDocument: (stable_id: string, fields: Record<string, string>) =>
    req<any>(`/documents/${encodeURIComponent(stable_id)}/update`, { method: "POST", body: JSON.stringify(fields) }),
  correctCitation: (body: Record<string, unknown>) =>
    req<any>("/citations/correct", { method: "POST", body: JSON.stringify(body) }),
  untag: (doc_id: string, tag: string) =>
    req<any>("/untag", { method: "POST", body: JSON.stringify({ doc_id, tag }) }),
  tagMany: (doc_ids: string[], tag: string) =>
    req<any>("/tag-many", { method: "POST", body: JSON.stringify({ doc_ids, tag }) }),
  resolveReference: (body: Record<string, unknown>) =>
    req<any>("/unresolved/resolve", { method: "POST", body: JSON.stringify(body) }),
  harvestReference: (ref: string, candidate?: string) =>
    req<any>("/unresolved/harvest", { method: "POST", body: JSON.stringify({ ref, candidate }) }),
  resolveReferenceUrl: (ref: string, url: string) =>
    req<any>("/unresolved/resolve", { method: "POST", body: JSON.stringify({ ref, url }) }),
  harvestAllReferences: (limit = 25, min_citing = 1) =>
    req<any>("/unresolved/harvest-all", { method: "POST", body: JSON.stringify({ limit, min_citing }) }),
  retryFailed: () => req<any>("/unresolved/retry-failed", { method: "POST" }),
  decideSuggestion: (ref: string, suggested_id: string, accept: boolean, resolve = true) =>
    req<any>("/suggestions/decide", { method: "POST", body: JSON.stringify({ ref, suggested_id, accept, resolve }) }),
  flagRefinement: (body: Record<string, unknown>) =>
    req<any>("/refinement-flags", { method: "POST", body: JSON.stringify(body) }),
  refinementFlags: (status = "open") => req<any[]>(`/refinement-flags?status=${encodeURIComponent(status)}`),
  setRefinementFlag: (id: number, status = "resolved") =>
    req<any>(`/refinement-flags/${id}/status`, { method: "POST", body: JSON.stringify({ status }) }),
  unfetchable: (limit = 200) => req<any>(`/unresolved/unfetchable?limit=${limit}`),
  harvestHoL: () => req<any>("/jobs/harvest-hol", { method: "POST", body: "{}" }),
  radiate: (body: Record<string, unknown>) =>
    req<any>("/radiate", { method: "POST", body: JSON.stringify(body) }),
  discoverCiting: (target: string, via = "auto") =>
    req<any>("/discover-citing", { method: "POST", body: JSON.stringify({ target, via }) }),
  backfillTitles: () => req<any>("/backfill-titles", { method: "POST", body: "{}" }),
  aliases: () => req<any[]>("/aliases"),
  createAlias: (phrase: string, target_id: string, apply = false) =>
    req<any>("/aliases", { method: "POST", body: JSON.stringify({ phrase, target_id, apply }) }),
  deleteAlias: (phrase: string) => req<any>(`/aliases?phrase=${encodeURIComponent(phrase)}`, { method: "DELETE" }),
  applyRules: () => req<any>("/aliases/apply", { method: "POST", body: "{}" }),
  outstandingEffects: (limit = 500) => req<any[]>(`/legislation/effects?limit=${limit}`),
  refreshEffects: (limit = 10) =>
    req<any>("/legislation/effects/refresh", { method: "POST", body: JSON.stringify({ limit }) }),
  legislationChanges: (id: string) => req<any[]>(`/legislation/changes?id=${encodeURIComponent(id)}`),
  propagateChanges: (id: string) =>
    req<any>("/legislation/changes/propagate", { method: "POST", body: JSON.stringify({ id }) }),
  legislationVersions: (id: string) => req<any>(`/legislation/versions?id=${encodeURIComponent(id)}`),
  legislationVersionAt: (id: string, date: string) =>
    req<any>("/legislation/version", { method: "POST", body: JSON.stringify({ id, date }) }),
  detectCitations: (text: string) =>
    req<any>("/detect-citations", { method: "POST", body: JSON.stringify({ text }) }),
  startJob: (kind: "radiate" | "harvest-all" | "seed-text" | "rescan-citations" | "backfill-metadata" | "expand-citing" | "refresh-category" | "pull-ag-opinions" | "rescan" | "match-legislation" | "match-echr" | "mine-parallel" | "harvest-echr" | "suggest-matches", body: Record<string, unknown>) =>
    req<{ job_id: string; error?: string; already_running?: boolean }>(`/jobs/${kind}`, { method: "POST", body: JSON.stringify(body) }),
  jobStatus: (id: string) => req<any>(`/jobs/${id}`),
  jobsList: () => req<any[]>("/jobs"),
  cancelJob: (id: string) => req<any>(`/jobs/${id}/cancel`, { method: "POST", body: "{}" }),
  restartJob: (id: string) => req<any>(`/jobs/${id}/restart`, { method: "POST", body: "{}" }),
  sourceCatalog: () => req<any[]>("/sources/catalog"),
  watches: () => req<any[]>("/watches"),
  createWatch: (body: Record<string, unknown>) =>
    req<any>("/watches", { method: "POST", body: JSON.stringify(body) }),
  runWatch: (id: number) => req<any>(`/watches/${id}/run`, { method: "POST", body: "{}" }),
  gapScan: (body: Record<string, unknown>) =>
    req<{ job_id?: string; error?: string }>("/jobs/gap-scan", { method: "POST", body: JSON.stringify(body) }),
  gapStatus: (court: string, year: number) =>
    req<any>(`/gap-status?court=${encodeURIComponent(court)}&year=${year}`),
  gapClear: (court?: string, year?: number) =>
    req<any>("/gap-clear", { method: "POST", body: JSON.stringify({ court, year }) }),
  updateWatch: (id: number, body: Record<string, unknown>) =>
    req<any>(`/watches/${id}`, { method: "POST", body: JSON.stringify(body) }),
  deleteWatch: (id: number) => req<any>(`/watches/${id}`, { method: "DELETE" }),
  reparse: (stable_id: string) =>
    req<any>(`/documents/${encodeURIComponent(stable_id)}/reparse`, { method: "POST", body: "{}" }),
  resolveReferenceFile: async (ref: string, file: File, fields: Record<string, string>) => {
    const fd = new FormData();
    fd.append("file", file);
    fd.append("ref", ref);
    Object.entries(fields).forEach(([k, v]) => v && fd.append(k, v));
    return postForm("/unresolved/resolve-file", fd);
  },
  sourceList: () => req<string[]>("/sources/list"),
  harvest: (body: Record<string, unknown>) =>
    req<any>("/harvest", { method: "POST", body: JSON.stringify(body) }),
  // Background backfill of a whole source — max_pages: null means "no page cap".
  harvestSource: (body: Record<string, unknown>) =>
    req<any>("/jobs/harvest-source", { method: "POST", body: JSON.stringify(body) }),
  resolve: () => req<any>("/resolve", { method: "POST", body: "{}" }),
  embeddingHealth: () => req<any>("/health/embedding"),
  embedBacklog: () => req<{ provider: string; model: string; pending: number; indexed: number; total: number }>("/embed/backlog"),
  tag: (doc_id: string, tag: string) =>
    req<any>("/tag", { method: "POST", body: JSON.stringify({ doc_id, tag }) }),
  link: (src_id: string, dst_id: string, relationship: string, src_anchor?: string, dst_anchor?: string) =>
    req<any>("/link", { method: "POST", body: JSON.stringify({ src_id, dst_id, relationship, src_anchor, dst_anchor }) }),
  attach: async (doc_id: string, file: File, kind: string) => {
    const fd = new FormData();
    fd.append("file", file);
    fd.append("kind", kind);
    return postForm(`/documents/${encodeURIComponent(doc_id)}/attach`, fd);
  },
  getSettings: () => req<{ settings: Setting[]; path: string }>("/settings"),
  saveSettings: (values: Record<string, string>) =>
    req<{ settings: Setting[] }>("/settings", { method: "POST", body: JSON.stringify(values) }),
  importUrl: (body: Record<string, string>) =>
    req<any>("/import/url", { method: "POST", body: JSON.stringify(body) }),
  importNote: (body: Record<string, string>) =>
    req<any>("/import/note", { method: "POST", body: JSON.stringify(body) }),
  importZotero: (body: Record<string, unknown>) =>
    req<any>("/import/zotero", { method: "POST", body: JSON.stringify(body) }),
  importFile: async (file: File, fields: Record<string, string>) => {
    const fd = new FormData();
    fd.append("file", file);
    Object.entries(fields).forEach(([k, v]) => v && fd.append(k, v));
    return postForm("/import/file", fd);
  },
  importCase: async (file: File, opts: { ref?: string; neutral_citation?: string; also_cited_as?: string } = {}) => {
    const fd = new FormData();
    fd.append("file", file);
    Object.entries(opts).forEach(([k, v]) => v && fd.append(k, v));
    return postForm("/import/case", fd) as Promise<{ stable_id: string; detected_citation: string | null; aliases: number; resolved_edges: number; chars: number; engine: string }>;
  },
  importBailii: async (stable_id: string, file: File, title?: string) => {
    const fd = new FormData();
    fd.append("file", file);
    fd.append("stable_id", stable_id);
    if (title) fd.append("title", title);
    return postForm("/import/bailii", fd) as Promise<{ stable_id: string; chars: number; resolved_edges: number }>;
  },
  importBailiiZip: async (file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return postForm("/import/bailii-zip", fd) as Promise<{ job_id?: string; error?: string }>;
  },
  // no-zip folder upload: stage a batch of .html files under an upload id
  importBailiiFilesBatch: async (upload_id: string, files: File[]) => {
    const fd = new FormData();
    fd.append("upload_id", upload_id);
    for (const f of files) fd.append("files", f, f.name);
    return postForm("/import/bailii-files", fd) as Promise<{ received: number; staged: number; error?: string }>;
  },
  importBailiiFilesStart: (upload_id: string) =>
    req<{ job_id?: string; error?: string }>("/import/bailii-files/start", { method: "POST", body: JSON.stringify({ upload_id }) }),
  importWestlawZip: async (file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return postForm("/import/westlaw-zip", fd) as Promise<{ job_id?: string; error?: string }>;
  },
  // no-zip folder upload: stage a batch of .rtf files under an upload id
  importWestlawFilesBatch: async (upload_id: string, files: File[]) => {
    const fd = new FormData();
    fd.append("upload_id", upload_id);
    for (const f of files) fd.append("files", f, f.name);
    return postForm("/import/westlaw-files", fd) as Promise<{ received: number; staged: number; error?: string }>;
  },
  importWestlawFilesStart: (upload_id: string) =>
    req<{ job_id?: string; error?: string }>("/import/westlaw-files/start", { method: "POST", body: JSON.stringify({ upload_id }) }),
  // unified case-law import: one uploader for BAILII .html + Westlaw .rtf, routed by extension
  importCaselawZip: async (file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return postForm("/import/caselaw-zip", fd) as Promise<{ job_id?: string; error?: string }>;
  },
  importCaselawFilesBatch: async (upload_id: string, files: File[]) => {
    const fd = new FormData();
    fd.append("upload_id", upload_id);
    for (const f of files) fd.append("files", f, f.name);
    return postForm("/import/caselaw-files", fd) as Promise<{ received: number; staged: number; error?: string }>;
  },
  importCaselawFilesStart: (upload_id: string) =>
    req<{ job_id?: string; error?: string }>("/import/caselaw-files/start", { method: "POST", body: JSON.stringify({ upload_id }) }),
  pendingSuggestions: (limit = 500) => req<any>(`/suggestions/pending?limit=${limit}`),
  // bulk near-miss decisions: one POST, one resolver pass at the end
  decideSuggestionsBulk: (items: { ref: string; suggested_id: string; accept: boolean }[]) =>
    req<any>("/suggestions/decide-bulk", { method: "POST", body: JSON.stringify({ items }) }),
  // the passages where the corpus cites a hanging reference (suggestion-review evidence)
  referenceContext: (ref: string, limit = 5) =>
    req<any>(`/reference-context?ref=${encodeURIComponent(ref)}&limit=${limit}`),
  // citation-network intelligence (design §3): related docs, citator, authority rebuild
  related: (id: string, limit = 12) =>
    req<any>(`/related?id=${encodeURIComponent(id)}&limit=${limit}`),
  citator: (id: string) => req<any>(`/citator?id=${encodeURIComponent(id)}`),
  provision: (id: string, opts: { label?: string; start?: number; end?: number; n?: number } = {}) => {
    const p = new URLSearchParams({ id });
    if (opts.label) p.set("label", opts.label);
    if (opts.start != null) p.set("start", String(opts.start));
    if (opts.end != null) p.set("end", String(opts.end));
    if (opts.n != null) p.set("n", String(opts.n));
    return req<any>(`/provision?${p}`);
  },
  rebuildAuthority: () => req<any>("/jobs/rebuild-authority", { method: "POST", body: "{}" }),
  // Explore homepage: the corpus's whole shape + in-place drill-down
  corpusShape: () => req<any>("/corpus-shape"),
  drill: (params: Record<string, string>) => req<any>(`/drill?${new URLSearchParams(params)}`),
  exportRetrievalCitations: (p: { min_citing?: number; batch_size?: number; include_names?: boolean; separator?: string; series?: string; jurisdictions?: string } = {}) =>
    req<any>(`/export/retrieval-citations?${new URLSearchParams(Object.entries(p).filter(([, v]) => v !== undefined && v !== "").map(([k, v]) => [k, String(v)]))}`),
  embed: () => req<any>("/embed", { method: "POST", body: "{}" }),
};
