import { useEffect, useMemo, useState } from "react";
import {
  api, type DL, type Movie, type RecheckState, type RescanState, type Status,
} from "../api";
import { runAction, usePoll, useStored } from "../lib/poll";
import { setParam, useRoute } from "../lib/router";
import { fmtAgo, fmtNum } from "../lib/format";
import {
  Act, AiPill, DownloadBar, DriftBadge, Empty, LiveDot, Pill, Poster, Progress, QueuedLine,
  RowMenu, STATE_LABEL, STATES, Tracks,
} from "../components/ui";
import { LipSyncModal, ReleaseModal, SyncEditor } from "../components/modals";

/* ======================================================================
   Films — every movie the scanner has recorded a gap for, and the whole
   per-title action surface.

   Two things the old tab could not do and this one must: the filter and
   the search live in the URL (a failing title is a link you can send),
   and the list view sorts. Everything else here is a port — the action
   matrix in particular is a regression list, not a menu design.
   ====================================================================== */

/* ---------------------------------------------------------------- library re-reads
   Two ways to read the library, and the difference only shows after an interruption:

     progressive — keeps the probe cache, so mkvmerge runs ONLY for files with no valid probe:
                   never read, changed on disk, or never reached because a previous pass was cut
                   short. Resumable by construction — each file is committed as it is read.
     full        — drops the cache for that scope first and reads everything again. What you want
                   when you don't trust the cached answer, not when you're filling gaps.

   Both walk the whole scope and both prune what has vanished. The progress line is the only way
   to tell a running scan from a stuck one, so it reports read-vs-reused rather than a spinner. */
function RescanButton({ full }: { full?: boolean }) {
  const [st, setSt] = useState<RescanState | null>(null);
  usePoll(() => api.rescanState().then(setSt).catch(() => {}), st?.running ? 3000 : 30000,
    [st?.running]);

  // Both endpoints answer 200 with {started:false, note} when SCAN_LOCK is already held — and
  // the button's own `disabled={running}` cannot cover that, because the hourly search job takes
  // the lock for its whole scan phase WITHOUT touching SCAN_STATE, so `running` is false the
  // entire time. Discarding the body meant the click did nothing and looked like it had worked,
  // with the previous pass's "last: …" line still on screen. runAction because this was the one
  // action here not wrapped in it, so a 401 after a key rotation reached nobody.
  const [note, setNote] = useState("");
  const go = () => runAction(async () => {
    const r = await api.rescan("films", !!full);
    setNote(r.started ? "" : (r.note || "a scan is already running"));
    await api.rescanState().then(setSt).catch(() => {});
  });

  // One scan runs at a time (SCAN_LOCK), so a pass started from another tab disables this one.
  const mine = st?.scope === "films";
  const running = !!st?.running;
  // ...and the two buttons for films share that one state object, so each must only report on
  // the mode that actually ran — otherwise a re-read claims the progressive pass's numbers.
  const done = mine && st && !running && st.finished > 0 && !!st.full === !!full;
  const dropped = (st?.pruned ?? 0) + (st?.pruned_records ?? 0);
  // why *arr-known files never became inventory rows, so a short file count is explainable
  const skips = Object.entries(st?.skips?.films ?? {}).filter(([, n]) => n > 0);

  return (
    <>
      <Act cls="btn sec" disabled={running} busyLabel="starting…" run={go}
        title={full
          ? "Read every film file again with mkvmerge, even ones that look unchanged, and "
            + "re-decide what each is missing. Use when you don't trust the cached answer. "
            + "Takes a few minutes on a big library."
          : "Read only the film files that have no result yet — new imports, files changed on "
            + "disk, and anything an interrupted scan never reached. Picks up where the last "
            + "pass stopped, so it is cheap to run any time."}>
        {running && mine ? (full ? "Re-reading…" : "Scanning…") : (full ? "Re-read films" : "Scan new films")}
      </Act>
      {note && <span className="warn">{note}</span>}
      {running && mine && <span className="muted">
        {st!.phase}…{(st!.read ?? 0) > 0 && <> · read {fmtNum(st!.read)}</>}
        {(st!.reused ?? 0) > 0 && <> · reused {fmtNum(st!.reused)}</>}</span>}
      {running && !mine && <span className="muted">busy: {st!.scope} scan running</span>}
      {done && !st.error && <span className="muted">
        last: read {fmtNum(st.read ?? 0)} file(s)
        {(st.reused ?? 0) > 0 && <> · {fmtNum(st.reused)} already cached</>}
        {" "}· {fmtNum(st.films ?? 0)} gap(s)
        {dropped > 0 && <> · {dropped} deleted entr{dropped === 1 ? "y" : "ies"} removed</>}
        {skips.length > 0 && <span title="files Radarr knows about that did not become inventory rows">
          {" "}· skipped {skips.map(([k, n]) => `${n} ${k}`).join(" · ")}</span>}
        {st.probes.unreadable > 0 && <span className="bad"> · {st.probes.unreadable} unreadable</span>}
      </span>}
      {mine && st?.error && <span className="bad">rescan failed: {st.error}</span>}
    </>
  );
}

