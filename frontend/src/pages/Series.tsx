import { useEffect, useMemo, useState } from "react";
import { api, type DL, type Episode, type RecheckState, type RescanState } from "../api";
import { runAction, usePoll, useStored } from "../lib/poll";
import { setParam, useRoute } from "../lib/router";
import { fmtNum, fmtSE } from "../lib/format";
import {
  Act, AiPill, DownloadBar, DriftBadge, Empty, LiveDot, Pill, Poster, QueuedLine,
  STATE_LABEL, Tracks, TV_STATES,
} from "../components/ui";
import { LipSyncModal, ReleaseModal } from "../components/modals";

/* ======================================================================
   Series — the 🎌 Anime and 📺 TV Shows pages, one component.

   The two differ only in which half of Sonarr they show (`series_type === "anime"`), and the
   backend partitions the same way for scans and re-checks, so a second component would be two
   copies of the most feature-dense screen in the app kept in sync by hand.

   The unit of work here is a SHOW, not an episode: a season pack is one grab, a numbering
   mismatch is one show's problem, and "Blue Lock has 24 episodes missing English" is the sentence
   an operator actually thinks in. So the list is one line per show carrying its states as counted
   pills, and the episodes — fifty rows of near-identical text — only exist once you open it.
   ====================================================================== */

const stateLabel = (s: string) => STATE_LABEL[s] ?? s.replace("_", " ");

interface Season { season: number; eps: Episode[]; byStatus: Record<string, number>; }
interface Show {
  key: string; sid: number; title: string; poster: string | null;
  eps: Episode[]; byStatus: Record<string, number>; seasons: Season[]; prio: boolean;
}

/** A show's (or season's) states, counted. The whole point of the collapsed row: fifty episodes
 *  in one line you can read, instead of fifty rows you have to scroll past. Clicking one filters
 *  the page to that state — the old chips were decorative, which made the count a dead end. */
function StatePills({ counts, onPick }:
  { counts: Record<string, number>; onPick: (s: string) => void }) {
  return (
    <span className="chips">
      {TV_STATES.filter(s => counts[s]).map(s => (
        <button key={s} className={`pill ${s}`} style={{ border: 0, font: "inherit", cursor: "pointer" }}
          title={`show only the ${stateLabel(s)} episodes`}
          onClick={e => { e.stopPropagation(); onPick(s); }}>
          {counts[s]} {stateLabel(s)}
        </button>
      ))}
    </span>
  );
}

/* ---------------------------------------------------------------- library reads
   Two ways to read the files, and the difference only shows after an interruption:

     progressive — keeps the probe cache, so mkvmerge runs ONLY for files with no valid probe:
                   never read, changed on disk, or never reached because a previous pass was cut
                   short. Resumable by construction, so it is cheap to run any time.
     full        — drops the cache for that scope first and reads everything again. What you want
                   when you don't trust the cached answer, not when you're filling gaps. */
function RescanButton({ scope, label, full }:
  { scope: "anime" | "series"; label: string; full?: boolean }) {
  const [st, setSt] = useState<RescanState | null>(null);
  const [note, setNote] = useState("");
  usePoll(() => api.rescanState().then(setSt).catch(() => {}), st?.running ? 3000 : 30000,
    [st?.running]);

  // One scan runs at a time (SCAN_LOCK), so a pass started from another tab disables this one…
  const mine = st?.scope === scope;
  const running = !!st?.running;
  // …and the two buttons for one scope share that one state object, so each must report only on
  // the mode that actually ran — otherwise "Scan new" claims the credit for a full re-read.
  const last = st && mine && !running && st.finished > 0 && !!st.full === !!full ? st : null;
  const dropped = (st?.pruned ?? 0) + (st?.pruned_records ?? 0);

  return (
    <>
      <Act cls="btn sec" disabled={running}
        busyLabel={full ? "Re-reading…" : "Scanning…"}
        title={full
          ? `Read every ${label} file again with mkvmerge, even ones that look unchanged, and `
            + "re-decide what each is missing. Use when you don't trust the cached answer. "
            + "Takes a few minutes on a big library."
          : `Read only the ${label} files that have no result yet — new imports, files changed on `
            + "disk, and anything an interrupted scan never reached. Picks up where the last pass "
            + "stopped, so it is cheap to run any time."}
        run={async () => {
          // started:false (SCAN_LOCK held, often by the hourly job, which never sets SCAN_STATE
          // so `running` is false here) must be said out loud — see Films.tsx.
          await runAction(async () => {
            const r = await api.rescan(scope, !!full);
            setNote(r.started ? "" : (r.note || "a scan is already running"));
            setSt(await api.rescanState());
          });
        }}>
        {running && mine ? (full ? "Re-reading…" : "Scanning…") : full ? `Re-read ${label}` : `Scan new ${label}`}
      </Act>
      {note && <span className="warn">{note}</span>}
      {running && mine && st && <span className="muted">
        {st.phase}… {(st.read ?? 0) > 0 && <>· read {fmtNum(st.read)}</>}
        {(st.reused ?? 0) > 0 && <> · reused {fmtNum(st.reused)}</>}</span>}
      {running && !mine && st && <span className="muted">busy: {st.scope} scan running</span>}
      {last && !last.error && <span className="muted">
        last: read {fmtNum(last.read ?? 0)} file(s)
        {(last.reused ?? 0) > 0 && <> · {fmtNum(last.reused)} already cached</>}
        {" "}· {last.episodes ?? "?"} gap(s)
        {dropped > 0 && <> · {dropped} deleted entr{dropped === 1 ? "y" : "ies"} removed</>}
        {last.probes.unreadable > 0 && <span className="bad"> · {last.probes.unreadable} unreadable</span>}
      </span>}
      {mine && st?.error && <span className="bad">rescan failed: {st.error}</span>}
    </>
  );
}

