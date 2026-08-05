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

// The CSRF nonce the server bound to our session cookie, mirrored to a readable cookie at
// login. Mutating requests must echo it; reads don't need it.
function csrfToken(): string {
  const m = document.cookie.match(/(?:^|;\s*)raglex_csrf=([^;]+)/);
  if (m) return decodeURIComponent(m[1]);
  return "1";  // IP-allow-listed sessions carry no nonce; header presence is the signal
}

function isWrite(method?: string): boolean {
  const m = (method || "GET").toUpperCase();
  return m !== "GET" && m !== "HEAD" && m !== "OPTIONS";
}

function authHeaders(extra: Record<string, string> = {}, method?: string): Record<string, string> {
  const token = apiToken();
  return {
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
    ...(isWrite(method) ? { "X-Raglex-CSRF": csrfToken() } : {}),
    ...extra,
  };
}

// An EU instrument's official title ends with the legislative footnote "(Text with EEA
// relevance)". It is part of the citation of record, so it stays in the database and in
// the harvested title — but on screen it costs a line on every heading, every search row
// and every citer link while saying nothing about the law. It is stripped here, at the
// one place every response enters the UI, rather than at the ~300 places a title is
// rendered (the static export strips it in its own renderer for the same reason).
const EEA_RELEVANCE = /\s*\(\s*Text with EEA relevance\.?\s*\)/gi;

function isTitleKey(key: string): boolean {
  return key === "title" || key.endsWith("_title");
}

/** Recursively strip the EEA footnote from every title-ish string in a payload. Only
 *  values under a title key are touched, so document TEXT is never rewritten. */
function hideEeaFootnote<T>(value: T, underTitleKey = false): T {
  if (typeof value === "string") {
    return (underTitleKey ? value.replace(EEA_RELEVANCE, "").trim() : value) as unknown as T;
  }
  if (Array.isArray(value)) {
    return value.map((v) => hideEeaFootnote(v, underTitleKey)) as unknown as T;
  }
  if (value && typeof value === "object") {
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(value as Record<string, unknown>)) {
      out[k] = hideEeaFootnote(v, isTitleKey(k));
    }
    return out as unknown as T;
  }
  return value;
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    ...init,
    credentials: "include",  // carry the session cookie (same-origin by default; explicit for CORS deploys)
    headers: authHeaders({ "Content-Type": "application/json", ...(init?.headers as Record<string, string> || {}) }, init?.method),
  });
  if (res.status === 401) { window.dispatchEvent(new CustomEvent("raglex-unauthenticated")); }
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return hideEeaFootnote(await res.json()) as T;
}

// Multipart POST (file upload) — same auth, but let the browser set the multipart
// Content-Type + boundary, so don't pass one.
async function postForm(path: string, fd: FormData): Promise<any> {
  const res = await fetch(`${BASE}${path}`, {
    method: "POST", body: fd, credentials: "include", headers: authHeaders({}, "POST"),
  });
  if (res.status === 401) { window.dispatchEvent(new CustomEvent("raglex-unauthenticated")); }
  if (!res.ok) throw new Error(`${res.status}`);
  return hideEeaFootnote(await res.json());
}