/** "Merged" only means a merge ran and "no release" only means nothing existed when we last
 *  looked — neither is a promise that the file meets its target. This re-probes the settled
 *  records and re-opens the ones still short. */
function RecheckButton() {
  const [st, setSt] = useState<RecheckState | null>(null);
  usePoll(() => api.recheckState().then(setSt).catch(() => {}), st?.running ? 3000 : 60000,
    [st?.running]);

  const [note, setNote] = useState("");
  const go = () => runAction(async () => {
    const r = await api.recheck("films");     // 200 + started:false while SCAN_LOCK is held
    setNote(r.started ? "" : (r.note || "a scan or re-check is already running"));
    await api.recheckState().then(setSt).catch(() => {});
  });
  const mine = st?.scope === "films";
  const running = !!st?.running;
  const done = mine && st && !running && st.finished > 0;

  return (
    <>
      <Act cls="btn sec" disabled={running} busyLabel="starting…" run={go}
        title={"Re-read every film parked in a finished state — merged, or no-release — and "
             + "re-open the ones that still don't meet their target. The release already tried "
             + "stays blocklisted, so a re-opened title searches for a different one; ignored "
             + "titles are left alone."}>
        {running && mine ? "Re-checking…" : "Re-check finished"}
      </Act>
      {note && <span className="warn">{note}</span>}
      {running && mine && <span className="muted">
        re-probed {fmtNum(st!.checked)} of {fmtNum(st!.total)} · re-opened {fmtNum(st!.reopened)}</span>}
      {done && !st.error && <span className="muted">
        last: {fmtNum(st.reopened)} re-opened of {fmtNum(st.total)} · {fmtNum(st.complete)} genuinely complete
        {st.gone > 0 && <> · {st.gone} file(s) gone</>}
        {st.unreadable > 0 && <span className="bad"> · {st.unreadable} unreadable</span>}</span>}
      {mine && st?.error && <span className="bad">re-check failed: {st.error}</span>}
    </>
  );
}

/* ---------------------------------------------------------------- the row menu
   Status-conditional, and the matrix is the regression list: each entry exists because it is the
   only way to perform that repair from the UI. */
type ModalWhat = "release" | "tune" | "lips";