/** "merged" only means a merge ran, and "no release" only means nothing existed when we last
 *  looked. This re-reads the files parked in those states and re-opens the ones still short. */
function RecheckButton({ scope }: { scope: "anime" | "series" }) {
  const [st, setSt] = useState<RecheckState | null>(null);
  const [note, setNote] = useState("");
  usePoll(() => api.recheckState().then(setSt).catch(() => {}), st?.running ? 3000 : 60000,
    [st?.running]);
  const mine = st?.scope === scope;
  const running = !!st?.running;
  const last = st && mine && !running && st.finished > 0 ? st : null;
  return (
    <>
      <Act cls="btn sec" disabled={running} busyLabel="Re-checking…"
        title={"Re-read every file parked in a finished state — merged, or no-release — and "
          + "re-open the ones that still don't meet their target. The release already tried "
          + "stays blocklisted, so a re-opened record searches for a different one; ignored "
          + "titles are left alone."}
        run={async () => {
          await runAction(async () => {
            const r = await api.recheck(scope);
            setNote(r.started ? "" : (r.note || "a scan or re-check is already running"));
            setSt(await api.recheckState());
          });
        }}>
        {running && mine ? "Re-checking…" : "Re-check finished"}
      </Act>
      {note && <span className="warn">{note}</span>}
      {running && mine && st && <span className="muted">
        re-probed {fmtNum(st.checked)} of {fmtNum(st.total)} · re-opened {st.reopened}</span>}
      {last && !last.error && <span className="muted">
        last: {last.reopened} re-opened of {fmtNum(last.total)} · {last.complete} genuinely complete
        {last.gone > 0 && <> · {last.gone} file(s) gone</>}
        {last.unreadable > 0 && <span className="bad"> · {last.unreadable} unreadable</span>}</span>}
      {mine && st?.error && <span className="bad">re-check failed: {st.error}</span>}
    </>
  );
}

