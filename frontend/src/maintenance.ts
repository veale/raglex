// What the Maintain page can DO, described rather than merely offered.
//
// The page used to be a single row of fourteen equal-looking buttons, with the answer to
// "would I ever press this?" living only in a title attribute — and several of them were
// migrations run once, for a defect fixed for good, that will now never find anything
// again. An operator cannot tell those apart from the nightly essentials by looking, so
// every one of them read as a standing obligation.
//
// So each action carries its cadence and its own explanation, and the UI groups by
// cadence: routine work first, one-off migrations folded away. `kind` is the job kind the
// API starts; combined with the last-run feed (/jobs/last-run) a repair that reported
// nothing to fix can be shown as spent instead of pending.

export type Cadence =
  | "scheduled"    // the scheduler already runs it; the button only forces it early
  | "routine"      // you may want this from time to time, on purpose
  | "occasional"   // for a specific situation — an interrupted import, a new source
  | "one-off";     // a migration for a defect since fixed; kept for a rebuild or a re-import

export type Action = {
  kind: string;
  params?: Record<string, unknown>;
  /** Non-default start path, for the few actions that predate /jobs/<kind>. */
  endpoint?: string;
  label: string;
  what: string;              // what it does, in one sentence
  when: string;              // when you would actually want it
  cadence: Cadence;
  group: string;
  /** Runs over the whole corpus — hours, not minutes. Worth saying before it is pressed. */
  heavy?: boolean;
};

export const CADENCE_META: Record<Cadence, { label: string; hint: string; tone: string }> = {
  scheduled: {
    label: "Runs itself",
    hint: "The background scheduler already does this on a cadence. The button only forces one now — you should not need it.",
    tone: "ok",
  },
  routine: {
    label: "Routine",
    hint: "Ordinary upkeep you might choose to run: it acts on whatever has accumulated since last time.",
    tone: "",
  },
  occasional: {
    label: "For a situation",
    hint: "Not upkeep — the answer to something specific that has happened, like an import that was cancelled part-way.",
    tone: "",
  },
  "one-off": {
    label: "Migration — probably spent",
    hint: "A repair for a defect that has since been fixed at the source. It was run when the fix landed and normally finds nothing now; it is kept because a re-import of old data can reintroduce the defect.",
    tone: "muted",
  },
};