function MovieActions({ m, act, onOpen, onNote }:
  { m: Movie; act: (fn: () => Promise<unknown>) => Promise<void>;
    onOpen: (what: ModalWhat, m: Movie) => void; onNote: (s: string) => void }) {
  const s = m.status;
  const B = (label: string, run: () => Promise<unknown>, title?: string) =>
    <Act cls="btn sec" title={title} run={run}>{label}</Act>;

  return (
    <RowMenu>
      {["pending", "no_release", "error"].includes(s) &&
        B("Search now", () => act(() => api.search(m.tmdb_id)),
          "Search the indexers for this title now instead of waiting for the sweep")}
      {["no_release", "error", "sync_fail", "downloading", "merged"].includes(s) &&
        B("Search again", () => act(() => api.research(m.tmdb_id)),
          "Re-open the record and search again — indexers gain releases over time")}
      {["pending", "no_release", "error", "review", "sync_fail", "grabbed", "downloading"].includes(s) &&
        <button className="btn sec" onClick={() => onOpen("release", m)}
          title="List what the indexers actually have and grab one by hand">Search…</button>}
      {["grabbed", "downloading", "no_release", "error", "sync_fail"].includes(s) &&
        B("Pick another", () => act(() => api.another(m.tmdb_id)),
          "Blocklist the release in hand and fetch a different one")}

      {/* The donor is already on disk in these states, so this is a queue push, not a download. */}
      {(!!m.en_file || ["ready", "review", "sync_fail"].includes(s)) &&
        B("Merge now", () => act(async () => {
          const r = await api.merge(m.tmdb_id);
          // The endpoint queues rather than merging inline, and `note` is where it says nothing
          // will drain that queue (pipeline disabled, or paused) — silence would read as success.
          onNote(r.queued ? `${m.title}: queued for merge${r.note ? ` — ${r.note}` : ""}`
                          : (r.note || `${m.title}: not queued`));
        }), "Put this back on the merge queue with the donor already downloaded")}
      {["merged", "sync_fail"].includes(s) &&
        B("Re-run sync detection", () => act(() => api.sync(m.tmdb_id, 0)),
          "Measure the offset again from scratch and merge with what it finds")}
      {s === "merged" &&
        <button className="btn sec" onClick={() => onOpen("tune", m)}
          title="Hear the offset while you change it, then apply it to the file">Tune sync</button>}
      {["ready", "merged", "review", "sync_fail", "error"].includes(s) &&
        <button className="btn sec" onClick={() => onOpen("lips", m)}
          title={"Correlate mouth movement in the picture against the audio — the only absolute "
               + "reading here, and the one that still works when the pair has been declared a "
               + "different cut"}>
          Read the lips</button>}

      {/* A merge is a sync detect plus a multi-GB remux; without this the only ways to stop one
          going wrong were to wait out mux_timeout_min (4h) or restart the container. */}
      {["merging", "ready"].includes(s) &&
        B("⛔ Abort merge", () => act(() => api.abortMovie(m.tmdb_id)),
          "Kill the decode or mux running right now, or take this off the queue")}
      {["error", "sync_fail", "review"].includes(s) &&
        B("↻ Retry", () => act(() => api.retry(m.tmdb_id)),
          "Blocklist the release that failed, drop its donor and go back to pending")}

      {/* Someone asked for this one: jump the search sweep AND the merge queue. Both are ordered
          by recency, so a title requested today otherwise sits behind the whole backlog. */}
      {!["merged", "ignored"].includes(s) && (m.priority
        ? B("★ Un-prioritise", () => act(() => api.priority(m.tmdb_id, 0)))
        : B("★ Prioritise", () => act(() => api.priority(m.tmdb_id, 1)),
            "Move ahead of the backlog in the search sweep and the merge queue"))}
      {s === "ignored" && B("Un-ignore", () => act(() => api.unignore(m.tmdb_id)))}
      {s !== "ignored" && s !== "merged" &&
        B("Ignore", () => act(() => api.ignore(m.tmdb_id)),
          "Stop searching for this one. A scan that later finds the file complete still closes it out.")}

      {/* The file on disk changed (replaced by hand, remuxed, subtitles dropped beside it):
          re-read it and act on whatever it is now missing, without waiting for a sweep. */}
      {B("↻ Re-read this film", () => act(() => api.rescanMovie(m.tmdb_id)),
        "Probe the file again with the cache bypassed and re-decide what it is missing")}
      <button className="btn sec" onClick={() => {
        if (confirm(`Start "${m.title}" from scratch?\n\n`
          + `Stops any merge, DELETES its download, and clears the blocklist, attempts, `
          + `candidates and sync data.\n\n`
          + `Your library file is NOT deleted — but tracks already merged into it stay merged, `
          + `so it is re-read afterwards to see what it actually contains now.`))
          void act(() => api.resetMovie(m.tmdb_id));
      }}>↺ Start over</button>
    </RowMenu>
  );
}

