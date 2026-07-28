// Real URLs for everything you can open, so the BROWSER's own navigation works.
//
// Every document, case and provision in RagLex was reachable only through a React click
// handler — an <a> or <button> with an onClick and no href. That is invisible to the
// browser: ⌘-click, middle-click, "Open link in new tab", "Copy link address" and drag-to-
// bookmark all did nothing, so a reader who wanted to look at a citing case *while keeping
// the judgment open* had no way to ask for it (the feedback that prompted this: "I wanted
// to navigate from case to most significant citor while still having original case open").
//
// The fix is not a router rewrite. Each link gets the href it always should have had — the
// same hash URL the app already writes for the open document — and keeps its click handler
// for the in-app path. A plain left click is intercepted as before (no reload, no lost
// scroll position, trays still open as trays); a click that ASKS for a new tab is simply
// not intercepted, and the browser does what it does everywhere else on the web.
//
// The one rule when adding a link: if it navigates to something with a URL, use DocLink
// (or an <a href={…}>), never a bare onClick.

import type { MouseEvent, ReactNode } from "react";

export const slug = (s: string) =>
  (s || "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");

/** The canonical deep link for a document, optionally pinpointed to one section.
 *  Must stay identical to what App writes into location.hash, or the address bar and the
 *  links pointing at the same place would disagree. */
export function docHref(id?: string | null, anchor?: string | null): string {
  if (!id) return "#";
  return `#/article/${encodeURIComponent(id)}` + (anchor ? `/section/${slug(anchor)}` : "");
}

/** The citation-network view of a document. */
export function graphHref(id?: string | null): string {
  return id ? `#/graph/${encodeURIComponent(id)}` : "#";
}

/** A top-level section of the app (explore, search, admin…). */
export function tabHref(tab: string): string {
  return `#/${tab}`;
}

/** Is this click a request for a NEW TAB/WINDOW rather than in-app navigation?
 *  ⌘ (mac), Ctrl (win/linux), Shift (new window), Alt (download), or a middle click. */
export function opensNewTab(e: MouseEvent): boolean {
  return e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button === 1;
}

type DocLinkProps = {
  id?: string | null;
  anchor?: string | null;
  /** What a plain left click should do in-app — open the reader, push a tray, … */
  onOpen?: () => void;
  /** Point at the graph view instead of the reader (the href changes with it). */
  graph?: boolean;
  className?: string;
  title?: string;
  style?: React.CSSProperties;
  children?: ReactNode;
};

/** A link to a document that behaves like a link. Renders a real anchor with the
 *  document's URL; a plain click runs ``onOpen`` in-app, a modified click is left to the
 *  browser so it opens a tab. Without ``onOpen`` it is an ordinary hash link (the app's
 *  own hashchange listener adopts it). */
export function DocLink({ id, anchor, onOpen, graph, children, ...rest }: DocLinkProps) {
  const href = graph ? graphHref(id) : docHref(id, anchor);
  return (
    <a
      href={href}
      onClick={(e) => {
        if (opensNewTab(e)) return;   // the browser is being asked for a tab — let it
        if (!onOpen) return;          // a plain hash link: the router picks it up
        e.preventDefault();
        onOpen();
      }}
      {...rest}
    >
      {children}
    </a>
  );
}
