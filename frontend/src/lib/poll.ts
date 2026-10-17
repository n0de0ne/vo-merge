import { useEffect, useRef, useState } from "react";

/* ---------------------------------------------------------------- error reporting
   Failures used to be swallowed twice over: the act() helpers were try/finally with no catch, so
   a failed grab/retry/merge click told the user nothing, and usePoll invoked its refresh uncaught,
   so a backend that was down produced an unhandled rejection every few seconds behind a silently
   stale screen. One subscriber-based sink means every one of those surfaces in the same banner,
   and it lives outside React so api.ts and non-component code can report too. */
type ErrSink = (msg: string | null) => void;
const errSinks = new Set<ErrSink>();

export function reportError(e: unknown) {
  if (e && (e as Error).name === "Unauthorized") { errSinks.forEach(f => f("__auth__")); return; }
  const msg = e instanceof Error ? e.message : String(e);
  console.error(e);
  errSinks.forEach(f => f(msg));
}

export function useErrorSink() {
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    const f: ErrSink = m => setErr(m);
    errSinks.add(f);
    return () => { errSinks.delete(f); };
  }, []);
  return [err, setErr] as const;
}

/** Run an action, surfacing any failure instead of dropping it. */
export async function runAction(fn: () => Promise<unknown>) {
  try { await fn(); return true; } catch (e) { reportError(e); return false; }
}

/** Poll `fn` every `ms`, but ONLY while the tab is visible.
 *
 *  Browsers throttle background-tab timers, so a tab left open would silently go stale (the
 *  "won't update without a reload" complaint). We pause while hidden and fire an immediate
 *  refresh the moment the tab is focused again. `deps` re-arms the loop (e.g. a changed filter). */
export function usePoll(fn: () => void, ms: number, deps: any[] = []) {
  const saved = useRef(fn);
  saved.current = fn;
  useEffect(() => {
    let alive = true;
    // Catch here, not at each call site: every refresh function used to be invoked uncaught, so
    // a backend that is down produced an unhandled rejection on every tick behind a screen
    // showing stale data. Reporting once, centrally, makes "the list stopped updating" visible.
    const run = () => {
      if (!alive || document.hidden) return;
      try {
        const r = saved.current() as unknown;
        if (r && typeof (r as Promise<unknown>).catch === "function")
          (r as Promise<unknown>).catch(reportError);
      } catch (e) { reportError(e); }
    };
    run();
    const id = setInterval(run, ms);
    const onVis = () => { if (!document.hidden) run(); };
    document.addEventListener("visibilitychange", onVis);
    window.addEventListener("focus", onVis);
    return () => {
      alive = false; clearInterval(id);
      document.removeEventListener("visibilitychange", onVis);
      window.removeEventListener("focus", onVis);
    };
    /* eslint-disable-next-line */
  }, [ms, ...deps]);
}

/** A value kept in localStorage. Used for view preferences (grid vs list, advanced settings on),
 *  which must survive a reload or they are not preferences at all. Private-mode safe. */
export function useStored<T>(key: string, initial: T) {
  const [v, setV] = useState<T>(() => {
    try {
      const raw = localStorage.getItem(key);
      return raw == null ? initial : (JSON.parse(raw) as T);
    } catch { return initial; }
  });
  const set = (next: T) => {
    setV(next);
    try { localStorage.setItem(key, JSON.stringify(next)); } catch { /* private mode */ }
  };
  return [v, set] as const;
}