/** The card's scrolling half: the languages, the donor, the failure. The table spreads the same
 *  fields across its own columns, so this is the card's shape only. */
function CardDetail({ m, dl }: { m: Movie; dl?: DL }) {
  return (
    <>
      {m.status === "downloading" && <DownloadBar dl={dl} />}
      {m.status === "ready" && <QueuedLine />}
      {/* Rendering `progress` only while merging is what kept a stuck record silent for 24 h:
          a stall shows up as a progress line that stops moving, and only if it is shown at all. */}
      {m.progress && <div className="sub"
        style={{ color: m.status === "merging" ? "#5ee9a0" : "#ffcf8f" }}>{m.progress}</div>}
      <Tracks a={m.audio_langs} s={m.sub_langs} na={m.need_audio} ns={m.need_subs} />
      {m.candidate_title && <div className="sub" title={m.candidate_title}>
        🎯 {m.candidate_title}
        {m.candidate_score != null && <span className="muted">
          {" "}· score {m.candidate_score}{m.candidate_seeders != null && ` · ${m.candidate_seeders}s`}
        </span>}
      </div>}
      <DriftBadge d={m.sync_drift} />
      {m.ai_verdict && <div className="sub clamp2" title={m.ai_verdict}>🤖 {m.ai_verdict}</div>}
      {m.error && <div className="sub bad clamp2" title={m.error}>{m.error}</div>}
    </>
  );
}

function Star({ m }: { m: Movie }) {
  if (!m.priority) return null;
  return <span className="prio"
    title="Prioritised — ahead of the backlog in both the search sweep and the merge queue">★</span>;
}

function MovieCard({ m, dl, act, onOpen, onNote }:
  { m: Movie; dl?: DL; act: (fn: () => Promise<unknown>) => Promise<void>;
    onOpen: (what: ModalWhat, m: Movie) => void; onNote: (s: string) => void }) {
  return (
    <div className="card">
      <Poster src={m.poster} alt={m.title} />
      <div className="card-body">
        <div className="card-title" title={`${m.title} (${m.year})`}>
          <Star m={m} />{m.title} <span className="muted">({m.year})</span>
        </div>
        {/* The menu lives in the PINNED row, never in the scroller: a dropdown inside an
            overflow container opens into nothing. */}
        <div className="card-row">
          <Pill s={m.status} />
          {m.status === "sync_fail" && m.sync_delta != null &&
            <span className="sub">Δ {m.sync_delta.toFixed(1)}s</span>}
          <div className="spacer" />
          <MovieActions m={m} act={act} onOpen={onOpen} onNote={onNote} />
        </div>
        {/* Same anchor the show cards carry, and the same idea: how much of what this title
            needs is done. For a film the unit is its language targets rather than its episodes —
            `targets`/`targets_met` come from the endpoint, resolved with the profile the scan
            itself used, because nothing in the record says how many were WANTED. Pinned above
            the scroller so a wall of text below can never push it out of view. */}
        <TargetProgress m={m} />
        <div className="card-scroll">
          <div className="sub">
            → {m.original_title} · {m.original_lang}{m.quality ? ` · ${m.quality}` : ""}
          </div>
          {m.ai_status && <div style={{ margin: "3px 0" }}><AiPill s={m.ai_status} /></div>}
          <CardDetail m={m} dl={dl} />
        </div>
      </div>
    </div>
  );
}

/** How much of this film's language target its file now meets. The two fields are optional
 *  because they are computed by the endpoint rather than stored, so an older cached response
 *  simply renders no bar rather than a wrong one. */