export const ACTIONS: Action[] = [
  // -- derived layers ------------------------------------------------------
  {
    kind: "rebuild-citation-counts",
    label: "Rebuild citation counts",
    group: "Derived layers",
    cadence: "scheduled",
    what: "Recomputes the citation-frequency roll-up that ranks the harvest worklist and the 'most cited' sorts.",
    when: "It runs on a schedule and after big imports. Force it if a fresh import's counts look stale.",
  },
  {
    kind: "rebuild-authority",
    label: "Rebuild authority (PageRank)",
    group: "Derived layers",
    cadence: "scheduled",
    heavy: true,
    what: "Recomputes the PageRank score over the whole citation graph — it feeds search ranking, 'most authoritative' sort, the citator strip and related documents.",
    when: "Weekly by schedule. A document is found and read perfectly well with a stale score, so this is a ranking refresh, not a correctness fix. Forcing it on a busy box is the classic way to make everything feel slow.",
  },
  {
    // started via POST /embed rather than /jobs/<kind> — see `endpoint`
    kind: "embed",
    endpoint: "/embed",
    label: "Embed pending documents",
    group: "Derived layers",
    cadence: "routine",
    heavy: true,
    what: "Gives vectors to documents that have text but no embedding, which is what puts them into semantic search.",
    when: "After any import, if semantic search is in use. A document with no vector is genuinely absent from semantic results until it has one.",
  },

  // -- citation extraction -------------------------------------------------
  {
    kind: "rescan",
    params: { only_unextracted: true },
    label: "Finish extraction (everything unscanned)",
    group: "Citation extraction",
    cadence: "occasional",
    what: "Extracts citations from every document that has none yet, whatever its source. Never re-touches a document already scanned.",
    when: "After a bulk import whose extraction phase was cancelled. This is the cheap, safe one — it only ever does work nobody has done.",
  },
  {
    kind: "rescan",
    params: { stale_days: 7 },
    label: "Re-scan anything older than a week",
    group: "Citation extraction",
    cadence: "routine",
    heavy: true,
    what: "Re-extracts documents not scanned in the last seven days, then runs the resolution chain.",
    when: "After a grammar or alias change, to spread the re-read over time rather than redoing the corpus at once.",
  },
  {
    kind: "rescan",
    params: { doc_types: ["judgment"] },
    label: "Re-scan every judgment",
    group: "Citation extraction",
    cadence: "occasional",
    heavy: true,
    what: "Re-extracts all judgments (skipping the far larger legislation set), then resolves. Never-scanned documents go first; anything done in the last 7 days is skipped.",
    when: "After a change to case-citation grammar specifically.",
  },
  {
    kind: "rescan",
    params: {},
    label: "Re-scan the entire corpus",
    group: "Citation extraction",
    cadence: "occasional",
    heavy: true,
    what: "Re-extracts every document including legislation, then runs the whole resolution chain.",
    when: "Rarely, and not while anything else matters — this is the heaviest thing the box can be asked to do. Prefer 'documents that mention X' from the Rules page when a specific shorthand changed.",
  },
  {
    kind: "finish-bulk-postprocess",
    label: "Finish an interrupted import's resolve + tag",
    group: "Citation extraction",
    cadence: "occasional",
    what: "Runs only the resolve and tag phases of a bulk import, without redoing discovery or extraction. Checkpointed, so it is safe to cancel and restart.",
    when: "After cancelling a huge harvest whose extraction had already finished — it continues from its saved cursor rather than starting the walk again.",
  },

  // -- corpus growth -------------------------------------------------------
  {
    kind: "expand-citing",
    label: "Pull in cited-but-missing authorities",
    group: "Corpus growth",
    cadence: "routine",
    what: "Follows citations out of held documents and fetches the authorities they point at but the corpus does not hold.",
    when: "Whenever you want the corpus to close in on itself. The nightly drain does the routable part of this already.",
  },
  {
    kind: "harvest-echr",
    label: "Harvest missing ECHR judgments",
    group: "Corpus growth",
    cadence: "routine",
    what: "Fetches Strasbourg judgments the corpus cites but does not hold.",
    when: "When the unresolved queue is showing ECHR gaps.",
  },
  {
    kind: "canlii-enrich",
    params: { limit: 200 },
    label: "Enrich Canadian decisions (CanLII)",
    group: "Corpus growth",
    cadence: "routine",
    what: "Decorates held Canadian decisions with CanLII metadata and citator edges. Budget-metered, and each checked case is stamped so a re-run walks on.",
    when: "Periodically, while the CanLII budget allows — it is paced deliberately.",
  },
  {
    kind: "backfill-eu-case-names",
    label: "Fill in CJEU case names",
    group: "Corpus growth",
    cadence: "scheduled",
    what: "Pulls official case names and subject-matter tags from the EUR-Lex webservice, which the free CELLAR data omits.",
    when: "Runs daily when the 'eu-case-names' task is enabled and credentials are set. Without it, CJEU citations read as bare case numbers.",
  },
  {
    // the operator-facing whole-catalogue walk is the backfill job; the per-act
    // sync-eu-consolidations kind is what a reader opening an act triggers automatically
    kind: "backfill-eu-consolidations",
    label: "Import EU dated consolidations",
    group: "Corpus growth",
    cadence: "scheduled",
    what: "Walks CELLAR's sector-0 catalogue for dated versions of EU acts, including future-effective snapshots.",
    when: "Weekly, and automatically when a reader opens an EU act whose lineage is missing. You rarely need to force it.",
  },

  // -- one-off migrations --------------------------------------------------
  // Each of these exists because a specific defect was found and fixed. The repair
  // cleared what the old code had already written. They are kept, not deleted, because
  // re-importing old raw data can write the same defect again — but on a corpus that has
  // already been through them they normally report nothing.
  {
    kind: "backfill-metadata",
    label: "Repair document metadata",
    group: "One-off migrations",
    cadence: "one-off",
    what: "Fills in title, court and date fields on documents stored before those columns were populated properly.",
    when: "After restoring an old dump, or importing a corpus captured before the metadata fix.",
  },
  {
    kind: "backfill-edge-keys",
    label: "Backfill citation edge keys",
    group: "One-off migrations",
    cadence: "one-off",
    what: "Populates candidate_id and raw_fold on citation edges written before those columns existed, so set-based resolution can see them.",
    when: "Once, after the upgrade that added the columns. A corpus built since then never needs it.",
  },
  {
    kind: "repair-eu-annexes",
    label: "Repair split EU annexes",
    group: "One-off migrations",
    cadence: "one-off",
    heavy: true,
    what: "Reparses held EU Formex packages whose annexes were stored as separate XML members, then re-extracts citations from the corrected text.",
    when: "Once, for packages fetched before the Formex parser learned to merge annex parts.",
  },
  {
    kind: "repair-de-citations",
    label: "Re-validate German citations",
    group: "One-off migrations",
    cadence: "one-off",
    what: "Re-checks German citations against the current grammar and drops the ones it would no longer mint.",
    when: "After a German grammar fix — this is the standing migration for it.",
  },
  {
    kind: "repair-de-renditions",
    label: "Fold duplicate German judgments",
    group: "One-off migrations",
    cadence: "one-off",
    what: "Merges a second register's copies of judgments already held back onto the originals.",
    when: "Once, after the two German registers were found to be forking the same judgment into two documents.",
  },
  {
    kind: "repair-eu-repeals",
    label: "Re-check EU implicit repeals",
    group: "One-off migrations",
    cadence: "one-off",
    what: "Re-asks CELLAR which 'repeals' edges were only implicit, and corrects the ones recorded as express.",
    when: "Once, after the distinction was added.",
  },
  {
    kind: "repair-mojibake",
    label: "Repair mojibake",
    group: "One-off migrations",
    cadence: "one-off",
    what: "Fixes text mangled by a double encoding at fetch time.",
    when: "Once, for documents fetched before the encoding fix. New fetches cannot produce it.",
  },
  {
    kind: "repair-oj-wrapper-notices",
    label: "Re-fetch OJ wrapper notices",
    group: "One-off migrations",
    cadence: "one-off",
    what: "Re-fetches EU notices that were stored as the Official Journal issue's masthead rather than the notice itself — their stored raw is the wrapper, so no local reparse can recover them.",
    when: "Once, for notices captured before the OJ archive reader was corrected.",
  },
  {
    kind: "rescan-contested-shorthands",
    label: "Clear contested shorthand edges",
    group: "One-off migrations",
    cadence: "one-off",
    what: "Re-reads documents carrying an edge from a learned shorthand the store holds against several candidates — a name that used to be applied on the coincidence that the document cited one of them.",
    when: "Once, after the shorthand rule was fixed. This clears what the old rule already wrote.",
  },
  {
    kind: "repair-fts-positions",
    label: "Repair free-text index positions",
    group: "One-off migrations",
    cadence: "one-off",
    what: "Reindexes free-text entries whose stored positions were truncated, which made phrase search miss inside long judgments.",
    when: "Once, after the long-judgment indexing fix.",
  },
];

export const GROUP_ORDER = [
  "Derived layers",
  "Citation extraction",
  "Corpus growth",
  "One-off migrations",
];