// Auth surface (see src/raglex/web/auth.py). `me` tells the SPA whether enforcement is on
// and at what role; login/logout manage the session cookie.
export interface AuthMe {
  authenticated: boolean; role: "anon" | "reader" | "admin"; method?: string;
  enforced: boolean; csrf?: string | null; can_elevate?: boolean; passkey_supported?: boolean;
}
export const auth = {
  me: () => req<AuthMe>("/auth/me"),
  login: (password: string) =>
    req<{ role: string; csrf?: string; error?: string }>("/auth/login",
      { method: "POST", body: JSON.stringify({ password }) }),
  logout: () => req<{ ok: boolean }>("/auth/logout", { method: "POST", body: "{}" }),
  passkeyLoginOptions: () => req<any>("/auth/webauthn/login/options", { method: "POST", body: "{}" }),
  passkeyLoginVerify: (cred: any) =>
    req<{ role: string; csrf?: string }>("/auth/webauthn/login/verify",
      { method: "POST", body: JSON.stringify(cred) }),
  passkeyRegisterOptions: () => req<any>("/auth/webauthn/register/options", { method: "POST", body: "{}" }),
  passkeyRegisterVerify: (cred: any) =>
    req<any>("/auth/webauthn/register/verify", { method: "POST", body: JSON.stringify(cred) }),
};

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
// CourtListener is the one source with a hard *daily* request ceiling, so "how much is
// left today" is the difference between a queue that is stalled and one that is simply
// waiting for the window to roll.
export interface UsCaselawBudget {
  configured: boolean;            // false = no API token set
  allowed_now: boolean;
  blocked_by: string | null;      // which window is binding ("minute" | "hour" | "day")
  retry_after_seconds: number;
  // null wherever the daily window is uncapped — a share of an unbounded quota is not
  // a number, and the sentinel limit would render as "600000000 requests reserved"
  remaining: number | null;       // the tightest window's headroom
  windows: Record<string, { used: number; limit: number | null }>;
  queue_allowance: number | null; // today's share reserved for the unattended queue
  queue_reserve: number;
  daily_cap: boolean;
  tier: "free" | "custom";
  pending_us_references: number;  // the US backlog waiting on this quota
  estimated_cases_today: number | null;
  estimated_days_to_clear: number | null;
}
// CanLII meters requests too (a persisted ledger below their documented ceiling).
// The API is metadata-only — the two backlogs it reports are the pending Canadian
// citations (each resolvable into a metadata stub) and the held decisions awaiting
// enrichment (permalink, docket, keywords, citator edges).
export interface CanliiBudget {
  configured: boolean;            // false = no API key set
  allowed_now: boolean;
  blocked_by: string | null;
  retry_after_seconds: number;
  remaining: number | null;
  windows: Record<string, { used: number; limit: number | null }>;
  daily_cap: boolean;
  tier: "default" | "custom";
  pending_ca_references: number;
  unenriched_documents: number;
  estimated_days_to_clear: number | null;
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
  set: boolean; source: string; display: string; kind?: string;
}
// The import form's vocabularies, read live from the corpus so the dropdowns can never
// offer a value the rest of the app would not recognise.
export interface ImportOptions {
  // `source` is what the item will be stored under (uk → uk-user-import) — which is what
  // makes every jurisdiction-sensitive citation grammar apply to it.
  jurisdictions: { code: string; label: string; source: string; documents: number }[];
  doc_types: string[];
  relationships: string[];
  structures: { value: string; label: string }[];
  // leading courts by volume, per jurisdiction bucket — not an exhaustive registry,
  // so the field takes free text too
  courts_by_jurisdiction: Record<string, { court: string; label: string; documents: number }[]>;
  languages: string[];
  tags: string[];
}
export interface ImportItem {
  title?: string; doc_type?: string; jurisdiction?: string; court?: string;
  decision_date?: string; citation?: string; language?: string; link_to?: string;
  relationship?: string; structure?: string; tags?: string[];
}
export interface ImportedDoc {
  index: number; stable_id?: string; title?: string | null; doc_type?: string;
  source?: string; jurisdiction?: string | null; chars?: number; segments?: number;
  structure?: string; tags?: string[]; citation?: string | null;
  linked_to?: string | null; needs_ocr?: boolean; filename?: string; error?: string;
}
export interface ImportBatchResult {
  imported: number; failed: number; documents: ImportedDoc[]; next?: string;
}
export interface StaticBundleItem {
  stable_id: string; slug: string; title: string; note: string;
  // Operator's own shorthand ("DSA"), shown bold before the full name on the index page
  // only — inside an edition the instrument keeps its full title.
  short?: string;
}
// One outbound request when a run finishes: an ntfy push, a Slack hook, or a listener
// that kicks off an scp. Deliberately a raw URL + headers + body rather than a named
// integration.
export interface StaticBundleWebhook {
  enabled: boolean; url: string; method: string;
  headers: Record<string, string>; body: string;
}
export interface StaticBundle {
  items: StaticBundleItem[];
  index_title: string; index_text: string; max_snippets: number;
  output_dir: string; resolved_output_dir?: string;
  // The index title as nostalgic rainbow WordArt. Index page only — an edition's title
  // is the name of a legal instrument.
  index_wordart?: boolean;
  webhook?: StaticBundleWebhook;
  last_run?: any;
  // at_hour pins a scheduled rebuild to one UTC hour (null = any hour).
  schedule?: { name: string; enabled: boolean; every_minutes: number | null;
               at_hour?: number | null } | null;
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
  // Administrator-only, self-contained law edition. Fetch rather than using a bare
  // download link so bearer-token deployments receive the same auth as the rest of UI.
  downloadStaticLaw: async (
    id: string,
    onProgress?: (message: string) => void,
  ) => {
    onProgress?.("Adding sources and excerpts…");
    // The administrator's button means "export what RagLex holds now". API callers that
    // want the last scheduled edition can omit refresh and download the cached artifact.
    const build = await req<any>("/export/static-law", {
      method: "POST",
      body: JSON.stringify({ id, refresh: true }),
    });
    if (build.job_id) {
      while (true) {
        const job = await req<any>(
          `/jobs/${encodeURIComponent(build.job_id)}`);
        if (job.status === "done") break;
        if (job.status === "error") {
          throw new Error(job.result?.error || job.error || "Static edition build failed");
        }
        if (job.status === "cancelled") {
          throw new Error("Static edition build was cancelled");
        }
        const done = Number(job.progress?.done || 0);
        const total = Number(job.progress?.total || 0);
        if (total > 0) {
          onProgress?.(
            `Adding sources and excerpts: ${done.toLocaleString()} of ${total.toLocaleString()}…`,
          );
        } else {
          onProgress?.("Preparing the static edition…");
        }
        await new Promise((resolve) => window.setTimeout(resolve, 1200));
      }
    }
    onProgress?.("Downloading…");
    const res = await fetch(
      `${BASE}/export/static-law.html?id=${encodeURIComponent(id)}`,
      { credentials: "include", headers: authHeaders() });
    if (res.status === 401) window.dispatchEvent(new CustomEvent("raglex-unauthenticated"));
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    const disposition = res.headers.get("content-disposition") || "";
    const match = disposition.match(/filename="([^"]+)"/i);
    const filename = match?.[1] || "raglex-static-edition.html";
    const url = URL.createObjectURL(await res.blob());
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    return filename;
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
  // The same, for a reference that isn't held yet (the peek's "read it here" on an
  // unfetched/unfetchable case) — links are constructed from the citation, so a
  // "can_upload" one doubles as the file to save-and-upload.
  referenceLiiLinks: (ref: string, raw?: string) =>
    req<{ ref: string; links: (LIILink & { kind?: string; can_upload?: boolean })[] }>(
      `/reference-lii-links?ref=${encodeURIComponent(ref)}${raw ? `&raw=${encodeURIComponent(raw)}` : ""}`),
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
  // honest cited-by facet counts (whole incoming set, not the loaded top slice) +
  // the per-facet server-side fetch for slices outside the loaded page
  citedByBreakdown: (id: string) =>
    req<{ buckets: { jurisdiction: string; kind: string; documents: number }[]; total: number }>(
      `/cited-by-breakdown?id=${encodeURIComponent(id)}`),
  citedBySlice: (id: string, jurisdiction: string, kind?: string, limit = 60) =>
    req<{ incoming: any[] }>(
      `/cited-by-slice?id=${encodeURIComponent(id)}&jurisdiction=${encodeURIComponent(jurisdiction)}${kind ? `&kind=${encodeURIComponent(kind)}` : ""}&limit=${limit}`),
  mentions: (id: string, anchor?: string, sort?: string, offset?: number, limit?: number, exact?: boolean,
             jurisdiction?: string | null, kind?: string | null) =>
    req<any>(`/mentions?id=${encodeURIComponent(id)}${anchor ? `&anchor=${encodeURIComponent(anchor)}` : ""}`
      + (sort ? `&sort=${encodeURIComponent(sort)}` : "")
      + (offset ? `&offset=${offset}` : "")
      + (limit ? `&limit=${limit}` : "")
      + (exact ? `&exact=true` : "")
      + (jurisdiction ? `&jurisdiction=${encodeURIComponent(jurisdiction)}` : "")
      + (kind ? `&kind=${encodeURIComponent(kind)}` : "")),
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
  usCaselawBudget: () => req<UsCaselawBudget>("/sources/us-caselaw/budget"),
  canliiBudget: () => req<CanliiBudget>("/sources/ca-canlii/budget"),
  canliiEnrich: (limit = 200) =>
    req<{ job_id?: string; error?: string }>("/jobs/canlii-enrich", { method: "POST", body: JSON.stringify({ limit }) }),
  queues: () => req<Record<string, number>>("/queues"),
  alerts: () => req<Alert[]>("/alerts"),
  worklist: (limit = 30) => req<any[]>(`/worklist?limit=${limit}`),
  unresolved: (limit = 100) => req<any>(`/unresolved?limit=${limit}`),
  coverage: () => req<any>("/coverage"),
  corpusMap: () => req<any>("/corpus-map"),
  refreshCorpusMap: () => req<any>("/corpus-map/refresh", { method: "POST" }),
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
  submitFeedback: (body: { kind: string; message: string; page?: string; url?: string; metadata?: Record<string, unknown> }) =>
    req<{ submitted?: boolean; feedback_id?: number; error?: string }>("/feedback", { method: "POST", body: JSON.stringify(body) }),
  feedback: (status = "open") => req<any[]>(`/feedback?status=${encodeURIComponent(status)}`),
  setFeedback: (id: number, status = "resolved") =>
    req<any>(`/feedback/${id}/status`, { method: "POST", body: JSON.stringify({ status }) }),
  freetext: (p: { q: string; exact?: boolean; limit?: number; offset?: number;
                  source?: string; doc_type?: string; court?: string;
                  jurisdiction?: string; year_from?: number }) =>
    req<any>(`/freetext?${new URLSearchParams(
      Object.entries(p).filter(([, v]) => v !== undefined && v !== "")
        .map(([k, v]) => [k, String(v)])).toString()}`),
  searchStatus: () => req<any>("/search/status"),
  freetextHydrate: (ids: string[], q: string, exact: boolean) =>
    req<{ items: any[] }>("/freetext/hydrate",
      { method: "POST", body: JSON.stringify({ ids, q, exact }) }),
  freetextCitesFilter: (ids: string[], target: string) =>
    req<{ ids: string[] }>("/freetext/cites-filter",
      { method: "POST", body: JSON.stringify({ ids, target }) }),
  freetextCoverage: () => req<any>("/freetext/coverage"),
  freetextScope: () => req<any>("/freetext/scope"),
  setFreetextScope: (body: { sources?: string[]; note?: string }) =>
    req<any>("/freetext/scope", { method: "POST", body: JSON.stringify(body) }),
  buildFts: (body: { sources?: string[]; reindex?: boolean } = {}) =>
    req<any>("/jobs/build-fts", { method: "POST", body: JSON.stringify({ ...body, queue: true }) }),
  shorthands: (p: { q?: string; state?: string; candidate_id?: string; limit?: number; offset?: number } = {}) =>
    req<any>(`/shorthands?${new URLSearchParams(
      Object.entries(p).filter(([, v]) => v !== undefined && v !== "")
        .map(([k, v]) => [k, String(v)])).toString()}`),
  setShorthand: (body: { shorthand: string; candidate_id: string; blocked?: boolean; is_abbrev?: boolean }) =>
    req<any>("/shorthands/set", { method: "POST", body: JSON.stringify(body) }),
  deleteShorthand: (shorthand: string, candidate_id: string) =>
    req<any>("/shorthands/delete", { method: "POST", body: JSON.stringify({ shorthand, candidate_id }) }),
  purgeShorthands: (dry_run = true) =>
    req<any>("/shorthands/purge-invalid", { method: "POST", body: JSON.stringify({ dry_run }) }),
  unfetchable: (limit = 200, min_citing?: number) =>
    req<any>(`/unresolved/unfetchable?limit=${limit}${min_citing ? `&min_citing=${min_citing}` : ""}`),
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
  legislativeStatus: (id: string) => req<any>(`/legislation/status?id=${encodeURIComponent(id)}`),
  legislationVersions: (id: string) => req<any>(`/legislation/versions?id=${encodeURIComponent(id)}`),
  // live CJEU proceedings on an instrument: Article 267 references apart from the other
  // pending actions, each with the provisions it turns on
  pendingReferences: (id: string) =>
    req<any>(`/legislation/pending-references?id=${encodeURIComponent(id)}`),
  legislationVersionAt: (id: string, date: string) =>
    req<any>("/legislation/version", { method: "POST", body: JSON.stringify({ id, date }) }),
  detectCitations: (text: string) =>
    req<any>("/detect-citations", { method: "POST", body: JSON.stringify({ text }) }),
  startJob: (kind: "harvest-all" | "rescan-citations" | "backfill-metadata" | "expand-citing" | "refresh-category" | "pull-ag-opinions" | "rescan" | "match-legislation" | "match-echr" | "mine-parallel" | "harvest-echr" | "suggest-matches" | "finish-bulk-postprocess" | "canlii-enrich" | "backfill-eu-case-names", body: Record<string, unknown>) =>
    req<{ job_id: string; error?: string; already_running?: boolean }>(`/jobs/${kind}`, { method: "POST", body: JSON.stringify(body) }),
  systemStorage: () => req<{ database_bytes: number; tables: { name: string; bytes: number }[] }>("/system/storage"),
  jobStatus: (id: string) => req<any>(`/jobs/${id}`),
  jobsList: () => req<any[]>("/jobs"),
  cancelJob: (id: string) => req<any>(`/jobs/${id}/cancel`, { method: "POST", body: "{}" }),
  restartJob: (id: string) => req<any>(`/jobs/${id}/restart`, { method: "POST", body: "{}" }),
  sourceCatalog: () => req<any[]>("/sources/catalog"),
  provisionMappings: (id: string) =>
    req<any>(`/provision-mappings?id=${encodeURIComponent(id)}`),
  saveProvisionMappings: (body: Record<string, unknown>) =>
    req<any>("/provision-mappings", { method: "POST", body: JSON.stringify(body) }),
  deleteProvisionMapping: (id: number) =>
    req<any>(`/provision-mappings/${id}`, { method: "DELETE" }),
  inheritedProvisionMentions: (id: string, anchor?: string) =>
    req<any>(`/provision-mappings/inherited?id=${encodeURIComponent(id)}${anchor ? `&current_anchor=${encodeURIComponent(anchor)}` : ""}`),
  keepCurrent: () => req<{ overlap_default_days: number; sources: any[] }>("/sources/keep-current"),
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
  checkEnglishRendition: (stable_id: string) =>
    req<any>("/documents/check-english", {
      method: "POST", body: JSON.stringify({ stable_id }),
    }),
  resolveReferenceFile: async (ref: string, file: File, fields: Record<string, string>) => {
    const fd = new FormData();
    fd.append("file", file);
    fd.append("ref", ref);
    Object.entries(fields).forEach(([k, v]) => v && fd.append(k, v));
    return postForm("/unresolved/resolve-file", fd);
  },
  queueStatus: () => req<{ running: number; queued: number; max_concurrent: number; scheduler_paused: boolean }>("/jobs/queue-status"),
  schedulerPause: (paused: boolean) =>
    req<{ scheduler_paused: boolean }>("/jobs/scheduler-pause", { method: "POST", body: JSON.stringify({ paused }) }),
  setMaxConcurrent: (max_concurrent: number) =>
    req<{ max_concurrent?: number; error?: string }>("/jobs/max-concurrent", { method: "POST", body: JSON.stringify({ max_concurrent }) }),
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
  linkAtSelection: (body: { doc_id: string; target_id: string; selected_text: string; context?: string; pinpoint?: string }) =>
    req<any>("/link-at-selection", { method: "POST", body: JSON.stringify(body) }),
  attach: async (doc_id: string, file: File, kind: string) => {
    const fd = new FormData();
    fd.append("file", file);
    fd.append("kind", kind);
    return postForm(`/documents/${encodeURIComponent(doc_id)}/attach`, fd);
  },
  getSettings: () => req<{ settings: Setting[]; path: string }>("/settings"),
  saveSettings: (values: Record<string, string>) =>
    req<{ settings: Setting[] }>("/settings", { method: "POST", body: JSON.stringify(values) }),
  // recurring scheduler tasks (enabled + cadence), e.g. the static-export folder refresh
  scheduledTasks: () => req<any>("/scheduled-tasks"),
  setScheduledTask: (body: Record<string, unknown>) =>
    req<any>("/scheduled-tasks", { method: "POST", body: JSON.stringify(body) }),
  // -- static export bundle (a set of editions + an index page) ---------------
  bundleConfig: () => req<StaticBundle>("/export/bundle"),
  saveBundleConfig: (body: Record<string, unknown>) =>
    req<StaticBundle>("/export/bundle", { method: "POST", body: JSON.stringify(body) }),
  // Fire the configured webhook once against the LAST run's summary, so the operator can
  // see the request land before trusting a scheduled export to it.
  testBundleWebhook: () =>
    req<any>("/export/bundle/webhook-test", { method: "POST", body: "{}" }),
  // Build every configured edition, then (when a zip was asked for) download it. The
  // build is a job because it reads thousands of source texts per statute; progress is
  // reported per edition AND within one, so a long run stays legible.
  buildBundle: async (
    opts: { zip?: boolean; refresh?: boolean },
    onProgress?: (p: { message: string; fraction: number }) => void,
  ) => {
    const zip = opts.zip !== false;
    onProgress?.({ message: "Starting the export…", fraction: 0 });
    const start = await req<any>("/export/bundle/build", {
      method: "POST",
      body: JSON.stringify({ zip, refresh: opts.refresh !== false }),
    });
    if (start.error) throw new Error(start.error);
    let result: any = null;
    if (start.job_id) {
      while (true) {
        const job = await req<any>(`/jobs/${encodeURIComponent(start.job_id)}`);
        if (job.status === "done") { result = job.result; break; }
        if (job.status === "error") throw new Error(job.result?.error || job.error || "The export failed");
        if (job.status === "cancelled") throw new Error("The export was cancelled");
        const p = job.progress || {};
        const done = Number(p.done || 0), total = Number(p.total || 0);
        // The bar counts editions; within one, the excerpt pass fills the step — so it
        // keeps moving through the hours a single heavily-cited statute can take.
        const sub = Number(p.sub_total || 0) > 0 ? Number(p.sub_done || 0) / Number(p.sub_total) : 0;
        onProgress?.({
          message: p.item ? String(p.item) : (job.status === "queued" ? "Waiting to start…" : "Preparing…"),
          fraction: total > 0 ? Math.min(1, (done + sub) / total) : 0,
        });
        await new Promise((r) => window.setTimeout(r, 1000));
      }
    }
    if (result?.error) throw new Error(result.error);
    if (zip) {
      onProgress?.({ message: "Downloading the zip…", fraction: 1 });
      const res = await fetch(`${BASE}/export/bundle.zip`, { credentials: "include", headers: authHeaders() });
      if (res.status === 401) window.dispatchEvent(new CustomEvent("raglex-unauthenticated"));
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
      const name = (res.headers.get("content-disposition") || "").match(/filename="([^"]+)"/i)?.[1]
        || "raglex-static-export.zip";
      const url = URL.createObjectURL(await res.blob());
      const a = document.createElement("a");
      a.href = url; a.download = name;
      document.body.appendChild(a); a.click(); a.remove();
      URL.revokeObjectURL(url);
    }
    return result || {};
  },
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
  importOptions: () => req<ImportOptions>("/import/options"),
  // One request for the whole drop: the files in order, and a parallel array of the
  // rows the operator filled in for them.
  importFiles: async (files: File[], items: ImportItem[]) => {
    const fd = new FormData();
    files.forEach((f) => fd.append("files", f));
    fd.append("items", JSON.stringify(items));
    return postForm("/import/files", fd) as Promise<ImportBatchResult>;
  },
  importLegislationAkn: async (file: File, stableId?: string) => {
    const fd = new FormData();
    fd.append("file", file);
    if (stableId) fd.append("stable_id", stableId);
    return postForm("/import/legislation-akn", fd) as Promise<{ stable_id?: string; title?: string; chars?: number; segments?: number; resolved_edges?: number; error?: string }>;
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
  rebuildCounts: () => req<any>("/jobs/rebuild-citation-counts", { method: "POST", body: "{}" }),
  drill: (params: Record<string, string>) => req<any>(`/drill?${new URLSearchParams(params)}`),
  exportRetrievalCitations: (p: { min_citing?: number; batch_size?: number; include_names?: boolean; separator?: string; series?: string; jurisdictions?: string } = {}) =>
    req<any>(`/export/retrieval-citations?${new URLSearchParams(Object.entries(p).filter(([, v]) => v !== undefined && v !== "").map(([k, v]) => [k, String(v)]))}`),
  embed: () => req<any>("/embed", { method: "POST", body: "{}" }),
};
