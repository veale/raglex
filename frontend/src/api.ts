// Typed client for the RagLex API. The base URL is "/api" in dev (Vite proxies it
// to the FastAPI backend) and configurable via VITE_API_BASE for other deploys.
const BASE = (import.meta.env.VITE_API_BASE as string | undefined) ?? "/api";

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json() as Promise<T>;
}

export interface Hit {
  doc_id: string; ecli: string | null; title: string | null; court: string | null;
  source: string | null; score: number; structural_unit: string | null;
  char_start: number | null; char_end: number | null; chunk_text: string;
  neighbours: { id: string; relationship_type: string; direction: string }[];
}
export interface SourceHealth {
  key: string; documents: number; consecutive_failures: number;
  watermark: string | null; last_yield_at: string | null;
}
export interface Alert { code: string; severity: string; subject: string; message: string; }
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
  documentBody: (id: string) => req<any>(`/document-body?id=${encodeURIComponent(id)}`),
  countDocuments: (filters: Record<string, string> = {}) =>
    req<{ total: number }>(`/documents/count?${new URLSearchParams(filters)}`),
  listDocuments: (filters: Record<string, string> = {}) =>
    req<any[]>(`/documents?${new URLSearchParams(filters)}`),
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
  startJob: (kind: "radiate" | "harvest-all" | "seed-text" | "rescan-citations" | "backfill-metadata" | "expand-citing" | "refresh-category" | "pull-ag-opinions", body: Record<string, unknown>) =>
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
    const res = await fetch(`${BASE}/unresolved/resolve-file`, { method: "POST", body: fd });
    if (!res.ok) throw new Error(`${res.status}`);
    return res.json();
  },
  sourceList: () => req<string[]>("/sources/list"),
  harvest: (body: Record<string, unknown>) =>
    req<any>("/harvest", { method: "POST", body: JSON.stringify(body) }),
  resolve: () => req<any>("/resolve", { method: "POST", body: "{}" }),
  embeddingHealth: () => req<any>("/health/embedding"),
  tag: (doc_id: string, tag: string) =>
    req<any>("/tag", { method: "POST", body: JSON.stringify({ doc_id, tag }) }),
  link: (src_id: string, dst_id: string, relationship: string, src_anchor?: string, dst_anchor?: string) =>
    req<any>("/link", { method: "POST", body: JSON.stringify({ src_id, dst_id, relationship, src_anchor, dst_anchor }) }),
  attach: async (doc_id: string, file: File, kind: string) => {
    const fd = new FormData();
    fd.append("file", file);
    fd.append("kind", kind);
    const res = await fetch(`${BASE}/documents/${encodeURIComponent(doc_id)}/attach`, { method: "POST", body: fd });
    if (!res.ok) throw new Error(`${res.status}`);
    return res.json();
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
    const res = await fetch(`${BASE}/import/file`, { method: "POST", body: fd });
    if (!res.ok) throw new Error(`${res.status}`);
    return res.json();
  },
  embed: () => req<any>("/embed", { method: "POST", body: "{}" }),
};