function TargetProgress({ m }: { m: Movie }) {
  const total = m.targets ?? 0;
  if (total <= 0) return null;
  const done = m.targets_met ?? 0;
  return (
    <Progress done={done} total={total}
      title={`${done} of ${total} target language(s) present in the file`}>
      <b>{done}</b> of {total} target{total === 1 ? "" : "s"} met
      {done >= total && " · complete"}
    </Progress>
  );
}

/* ---------------------------------------------------------------- sorting
   The default is NO client sort: the server orders by priority first, then recency, so starring
   a title visibly moves it. A sort would silently undo that, which is why the third click on a
   column returns to the server's order rather than cycling back to ascending. */
type SortKey = "" | "title" | "status" | "updated";
const STATE_ORDER = new Map(STATES.map((s, i) => [s, i]));
const natural = (k: SortKey): 1 | -1 => (k === "updated" ? -1 : 1);

function SortTh({ label, k, sort, onSort, width }:
  { label: string; k: SortKey; sort: { k: SortKey; dir: 1 | -1 };
    onSort: (k: SortKey) => void; width?: number }) {
  const on = sort.k === k;
  return (
    <th className="sortable" style={width ? { width } : undefined} tabIndex={0} role="columnheader"
      aria-sort={on ? (sort.dir === 1 ? "ascending" : "descending") : "none"}
      onClick={() => onSort(k)}
      onKeyDown={e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onSort(k); } }}
      title="Sort by this column. A third click restores the default order — prioritised titles first, then most recently changed.">
      {label}<span className="arrow">{on ? (sort.dir === 1 ? "▲" : "▼") : "⇅"}</span>
    </th>
  );
}

