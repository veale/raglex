// Shared behaviour for every type-ahead in the app.
//
// Each dropdown asked for a fixed handful — six on the Explore hero, eight in the search
// bar and in DocAutocomplete — and then simply stopped. That is fine when the handful
// contains what you wanted and useless when it does not: "Investigatory Powers" matches
// 140 documents in the corpus and the box would show eight of them with no hint that the
// other 132 existed, so the only way on was to abandon the field and run a full search.
//
// So the batch is a floor, not a ceiling: when a batch comes back FULL the list offers to
// fetch another, and when it comes back short it says nothing, because a short batch is
// the end of the matches and a "show more" there would be a lie.
//
// The extra row is a real member of the list for the keyboard too. ArrowDown reaches it
// and Enter activates it; without that, "show more" would be a mouse-only affordance in a
// control whose whole point is that you never leave the keyboard.

import { useCallback, useEffect, useRef, useState } from "react";

export interface Autosuggest<T> {
  items: T[];
  /** the last batch came back full — there is probably more behind it */
  hasMore: boolean;
  loading: boolean;
  showMore: () => void;
  /** highlight index; `items.length` is the "show more" row */
  hi: number;
  setHi: (n: number) => void;
  /** ArrowUp/ArrowDown/Escape over the list. Returns true if it handled the key. */
  onNavKey: (e: { key: string; preventDefault: () => void }) => boolean;
  /** the row `hi` points at, or undefined when it points at "show more" / nothing */
  highlighted: T | undefined;
  /** true when the highlight is on the "show more" row */
  onMoreRow: boolean;
  clear: () => void;
}

export function useAutosuggest<T>(
  query: string,
  fetchPage: (limit: number) => Promise<T[]>,
  opts: { minChars?: number; batch?: number; delay?: number; enabled?: boolean } = {},
): Autosuggest<T> {
  const { minChars = 2, batch = 8, delay = 130, enabled = true } = opts;
  const [items, setItems] = useState<T[]>([]);
  const [limit, setLimit] = useState(batch);
  const [loading, setLoading] = useState(false);
  const [hi, setHi] = useState(-1);
  // the caller rebuilds this closure every render; keeping it in a ref stops it from
  // re-triggering the effect on keystrokes that did not change the query
  const fetcher = useRef(fetchPage);
  fetcher.current = fetchPage;

  // a NEW query is a new batch — otherwise typing one more letter would keep whatever
  // enlarged limit the previous query had been expanded to
  useEffect(() => { setLimit(batch); setHi(-1); }, [query, batch, enabled]);

  useEffect(() => {
    let live = true;
    if (!enabled || query.trim().length < minChars) { setItems([]); setLoading(false); return; }
    setLoading(true);
    const t = setTimeout(async () => {
      try {
        const rows = await fetcher.current(limit);
        if (live) setItems(rows || []);
      } catch { /* a failed suggestion is not an error worth showing */ }
      finally { if (live) setLoading(false); }
    }, delay);
    return () => { live = false; clearTimeout(t); };
  }, [query, limit, minChars, delay, enabled]);

  const hasMore = items.length >= limit;
  const rows = items.length + (hasMore ? 1 : 0);

  const onNavKey = useCallback((e: { key: string; preventDefault: () => void }) => {
    if (e.key === "ArrowDown") { e.preventDefault(); setHi((h) => Math.min(h + 1, rows - 1)); return true; }
    if (e.key === "ArrowUp") { e.preventDefault(); setHi((h) => Math.max(h - 1, -1)); return true; }
    if (e.key === "Escape") { setItems([]); setHi(-1); return true; }
    return false;
  }, [rows]);

  const onMoreRow = hasMore && hi === items.length;
  return {
    items, hasMore, loading, hi, setHi, onNavKey, onMoreRow,
    highlighted: hi >= 0 && hi < items.length ? items[hi] : undefined,
    showMore: () => setLimit((n) => n + batch),
    clear: () => { setItems([]); setHi(-1); },
  };
}

/** The row at the foot of a full batch. Rendered inside `.ac-list`, after the options. */
export function AcMore({ onClick, loading, hi, onHover }:
  { onClick: () => void; loading?: boolean; hi?: boolean; onHover?: () => void }) {
  return (
    <div className={`ac-opt ac-more${hi ? " hi" : ""}`} role="option" aria-selected={!!hi}
      onMouseEnter={onHover}
      onMouseDown={(e) => { e.preventDefault(); onClick(); }}>
      {loading ? "finding more…" : "(show more)"}
    </div>
  );
}