export default function Series({ anime }: { anime: boolean }) {
  const route = useRoute();
  const status = route.query.get("status") ?? "";
  const urlQ = route.query.get("q") ?? "";
  const scope = anime ? "anime" : "series";
  const label = anime ? "anime" : "TV shows";

  const [eps, setEps] = useState<Episode[]>([]);
  const [dls, setDls] = useState<Record<string, DL>>({});
  const [text, setText] = useState(urlQ);
  const [view, setView] = useStored<"grid" | "list">("vo_tv_view", "list");
  const [open, setOpen] = useState<Set<string>>(new Set());
  const [sel, setSel] = useState<Set<string>>(new Set());
  const [msg, setMsg] = useState("");
  const [relSeries, setRelSeries] = useState<Show | null>(null);
  const [relSeason, setRelSeason] = useState<{ sh: Show; season: number } | null>(null);
  const [relEp, setRelEp] = useState<Episode | null>(null);
  const [lips, setLips] = useState<Episode | null>(null);

  /* The whole episode table, unfiltered, then split in the browser. Not a shortcut: the status
     filter has to be applied AFTER the per-kind split anyway, and fetching filtered made the
     state chips lie (every other state read 0 while a filter was on) and blanked the list on
     every filter change. Same request either way. */
  const refresh = () => api.tvEpisodes().then(setEps);
  usePoll(refresh, 8000);
  usePoll(() => api.downloads().then(d => setDls(d.items || {})).catch(() => {}), 4000);

  // The URL is the source of truth for the search, so the box follows it when it changes
  // underneath — a Back press, or someone else's shared link.
  useEffect(() => { setText(urlQ); }, [urlQ]);
  useEffect(() => {
    if (text === urlQ) return;
    const t = setTimeout(() => setParam("q", text || null), 300);
    return () => clearTimeout(t);
  }, [text, urlQ]);
  // A selection names rows on screen; changing what is on screen must not leave it pointing at
  // records the operator can no longer see.
  useEffect(() => { setSel(new Set()); }, [status, urlQ]);

  const dlOf = (e: Episode) => dls[(e.dl_hash || "").toLowerCase()];
  const toggle = (k: string) =>
    setOpen(o => { const n = new Set(o); n.has(k) ? n.delete(k) : n.add(k); return n; });

  /** Every action goes through runAction (failures reach the banner instead of being swallowed)
   *  and re-reads afterwards, so the row reflects what the server actually did. */
  async function act(fn: () => Promise<unknown>) {
    await runAction(fn);
    await runAction(refresh);
  }

  const kindEps = useMemo(
    () => eps.filter(e => ((e.series_type || "") === "anime") === anime), [eps, anime]);
  const counts = useMemo(() => {
    const c: Record<string, number> = {};
    for (const e of kindEps) c[e.status] = (c[e.status] || 0) + 1;
    return c;
  }, [kindEps]);

  const shows = useMemo<Show[]>(() => {
    const tally = (list: Episode[]) => {
      const c: Record<string, number> = {};
      for (const e of list) c[e.status] = (c[e.status] || 0) + 1;
      return c;
    };
    const m = new Map<string, Episode[]>();
    for (const e of kindEps) {
      if (status && e.status !== status) continue;
      // Keyed by the Sonarr id, not the title: two shows can share a title (remakes, and the
      // JP/EN names of one anime), and a title-keyed accordion silently merged them into one row.
      const k = String(e.series_id ?? e.series_title);
      const a = m.get(k); if (a) a.push(e); else m.set(k, [e]);
    }
    return [...m.entries()].map(([key, list]) => {
      const sm = new Map<number, Episode[]>();
      for (const e of list) { const a = sm.get(e.season); if (a) a.push(e); else sm.set(e.season, [e]); }
      const seasons = [...sm.entries()].sort((a, b) => a[0] - b[0]).map(([season, seps]) => ({
        season, eps: seps.slice().sort((x, y) => x.episode - y.episode), byStatus: tally(seps),
      }));
      return {
        key, sid: list[0].series_id, title: list[0].series_title, poster: list[0].poster,
        eps: list, byStatus: tally(list), seasons,
        prio: list.some(e => (e.priority ?? 0) > 0),
      };
    }).sort((a, b) => a.title.localeCompare(b.title));
  }, [kindEps, status]);

  const shown = useMemo(() => {
    const s = urlQ.trim().toLowerCase();
    return s ? shows.filter(sh => sh.title.toLowerCase().includes(s)) : shows;
  }, [shows, urlQ]);
  const shownEps = useMemo(() => shown.reduce((n, sh) => n + sh.eps.length, 0), [shown]);
  const allOpen = shown.length > 0 && shown.every(sh => open.has(sh.key));
  const picked = useMemo(
    () => shown.flatMap(sh => sh.eps).filter(e => sel.has(e.id)), [shown, sel]);

  const pickStatus = (s: string) => setParam("status", s || null);
  const toggleSel = (id: string) =>
    setSel(o => { const n = new Set(o); n.has(id) ? n.delete(id) : n.add(id); return n; });
  const setSeasonSel = (se: Season, on: boolean) =>
    setSel(o => {
      const n = new Set(o);
      for (const e of se.eps) on ? n.add(e.id) : n.delete(e.id);
      return n;
    });

  async function bulk(verb: string, fn: (id: string) => Promise<unknown>) {
    const ids = picked.map(e => e.id);
    // Sequential on purpose: these are cheap DB writes, and firing fifty at once at a container
    // that is also probing files buys nothing.
    await runAction(async () => { for (const id of ids) await fn(id); });
    setSel(new Set());
    setMsg(`${verb} ${ids.length} episode(s)`);
    await runAction(refresh);
  }

  async function searchNow() {
    setMsg("starting…");
    try {
      const r = await api.searchAll();
      setMsg(r.started ? `searching ${r.pending ?? 0} pending · ${r.slots ?? "?"} free slot(s)`
                       : (r.note || "already running"));
    } catch (e) { setMsg("failed: " + (e as Error).message); }
    await runAction(refresh);
  }

  async function scanNow() {
    try {
      const r = await api.tvScan();
      setMsg(r.started ? "scan started — reading the files…" : (r.note || "a scan is already running"));
    } catch (e) { setMsg("failed: " + (e as Error).message); }
    await runAction(refresh);
  }

  /* Re-read ONE show's files and act on the result. The unit an operator works in: after
     replacing a whole show's files by hand (a fresh MULTI rip), a library-wide re-read is minutes
     over tens of thousands of files, and the hourly sweep may not reach this show for ages. */
  async function rescanShow(sh: Show) {
    if (!sh.sid) { setMsg("no Sonarr id on these records"); return; }
    try {
      const r = await api.rescanSeries(sh.sid);
      setMsg(r.started ? `re-reading ${sh.title} and searching what's still missing…`
                       : (r.note || "a scan is already running"));
    } catch (e) { setMsg((e as Error).message || "re-read failed"); }
    await runAction(refresh);
  }

  /* Start the whole show over. Deletes DOWNLOADS, never library files — the confirm says so,
     because "delete everything" is the one instruction that must not be ambiguous. */
  async function resetShow(sh: Show) {
    if (!sh.sid) { setMsg("no Sonarr id on these records"); return; }
    if (!window.confirm(`Start "${sh.title}" (${sh.eps.length} episodes) from scratch?\n\n`
      + `• stops any merge in progress\n• DELETES the downloads it grabbed\n`
      + `• clears the blocklist, attempts, candidates, sync data and AI verdicts\n`
      + `• re-reads every file afterwards\n\n`
      + `Your library files are NOT deleted. Tracks already merged into them stay merged — `
      + `the re-read is what records what each file actually contains now.`)) return;
    try {
      const r = await api.resetSeries(sh.sid);
      setMsg(`${sh.title}: ${r.episodes} episode(s) reset, ${r.donors_deleted} download(s) `
        + `deleted, ${r.merges_stopped} merge(s) stopped — re-reading the files…`);
    } catch (e) { setMsg((e as Error).message || "reset failed"); }
    await runAction(refresh);
  }

  // ---------------------------------------------------------------- episode row
  function epRow(e: Episode) {
    const settled = e.status === "ignored" || e.status === "merged";
    return (
      <tr key={e.id} className={sel.has(e.id) ? "sel" : undefined}>
        <td className="pick">
          <input type="checkbox" checked={sel.has(e.id)} onChange={() => toggleSel(e.id)}
            aria-label={`select ${e.series_title} ${fmtSE(e.season, e.episode)}`} />
        </td>
        <td style={{ width: 104 }}>
          <b>{fmtSE(e.season, e.episode)}</b>
          {(e.priority ?? 0) > 0 && <span className="prio" title="prioritised"> ★</span>}
          {/* An anime library filed with absolute numbering shows a different number from the one
              releases use. Hiding that makes every numbering mismatch invisible. */}
          {e.aired && <div className="sub" title="the numbering releases use for this episode">
            released as {e.aired}</div>}
        </td>
        <td style={{ minWidth: 170 }}>
          <div className="addrow">
            <Pill s={e.status} />
            <AiPill s={e.ai_status} />
            {e.status === "sync_fail" && e.sync_delta != null && <span className="sub">Δ {e.sync_delta}s</span>}
            <DriftBadge d={e.sync_drift} />
          </div>
          {e.status === "downloading" && <DownloadBar dl={dlOf(e)} />}
          {e.status === "ready" && <QueuedLine />}
          {/* Rendered for ANY status, not just merging: a stuck record's progress line is the only
              thing that says what it is stuck ON, and hiding it kept one silent for a day. */}
          {e.progress && <div className="sub"
            style={{ color: e.status === "merging" ? "#5ee9a0" : "#ffcf8f" }}>{e.progress}</div>}
          <Tracks a={e.audio_langs} s={e.sub_langs} na={e.need_audio} ns={e.need_subs} />
          {e.error && <div className="sub bad clamp2" title={e.error}>{e.error}</div>}
          {e.ai_verdict && <div className="sub clamp2" title={e.ai_verdict}>🤖 {e.ai_verdict}</div>}
        </td>
        <td>{e.candidate_title
          ? <>{e.candidate_title}
              <div className="sub">score {e.candidate_score} · {e.candidate_seeders}s</div></>
          : <span className="muted">—</span>}</td>
        <td className="muted" style={{ width: 90 }}>{e.quality || "—"}</td>
        <td>
          <div className="row">
            {!settled && <button className="btn sec small" onClick={() => setRelEp(e)}
              title="Search the indexers for this one episode and pick a release yourself">
              Search…</button>}
            {(e.status === "merging" || e.status === "ready") &&
              <Act cls="btn sec small" run={() => act(() => api.abortEpisode(e.id))}
                title="Kill the decode/mux running now, or take it off the merge queue">⛔ Abort</Act>}
            {["pending", "no_release", "error", "sync_fail"].includes(e.status) &&
              <Act cls="btn sec small" run={() => act(() => api.epRetry(e.id))}
                title="Blocklist the release that failed, drop its donor and search again">Retry</Act>}
            {!settled && <Act cls="btn sec small"
              title={(e.priority ?? 0) > 0
                ? "Back to normal order"
                : "Jump the search sweep and the merge queue — for something someone is waiting on"}
              run={() => act(() => api.epPriority(e.id, (e.priority ?? 0) > 0 ? 0 : 1))}>
              {(e.priority ?? 0) > 0 ? "★ Un-prioritise" : "★"}</Act>}
            {e.status !== "ignored" && <button className="btn sec small" onClick={() => setLips(e)}
              title={"Read the lips: measure this file's audio against the picture itself — the "
                + "one reading here that doesn't compare two files that could both be wrong"}>
              👄</button>}
            {!settled && <Act cls="btn sec small" run={() => act(() => api.epIgnore(e.id))}
              title={"Stop working on this episode. A rescan won't re-open it; it is revisited on "
                + "the ignored-revisit schedule."}>Ignore</Act>}
          </div>
        </td>
      </tr>
    );
  }

  // ---------------------------------------------------------------- one show, expanded
  function detail(sh: Show) {
    return (
      <div className="showcard-detail">
        <div className="row tight" style={{ margin: "8px 0 2px" }}>
          <Act cls="btn sec small"
            title={sh.prio
              ? "Back to normal order for every episode of this show"
              : "Put every episode of this show at the front of the search sweep AND the merge "
                + "queue — TV is requested per series, and a film downloaded promptly achieves "
                + "nothing if it then queues behind thirty season-pack episodes"}
            run={() => act(() => api.seriesPriority(sh.sid, sh.prio ? 0 : 1))}>
            {sh.prio ? "★ Un-prioritise the show" : "★ Prioritise the show"}</Act>
          <Act cls="btn sec small" run={() => rescanShow(sh)}
            title={"Re-read every file of this show (bypassing the probe cache) and search for "
              + "whatever it still lacks"}>↻ Re-read this show</Act>
          {/* The per-season search composes "Title Sxx", which an indexer never answers with a
              complete-series batch — so the one release that can fill a 150-episode gap in a
              single grab had no way of being found. */}
          {!!sh.sid && <button className="btn sec small" onClick={() => setRelSeries(sh)}
            title={"Find a COMPLETE-series release (a batch covering every season) and claim "
              + "every episode with it"}>⧉ Complete series…</button>}
          <div className="spacer" />
          <Act cls="btn sec small danger" run={() => resetShow(sh)}
            title={"Start this show over: stop merges, delete its downloads, clear all pipeline "
              + "state, re-read the files"}>↺ Reset the show</Act>
        </div>

        {sh.seasons.map(se => {
          const allSel = se.eps.every(e => sel.has(e.id));
          return (
            <div key={se.season}>
              <div className="row tight" style={{ marginTop: 10 }}>
                {/* .seasonhead is uppercase, so it wraps only the label — the state pills below
                    would be shouted at you otherwise. */}
                <span className="seasonhead"><b>Season {se.season}</b></span>
                <span className="muted">· {se.eps.length} episode{se.eps.length === 1 ? "" : "s"}</span>
                <StatePills counts={se.byStatus} onPick={pickStatus} />
                <div className="spacer" />
                <button className="btn sec small"
                  title="Search for a season pack and pick one yourself"
                  onClick={() => setRelSeason({ sh, season: se.season })}>Search pack…</button>
              </div>
              <table>
                <thead>
                  <tr>
                    <th className="pick">
                      <input type="checkbox" checked={allSel}
                        onChange={ev => setSeasonSel(se, ev.target.checked)}
                        aria-label={`select every episode of season ${se.season}`} />
                    </th>
                    <th>Episode</th><th>State</th><th>Candidate</th><th>Quality</th>
                    <th style={{ textAlign: "right" }}>Actions</th>
                  </tr>
                </thead>
                <tbody>{se.eps.map(epRow)}</tbody>
              </table>
            </div>
          );
        })}
      </div>
    );
  }

  /** One show. Grid and list are the same card in two containers — a poster tile that expands
   *  across the whole grid row, or a full-width row — so the head can never drift between them. */
  function showCard(sh: Show) {
    const isOpen = open.has(sh.key);
    return (
      <div className={"showcard" + (isOpen ? " open" : "")} key={sh.key}>
        <div className="showcard-head" onClick={() => toggle(sh.key)}
          role="button" tabIndex={0} aria-expanded={isOpen}
          onKeyDown={ev => {
            if (ev.key !== "Enter" && ev.key !== " ") return;
            ev.preventDefault();        // Space on a role=button must open the show, not scroll
            toggle(sh.key);
          }}>
          <Poster src={sh.poster} alt={sh.title} sm={view === "list"} />
          <div className="showcard-meta">
            <div className="showcard-title">
              <span className="muted">{isOpen ? "▾ " : "▸ "}</span>
              {sh.prio && <span className="prio" title="prioritised">★</span>}
              {sh.title}
            </div>
            <div className="sub">
              {sh.eps.length} episode{sh.eps.length === 1 ? "" : "s"} tracked
              {" · "}{sh.seasons.length} season{sh.seasons.length === 1 ? "" : "s"}
            </div>
            <StatePills counts={sh.byStatus} onPick={pickStatus} />
          </div>
        </div>
        {isOpen && detail(sh)}
      </div>
    );
  }

  const modalTitle = (e: Episode) => `${e.series_title} ${fmtSE(e.season, e.episode)}`;

  return (
    <>
      <div className="panel">
        <div className="row">
          <div>
            <b>{anime ? "🎌 Anime" : "📺 TV Shows"}</b>{" "}
            <span className="muted">
              episodes whose file is short of the {anime ? "anime" : "series"} language profile
            </span>
          </div>
          <div className="spacer" />
          {msg && <span className="muted">{msg}</span>}
          <Act cls="btn sec" run={searchNow}
            title="Search every pending episode now instead of waiting for the timer">
            🔍 Search now</Act>
          <Act cls="btn" run={scanNow}
            title="Ask Sonarr what it has, read those files and record what each is missing">
            Scan {anime ? "anime" : "series"}</Act>
          <RescanButton scope={scope} label={label} />
          <RescanButton scope={scope} label={label} full />
          <RecheckButton scope={scope} />
        </div>
        <div className="chips" style={{ marginTop: 12 }}>
          {TV_STATES.map(s => (
            <button key={s} className="chip"
              style={{
                font: "inherit", cursor: "pointer",
                borderColor: status === s ? "var(--accent)" : undefined,
                color: status === s ? "var(--text)" : undefined,
              }}
              aria-pressed={status === s}
              title={status === s ? "showing only these — click to clear"
                                  : `show only the ${stateLabel(s)} episodes`}
              onClick={() => pickStatus(status === s ? "" : s)}>
              {stateLabel(s)} <b>{fmtNum(counts[s] ?? 0)}</b>
            </button>
          ))}
        </div>
      </div>

      {picked.length > 0 && (
        <div className="bulkbar">
          <b>{picked.length}</b> episode{picked.length === 1 ? "" : "s"} selected
          <div className="spacer" />
          <Act cls="btn sec small" run={() => bulk("retried", api.epRetry)}
            title="Blocklist each failed release, drop its donor and search again">↻ Retry</Act>
          <Act cls="btn sec small" run={() => bulk("prioritised", id => api.epPriority(id, 1))}
            title="Jump the search sweep and the merge queue">★ Prioritise</Act>
          <Act cls="btn sec small"
            title="Stop working on these. A rescan won't re-open them."
            run={async () => {
              if (!window.confirm(`Stop working on ${picked.length} episode(s)?\n\n`
                + `They are marked ignored: no more searches, and a rescan won't re-open them. `
                + `The pipeline revisits ignored records on its own schedule.`)) return;
              await bulk("ignored", api.epIgnore);
            }}>Ignore</Act>
          <button className="btn ghost small" onClick={() => setSel(new Set())}>Clear</button>
        </div>
      )}

      <div className="panel capped full">
        <div className="row toolbar panel-head">
          <select value={status} onChange={e => pickStatus(e.target.value)}
            aria-label="Filter by pipeline state">
            <option value="">all states</option>
            {TV_STATES.map(s => <option key={s} value={s}>{stateLabel(s)}</option>)}
          </select>
          <input className="search" type="search" value={text}
            placeholder={anime ? "Search anime…" : "Search show…"}
            onChange={e => setText(e.target.value)}
            aria-label={anime ? "Search anime by title" : "Search shows by title"} />
          <span className="muted">
            {fmtNum(shown.length)} show{shown.length === 1 ? "" : "s"} · {fmtNum(shownEps)} episode
            {shownEps === 1 ? "" : "s"}
          </span>
          <LiveDot />
          <div className="spacer" />
          {(counts.error ?? 0) > 0 &&
            <Act cls="btn sec" run={() => act(api.tvRetryErrors)}
              title={"Blocklist the failed release, drop its donor and re-search — every failed "
                + "episode, not just this show's"}>
              ↻ Retry {counts.error} errors</Act>}
          <button className="btn sec" disabled={shown.length === 0}
            onClick={() => setOpen(allOpen ? new Set() : new Set(shown.map(s => s.key)))}>
            {allOpen ? "Collapse all" : "Expand all"}</button>
          <div className="segbtns">
            <button className={view === "grid" ? "active" : ""} onClick={() => setView("grid")}
              title="Poster cards">▦ Grid</button>
            <button className={view === "list" ? "active" : ""} onClick={() => setView("list")}
              title="One row per show">☰ List</button>
          </div>
        </div>

        <div className="panel-body">
          {shown.length === 0
            ? <Empty>
                {kindEps.length === 0
                  ? <>Nothing tracked here yet. A record is only created for an episode whose file
                      is <b>missing</b> something, so an empty list can also mean the scanner has
                      not read these files — run <b>Scan new {label}</b> above.</>
                  : <>No {anime ? "anime" : "show"} matches those filters.{" "}
                      <button className="btn sec small" style={{ marginLeft: 6 }}
                        onClick={() => { setParam("status", null); setText(""); setParam("q", null); }}>
                        Clear filters</button></>}
              </Empty>
            : view === "grid"
              ? <div className="showcardgrid fixed">{shown.map(showCard)}</div>
              : <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
                  {shown.map(showCard)}
                </div>}
        </div>
      </div>

      {relSeries && <ReleaseModal title={`${relSeries.title} — complete series`}
        load={() => api.seriesCandidates(relSeries.sid)}
        onGrab={c => api.seriesGrab(relSeries.sid, c.link, c.rid, c.title)}
        onClose={() => setRelSeries(null)} onGrabbed={() => void runAction(refresh)} />}
      {relSeason && <ReleaseModal
        title={`${relSeason.sh.title} — season ${relSeason.season}`}
        load={() => api.seasonCandidates(relSeason.sh.sid, relSeason.season)}
        onGrab={c => api.seasonGrab(relSeason.sh.sid, relSeason.season, c.link, c.rid, c.title)}
        onClose={() => setRelSeason(null)} onGrabbed={() => void runAction(refresh)} />}
      {relEp && <ReleaseModal title={modalTitle(relEp)}
        load={() => api.episodeCandidates(relEp.id)}
        onGrab={c => api.episodeGrab(relEp.id, c.link, c.rid, c.title)}
        onClose={() => setRelEp(null)} onGrabbed={() => void runAction(refresh)} />}
      {lips && <LipSyncModal kind="episode" id={lips.id} title={modalTitle(lips)}
        onClose={() => setLips(null)} onApplied={() => void runAction(refresh)} />}
    </>
  );
}
