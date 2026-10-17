import { useEffect, useState } from "react";

/** A hash router, in thirty lines, because the app needs exactly one thing from a router:
 *  addressable pages.
 *
 *  The old UI held the current tab in a `useState` inside App, which meant no page could be
 *  linked to, the back button did nothing, and a reload always dumped you on the Overview — so
 *  "look at this failing title" was a set of instructions rather than a URL. A hash route also
 *  keeps the SPA fallback trivial: every path is index.html and the server needs no route table.
 *
 *  Routes look like "#/films" or "#/problems/rate_mismatch?q=alien".
 */
export interface Route { path: string[]; query: URLSearchParams; hash: string; }

function parse(): Route {
  const raw = window.location.hash.replace(/^#\/?/, "");
  const [p, q] = raw.split("?");
  return {
    path: p ? p.split("/").filter(Boolean).map(decodeURIComponent) : [],
    query: new URLSearchParams(q || ""),
    hash: raw,
  };
}

export function useRoute(): Route {
  const [r, setR] = useState<Route>(parse);
  useEffect(() => {
    const on = () => setR(parse());
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return r;
}

/** Navigate. `replace` avoids stacking history entries for things like a filter change, where
 *  twenty back-presses to escape a page is worse than no history at all. */
export function go(to: string, replace = false) {
  const h = "#/" + to.replace(/^\/+/, "");
  if (replace) window.history.replaceState(null, "", h);
  else window.location.hash = h;
  if (replace) window.dispatchEvent(new HashChangeEvent("hashchange"));
}

/** Keep one query parameter in the URL, so a filtered list can be shared or reloaded. */
export function setParam(key: string, value: string | null, base?: string) {
  const r = parse();
  const q = new URLSearchParams(r.query);
  if (value == null || value === "") q.delete(key); else q.set(key, value);
  const path = base ?? r.path.join("/");
  const s = q.toString();
  go(path + (s ? "?" + s : ""), true);
}