export default function Films() {
  const route = useRoute();
  // The URL is the source of truth for both filters, so a filtered list is a link and Back works.
  const urlStatus = route.query.get("status") ?? "";
  const filter = STATES.includes(urlStatus) ? urlStatus : "";
  const urlQ = route.query.get("q") ?? "";

  const [st, setSt] = useState<Status | null>(null);
  const [movies, setMovies] = useState<Movie[]>([]);
  const [dls, setDls] = useState<Record<string, DL>>({});
  const [text, setText] = useState(urlQ);
  // One line for whatever the last action reported back — a note from a row action ("queued for
  // merge — the pipeline is paused") belongs in the toolbar, not repeated inside every row.
  const [note, setNote] = useState("");
  const [sort, setSort] = useState<{ k: SortKey; dir: 1 | -1 }>({ k: "", dir: 1 });
  const [modal, setModal] = useState<{ what: ModalWhat; m: Movie } | null>(null);
  // The old UI wrote this key as a BARE string, not JSON, so useStored's parse throws and falls
  // back to the initial — read the legacy value as that initial and a saved preference survives.
  const [view, setView] = useStored<"grid" | "list">("vo_view", legacyView());

  const load = async () => {
    const [s, m] = await Promise.all([api.status(), api.movies(filter || undefined)]);
    setSt(s); setMovies(m);
  };
  usePoll(load, 8000, [filter]);
  // Download progress moves far faster than the record list, and costs one small qB call.
  usePoll(() => api.downloads().then(d => setDls(d.items || {})), 4000);

  // Follow the URL when it changes underneath — a Back press, or someone else's shared link.
  useEffect(() => { setText(urlQ); }, [urlQ]);
  useEffect(() => {
    if (text === urlQ) return;
    const t = setTimeout(() => setParam("q", text || null), 250);
    return () => clearTimeout(t);
  }, [text, urlQ]);

  const shown = useMemo(() => {
    const q = urlQ.trim().toLowerCase();
    const list = q
      ? movies.filter(m => (m.title || "").toLowerCase().includes(q)
                        || (m.original_title || "").toLowerCase().includes(q))
      : movies;
    if (!sort.k) return list;
    const cmp = (a: Movie, b: Movie) => {
      switch (sort.k) {
        case "title": return (a.title || "").localeCompare(b.title || "");
        // Pipeline order, not alphabetical: "pending → … → ignored" is the sequence a record
        // actually walks, and grouping by it is the reason to sort on status at all.
        case "status": return (STATE_ORDER.get(a.status) ?? 99) - (STATE_ORDER.get(b.status) ?? 99);
        default: return (a.updated || 0) - (b.updated || 0);
      }
    };
    return [...list].sort((a, b) => cmp(a, b) * sort.dir);
  }, [movies, urlQ, sort]);

  const onSort = (k: SortKey) => setSort(s =>
    s.k !== k ? { k, dir: natural(k) }
      : s.dir === natural(k) ? { k, dir: (natural(k) === 1 ? -1 : 1) as 1 | -1 }
        : { k: "", dir: 1 });

  const dlOf = (m: Movie) => dls[(m.dl_hash || "").toLowerCase()];

  // try/finally with no catch meant a failed grab, ignore, retry or merge click told the user
  // nothing at all and logged an unhandled rejection. runAction surfaces it in the banner.
  const act = async (fn: () => Promise<unknown>) => {
    await runAction(fn);
    await runAction(load);
  };
  const searchAll = () => act(async () => {
    const r = await api.searchAll();
    setNote(r.started
      ? `searching ${fmtNum(r.pending ?? 0)} pending · ${r.slots ?? "?"} free slot(s)`
      : (r.note || "a search sweep is already running"));
  });
  const openModal = (what: ModalWhat, m: Movie) => setModal({ what, m });

  return (
    <>
      <div className="row toolbar">
        <select value={filter} aria-label="Filter by pipeline state"
          onChange={e => setParam("status", e.target.value || null)}>
          <option value="">all states ({fmtNum(movies.length)})</option>
          {STATES.map(s => (
            <option key={s} value={s}>
              {STATE_LABEL[s] ?? s.replace("_", " ")} ({fmtNum(st?.counts[s] ?? 0)})
            </option>
          ))}
        </select>
        <input className="search" type="search" placeholder="Search films…" value={text}
          aria-label="Search by title or original title" onChange={e => setText(e.target.value)} />
        <div className="segbtns" role="group" aria-label="View">
          <button className={view === "grid" ? "active" : ""} onClick={() => setView("grid")}
            title="Cards — poster-led, one height each">▦ Grid</button>
          <button className={view === "list" ? "active" : ""} onClick={() => setView("list")}
            title="Table — sortable, more titles per screen">☰ List</button>
        </div>
        <span className="muted">
          {fmtNum(shown.length)}{shown.length !== movies.length && <> of {fmtNum(movies.length)}</>} films
        </span>
        <div className="spacer" />
        <Act cls="btn sec" run={searchAll}
          title="Search every pending title now instead of waiting for the timer (grabs up to the free download slots)">
          🔍 Search now</Act>
        <RescanButton />
        <RescanButton full />
        <RecheckButton />
        <LiveDot />
      </div>

      {note && <div className="sub" style={{ margin: "-4px 2px 10px" }}>{note}</div>}

      {/* The whole state breakdown at a glance — the select filters, this is what you read to
          decide what to filter TO. */}
      <div className="chips" style={{ marginBottom: 12 }}>
        {STATES.map(s => (
          <span className="chip" key={s}>
            {STATE_LABEL[s] ?? s.replace("_", " ")} <b>{fmtNum(st?.counts[s] ?? 0)}</b>
          </span>
        ))}
      </div>

      <div className="panel">
        {view === "grid"
          ? <div className="cardgrid fixed">
              {shown.map(m => (
                <MovieCard key={m.tmdb_id} m={m} dl={dlOf(m)} act={act} onOpen={openModal}
                  onNote={setNote} />
              ))}
            </div>
          : <div className="scroll-x">
              <table>
                <thead>
                  <tr>
                    <SortTh label="Title" k="title" sort={sort} onSort={onSort} />
                    <SortTh label="Status" k="status" sort={sort} onSort={onSort} width={210} />
                    <th>Candidate</th>
                    <th>File</th>
                    <SortTh label="Updated" k="updated" sort={sort} onSort={onSort} width={100} />
                    <th style={{ width: 120 }} />
                  </tr>
                </thead>
                <tbody>
                  {shown.map(m => (
                    <tr key={m.tmdb_id}>
                      <td>
                        <div className="titlecell">
                          <Poster src={m.poster} alt={m.title} />
                          <div>
                            <Star m={m} />{m.title}
                            <div className="sub">
                              → {m.original_title} ({m.year}) · {m.original_lang}
                            </div>
                            {m.ai_verdict && <div className="sub clamp2" title={m.ai_verdict}>
                              🤖 {m.ai_verdict}</div>}
                            {m.error && <div className="sub bad clamp2" title={m.error}>{m.error}</div>}
                          </div>
                        </div>
                      </td>
                      <td>
                        <Pill s={m.status} /> <AiPill s={m.ai_status} />
                        {/* The same bar the cards carry, so switching view does not change what
                            you can see about a title. */}
                        <TargetProgress m={m} />
                        {m.status === "sync_fail" && m.sync_delta != null &&
                          <div className="sub">Δ {m.sync_delta.toFixed(1)}s</div>}
                        {m.status === "downloading" && <DownloadBar dl={dlOf(m)} />}
                        {m.status === "ready" && <QueuedLine />}
                        {m.progress && <div className="sub"
                          style={{ color: m.status === "merging" ? "#5ee9a0" : "#ffcf8f" }}>
                          {m.progress}</div>}
                        <DriftBadge d={m.sync_drift} />
                      </td>
                      <td>
                        {m.candidate_title
                          ? <div title={m.candidate_title}>{m.candidate_title}
                              <div className="sub">
                                score {m.candidate_score ?? "—"} · {m.candidate_seeders ?? 0}s</div>
                            </div>
                          : <span className="muted">—</span>}
                      </td>
                      <td>
                        <span className="muted">{m.quality || "—"}</span>
                        <Tracks a={m.audio_langs} s={m.sub_langs} na={m.need_audio} ns={m.need_subs} />
                      </td>
                      <td className="muted">{fmtAgo(m.updated)}</td>
                      <td><MovieActions m={m} act={act} onOpen={openModal} onNote={setNote} /></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>}

        {shown.length === 0 && (movies.length > 0 || filter || urlQ
          ? <Empty>
              No films match{filter && <> the state <b>{STATE_LABEL[filter] ?? filter.replace("_", " ")}</b></>}
              {filter && urlQ && " and"}{urlQ && <> “{urlQ}”</>}.{" "}
              <button className="btn ghost small"
                onClick={() => { setParam("status", null); setText(""); setParam("q", null); }}>
                Clear the filters</button>
            </Empty>
          : <Empty>
              <b>No films recorded yet.</b> A record appears here only once a scan has read the
              file and found it short of its language profile — so an empty list means either
              nothing has been scanned, or every film already carries what it should.
              <div className="row" style={{ marginTop: 10 }}><RescanButton /></div>
            </Empty>)}
      </div>

      {modal?.what === "tune" &&
        <SyncEditor movie={modal.m} onClose={() => { setModal(null); void runAction(load); }} />}
      {modal?.what === "release" && <ReleaseModal
        title={`${modal.m.title} (${modal.m.year})`}
        load={() => api.candidates(modal.m.tmdb_id)}
        onGrab={c => api.grab(modal.m.tmdb_id, c.link, c.rid, c.title)}
        onClose={() => setModal(null)} onGrabbed={() => { void runAction(load); }} />}
      {modal?.what === "lips" && <LipSyncModal
        kind="movie" id={String(modal.m.tmdb_id)} title={modal.m.title}
        onClose={() => setModal(null)} onApplied={() => { void runAction(load); }} />}
    </>
  );
}

function legacyView(): "grid" | "list" {
  try { return localStorage.getItem("vo_view") === "list" ? "list" : "grid"; }
  catch { return "grid"; }
}
