import { useEffect, useMemo, useRef, useState } from "react";
import {
  api, type Coverage, type CoverageLib, type HistorySummary, type LibItem, type LibPage,
  type RepairPlan, type RepairState, type RescanState,
} from "../api";
import { runAction, usePoll } from "../lib/poll";
import { setParam, useRoute } from "../lib/router";
import { fmtDay, fmtNum, fmtPct } from "../lib/format";
import { Act, Empty, lang } from "../components/ui";
import {
  ChartCard, ChartEmpty, Legend, SERIES, StackBar, STATUS, TimeSeries,
  type Series, type Slice,
} from "../components/charts";

/* ======================================================================
   The library, as files rather than as records.

   `movies`/`episodes` are a list of PROBLEMS — scan() inserts a record only
   when a file has a gap — so they can never answer "how much of the library
   is correct". The `probes` table is the only complete inventory, and this
   page is its two views: /coverage says how much, /library says which ones.
   Both read the same classifier, so the chart above and the list below can
   never disagree about what "complete" means.
   ====================================================================== */

const PAGE = 200;
type LibState = "incomplete" | "complete" | "unreadable" | "all";
const LIB_STATES: LibState[] = ["incomplete", "complete", "unreadable", "all"];

/** The one error that means the FILE is broken. The other three are statements about our tools,
 *  and repair only ever acts on this one. */
const BROKEN = "no audio track";

/** An mkvmerge track list is empty for four unrelated reasons and only one of them is about the
 *  media. Collapsing them is how someone deletes a file that plays perfectly well. */
const ERR_MEANING: [string, string, string][] = [
  [BROKEN, "mkvmerge read the container and ffprobe agrees: zero audio streams.",
    "Broken — nothing to graft into, so the only repair is a fresh copy."],
  ["audio mkvmerge can't read (N stream(s) per ffprobe)",
    "A codec we can't mux, in a container we can read.",
    "Leave it — the file plays, vo-merge just can't add tracks to it."],
  ["unsupported container",
    "mkvmerge can't parse the format at all, so its empty track list says nothing.",
    "Leave it — remux it yourself if you want vo-merge to work on it."],
  ["unreadable", "Neither mkvmerge nor ffprobe could open it.",
    "Leave it — check the file by hand before assuming it is dead."],
];

/* ---------------------------------------------------------------- scanning */

/** Both buttons for one scope share ONE state object (a single scan runs at a time, under
 *  SCAN_LOCK), so the finished line has to name the mode that actually ran — otherwise a cheap
 *  progressive pass reports itself as the full re-read you asked for and you trust a cached
 *  answer you meant to throw away. */
function ScanControls() {
  const [st, setSt] = useState<RescanState | null>(null);
  const [note, setNote] = useState("");
  usePoll(() => api.rescanState().then(setSt).catch(() => {}),
    st?.running ? 3000 : 30000, [st?.running]);

  const running = !!st?.running;
  const mine = st?.scope === "all";
  const start = (full: boolean) => runAction(async () => {
    const r = await api.rescan("all", full);
    setNote(r.started ? "" : (r.note || "another pass is already running"));
    setSt(await api.rescanState());
  });

  const done = mine && st && !running && st.finished > 0;
  const gaps = (st?.films ?? 0) + (st?.episodes ?? 0);
  const dropped = (st?.pruned ?? 0) + (st?.pruned_records ?? 0);

  return (
    <>
      <Act cls="btn sec" busyLabel="starting…" disabled={running} run={() => start(false)}
        title={"Read only the files with no result yet — new imports, files changed on disk, and "
          + "anything an interrupted pass never reached. It picks up where the last one stopped, "
          + "so it is cheap to run any time."}>Scan new files</Act>
      <Act cls="btn" busyLabel="starting…" disabled={running} run={() => start(true)}
        title={"Drop the probe cache and read every file again with mkvmerge, even ones that look "
          + "unchanged, then re-decide what each is missing. What you want when you don't trust "
          + "the cached answer. Minutes on a big library."}>Re-read everything</Act>
      {/* `.sub` is declared after `.warn`, so "sub warn" would render muted — the one message
          here that says a click did nothing must not be the quietest thing in the row. */}
      {note && <span className="warn" style={{ fontSize: 12 }}>{note}</span>}
      {running && mine && <span className="sub">
        {st!.full ? "re-reading" : "scanning"} · {st!.phase}…
        {(st!.read ?? 0) > 0 && <> · read {fmtNum(st!.read)}</>}
        {(st!.reused ?? 0) > 0 && <> · reused {fmtNum(st!.reused)}</>}</span>}
      {running && !mine && <span className="sub">busy: {st!.scope} scan running</span>}
      {done && !st.error && <span className="sub">
        last {st.full ? "re-read" : "scan"}: read {fmtNum(st.read ?? 0)} file(s)
        {(st.reused ?? 0) > 0 && <> · {fmtNum(st.reused)} already cached</>}
        {" "}· {fmtNum(gaps)} gap(s)
        {dropped > 0 && <> · {dropped} deleted entr{dropped === 1 ? "y" : "ies"} removed</>}
        {st.probes.unreadable > 0 &&
          <span className="bad"> · {fmtNum(st.probes.unreadable)} unreadable</span>}</span>}
      {mine && st?.error && <span className="err">rescan failed: {st.error}</span>}
    </>
  );
}

/* ---------------------------------------------------------------- the list */

function LibRow({ i }: { i: LibItem }) {
  // "French, English audio · English subs" — grouped by kind rather than one clause per language,
  // so a file short of three things still reads as one short phrase.
  const miss = [
    i.missing_audio.length ? `${i.missing_audio.map(lang).join(", ")} audio` : "",
    i.missing_subs.length ? `${i.missing_subs.map(lang).join(", ")} subs` : "",
  ].filter(Boolean);
  return (
    <div className={`librow ${i.state}`}>
      <div className="libmain">
        <div className="libtitle" title={i.title}>{i.title}</div>
        <div className="libfile" title={i.path}>{i.file}</div>
      </div>
      <div className="libtracks">
        {i.state === "unreadable"
          ? <span className="bad" title={i.err || ""}>⚠ {i.err || "probe failed"}</span>
          : <>
              <span title="audio languages read from the file itself">
                🔊 {i.audio.map(lang).join(", ") || <span className="muted">none tagged</span>}</span>
              <span title="subtitle languages read from the file itself">
                💬 {i.subs.map(lang).join(", ") || <span className="muted">none</span>}</span>
            </>}
      </div>
      <div className="libmiss">
        {i.state === "complete"
          ? <span className="ok-txt">✓ meets target</span>
          : miss.length > 0 ? <span className="needs">missing {miss.join(" · ")}</span> : null}
      </div>
    </div>
  );
}

/* ---------------------------------------------------------------- repair */

/** A file with no audio at all can never be fixed by grafting — there is nothing to sync against
 *  and nothing to keep — so the only repair is a fresh copy. This deletes the operator's media,
 *  so it is never a one-click action: ask the backend what it WOULD do, show that, and only then
 *  offer the run. */
function RepairPanel({ n, onDone }: { n: number; onDone: () => void }) {
  const [plan, setPlan] = useState<RepairPlan | null>(null);
  const [st, setSt] = useState<RepairState | null>(null);
  const [err, setErr] = useState("");
  const running = !!st?.running;
  usePoll(() => { if (running) api.repairState().then(setSt).catch(() => {}); }, 3000, [running]);

  const check = () => runAction(async () => { setErr(""); setPlan(await api.repairPlan()); });

  const run = () => runAction(async () => {
    const known = plan!.total - plan!.unknown;
    if (!window.confirm(`Delete ${known} file(s) with no audio track and ask Radarr/Sonarr to `
      + `download a replacement?\n\nEach file is re-probed first and skipped if it turns out to `
      + `be readable. Deletion goes through the *arrs, so they mark the title missing and search `
      + `again.\n\nThis cannot be undone.`)) return;
    setErr("");
    const r = await api.repairRun();
    if (!r.started) { setErr(r.note || "could not start"); return; }
    setSt(await api.repairState());
  });

  const finished = st && !running && st.finished > 0;
  const deletable = plan ? plan.total - plan.unknown : 0;
  return (
    <div className="panel repair">
      <div className="row">
        <b className="h2">🔇 {fmtNum(n)} file(s) carry no audio at all</b>
        <span className="sub">mkvmerge and ffprobe both read them and found zero audio streams,
          so there is nothing to graft into — the only fix is a fresh copy.</span>
        <div className="spacer" />
        {!plan && !running && !finished &&
          <Act cls="btn sec" busyLabel="Checking…" run={check}>Check what can be replaced</Act>}
      </div>
      {err && <div className="err" style={{ marginTop: 6 }}>{err}</div>}

      {plan && !running && !finished && (
        <div className="repair-plan">
          <div>
            <b>{fmtNum(deletable)}</b> would be deleted and re-searched via Radarr/Sonarr
            {plan.unknown > 0 && <> · <b>{fmtNum(plan.unknown)}</b> skipped — not in
              Radarr/Sonarr, so deleting them would just lose the title</>}
          </div>
          {plan.candidates.filter(c => c.known).slice(0, 8).map(c => (
            <div className="muted small" key={c.path} title={c.path}>
              {c.title || c.path.split("/").pop()}</div>
          ))}
          {deletable > 8 && <div className="muted small">+{fmtNum(deletable - 8)} more</div>}
          <div className="row" style={{ marginTop: 8 }}>
            <Act cls="btn danger" disabled={plan.total === plan.unknown} busyLabel="Starting…"
              run={run}>Delete {fmtNum(deletable)} and re-search</Act>
            <button className="btn sec" onClick={() => setPlan(null)}>Cancel</button>
          </div>
        </div>
      )}

      {running && <div className="sub" style={{ marginTop: 6 }}>
        {st!.phase} · re-probed {fmtNum(st!.checked)} of {fmtNum(st!.total)}
        {" "}· deleted {fmtNum(st!.deleted)}</div>}

      {finished && (
        <div className="repair-plan">
          <div><b>{fmtNum(st!.deleted)}</b> deleted and re-searched
            {st!.skipped.length > 0 && <> · <b>{fmtNum(st!.skipped.length)}</b> skipped</>}</div>
          {st!.skipped.slice(0, 8).map(s => (
            <div className="muted small" key={s.path} title={s.path}>
              {s.path.split("/").pop()} — {s.reason}</div>
          ))}
          {st!.skipped.length > 8 &&
            <div className="muted small">+{fmtNum(st!.skipped.length - 8)} more</div>}
          {st!.error && <div className="err">{st!.error}</div>}
          <div className="row" style={{ marginTop: 8 }}>
            <button className="btn sec"
              onClick={() => { setSt(null); setPlan(null); onDone(); }}>Done</button>
          </div>
        </div>
      )}
    </div>
  );
}

/* ---------------------------------------------------------------- coverage now */

/** The status palette indexed by StackBar's segment class, so the legend swatch is the same
 *  colour as the bar segment it names — the CSS owns the segment, this owns the dot. */
const SLICE_COLOR: Record<string, string> = {
  complete: STATUS.good, subs: STATUS.warn, audio: STATUS.serious,
  both: STATUS.crit, unread: STATUS.none,
};

function LibBar({ l, onPick }: { l: CoverageLib; onPick: (name: string) => void }) {
  const slices: Slice[] = [
    { key: "complete", label: "meets target", n: l.complete, cls: "complete" },
    { key: "subs", label: "missing subtitles", n: l.missing_subs, cls: "subs" },
    { key: "audio", label: "missing audio", n: l.missing_audio, cls: "audio" },
    { key: "both", label: "missing audio + subtitles", n: l.missing_both, cls: "both" },
    { key: "unread", label: "unreadable", n: l.unreadable, cls: "unread" },
  ];
  const pct = Math.round((l.complete / Math.max(l.total, 1)) * 100);
  return (
    <div className="covlib">
      <div className="covhead">
        <b>{l.name}</b>
        <span className="muted">
          {fmtNum(l.total)} files · targets {l.targets.audio.join("/")} audio</span>
        <div className="spacer" />
        {/* This page is the one place the chart can hand you the files behind a segment. */}
        <button className="btn ghost small" onClick={() => onPick(l.name)}
          title={`List the ${l.name} files that don't meet their target`}>
          show what's short →</button>
        <span className="covpct" title={`${fmtNum(l.complete)} of ${fmtNum(l.total)} files meet it`}>
          {pct}%</span>
      </div>
      <StackBar slices={slices} total={l.total} />
      <div className="legend">
        {slices.filter(s => s.n > 0).map(s => (
          <span className="k" key={s.key}>
            <span className="sw" style={{ background: SLICE_COLOR[s.cls] }} />
            {s.label}<b>{fmtNum(s.n)}</b>
          </span>
        ))}
      </div>
      <div className="covlangs">
        {(["audio", "subs"] as const).map(which => (
          <div className="covlangrow" key={which}>
            <span className="covlangkind">{which === "audio" ? "🔊 audio" : "💬 subs"}</span>
            {l.targets[which].map(code => {
              const have = (which === "audio" ? l.audio : l.subs)[code] ?? 0;
              // Scored against the files that TARGET this language, never the library total: the
              // anime profile's `orig` slot resolves per title, so Blue Lock wants jpn and Arcane
              // does not. Dividing by the total understates every language in that library.
              const of = (which === "audio" ? l.audio_of : l.subs_of)?.[code] ?? l.total;
              const p = (have / Math.max(of, 1)) * 100;
              return (
                <span className="covlang" key={code}
                  title={`${fmtNum(have)} of the ${fmtNum(of)} files that target ${lang(code)} `
                    + `${which} have it`}>
                  <span className="covlangname">{lang(code)}</span>
                  <span className="covmini">
                    <span className={p >= 95 ? "" : p >= 50 ? "warn" : "bad"}
                      style={{ width: `${p}%` }} /></span>
                  <span className="covlangpct">{Math.round(p)}%</span>
                </span>
              );
            })}
          </div>
        ))}
      </div>
    </div>
  );
}

function CoveragePanel({ onPick }: { onPick: (name: string) => void }) {
  const [c, setC] = useState<Coverage | null>(null);
  const [err, setErr] = useState("");
  // A swallowed error here rendered NOTHING — the chart simply vanished, which reads as "the
  // feature was removed" rather than "the request failed". Say which it is.
  usePoll(() => api.coverage().then(x => { setC(x); setErr(""); })
    .catch(e => setErr(e?.message || "coverage unavailable")), 60000);

  if (err) return (
    <div className="panel">
      <div className="row panel-head"><b className="h2">Language coverage</b></div>
      <div className="err">{err}</div>
    </div>
  );
  if (!c) return null;
  if (!c.probed) return null;      // the list above already carries the "nothing read yet" state

  const pct = Math.round((c.complete / Math.max(c.probed, 1)) * 100);
  return (
    <div className="panel capped">
      <div className="row panel-head">
        <b className="h2">Language coverage</b>
        <span className="muted">
          {fmtNum(c.complete)} of {fmtNum(c.probed)} probed files meet their target · {pct}%
          {c.unreadable > 0 && <> · <span className="bad">{fmtNum(c.unreadable)} unreadable</span></>}
        </span>
      </div>
      <div className="panel-body">
        {c.libraries.map(l => <LibBar key={l.name} l={l} onPick={onPick} />)}
      </div>
    </div>
  );
}

/* ---------------------------------------------------------------- coverage over time */

const pctOf = (l?: { total: number; complete: number }) =>
  l && l.total > 0 ? (l.complete / l.total) * 100 : null;

/** Per-library completion, one line each, over the last 90 days. The whole-library line is on the
 *  same plot on purpose: a library that is dragging the average down is only visible against it.
 *  A day with no sample is `null`, not 0 — nobody read the library that day, which is a different
 *  fact from "the library was empty". */
function CoverageHistory() {
  const [h, setH] = useState<HistorySummary | null>(null);
  const [err, setErr] = useState("");
  usePoll(() => api.history(90).then(x => { setH(x); setErr(""); })
    .catch(e => setErr(e?.message || "history unavailable")), 300000);

  // Colour follows the library NAME, not its position in the list, so a library appearing or
  // dropping out of the window never repaints the others.
  const names = useMemo(() => {
    const s = new Set<string>();
    h?.coverage.forEach(p => Object.keys(p.libs ?? {}).forEach(n => s.add(n)));
    return [...s].sort();
  }, [h]);

  const days = h?.coverage.map(p => p.day) ?? [];
  const series: Series[] = [
    { key: "__all", label: "whole library", color: SERIES[0],
      values: h?.coverage.map(p => p.pct) ?? [] },
    ...names.map((name, i) => ({
      key: name, label: name, color: SERIES[(i + 1) % SERIES.length],
      values: (h?.coverage ?? []).map(p => pctOf(p.libs?.[name])),
    })),
  ];
  const latest = series.map(s => {
    const v = [...s.values].reverse().find(x => x != null);
    return v == null ? null : fmtPct(v, 0);
  });

  return (
    <ChartCard title="Coverage over time"
      note={h?.gained_pct != null
        ? <>{h.gained_pct >= 0 ? "+" : ""}{h.gained_pct} points
          {h.first_sample && <> since {fmtDay(h.first_sample)}</>}</>
        : undefined}
      table={h && (
        <table>
          <thead>
            <tr><th>Day</th>{series.map(s => <th key={s.key}>{s.label}</th>)}</tr>
          </thead>
          <tbody>
            {days.map((day, i) => (
              <tr key={day}>
                <td className="nowrap">{fmtDay(day)}</td>
                {series.map(s => (
                  <td key={s.key}>{s.values[i] == null
                    ? <span className="muted">no sample</span>
                    : fmtPct(s.values[i] as number)}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>)}>
      {err ? <ChartEmpty><span className="bad">{err}</span></ChartEmpty>
        : !h ? <ChartEmpty>reading the last 90 days…</ChartEmpty>
        : !h.have_coverage
          ? <ChartEmpty>
              <div>
                No scan has sampled the library yet, so there is nothing to plot. One point per
                day is recorded from the first read onwards.
                <div style={{ marginTop: 8 }}>
                  <Act cls="btn" busyLabel="starting…"
                    title="Drops the probe cache and re-reads every file"
                    run={() => runAction(() => api.rescan("all", true))}>Re-read everything</Act>
                </div>
              </div>
            </ChartEmpty>
          : <>
              <TimeSeries area days={days} series={series} unit="%" yMax={100}
                valueFmt={v => v.toFixed(1) + "%"} />
              <Legend series={series} values={latest} />
            </>}
    </ChartCard>
  );
}

/* ---------------------------------------------------------------- the page */

export default function Library() {
  const route = useRoute();
  // The URL is user-editable, and an unknown state is a 422 from the API — i.e. a hand-typed
  // link would render an error where the list should be. Fall back instead.
  const asked = (route.query.get("state") ?? "") as LibState;
  const state: LibState = LIB_STATES.includes(asked) ? asked : "incomplete";
  const lib = route.query.get("lib") ?? "";
  const q = route.query.get("q") ?? "";
  const [page, setPage] = useState(0);
  const [d, setD] = useState<LibPage | null>(null);
  const [err, setErr] = useState("");

  const load = () => api.library({ state, lib, q, limit: PAGE, offset: page * PAGE })
    .then(x => { setD(x); setErr(""); })
    .catch(e => setErr(e?.message || "load failed"));
  usePoll(load, 30000, [state, lib, q, page]);

  // A filter change invalidates the page number — page 4 of a 2-page result is blank. The reset
  // rides in the SAME update as the filter so the poll re-arms once with both new values, instead
  // of firing a throwaway request at the stale offset first.
  const pick = (key: string, v: string) => { setParam(key, v); setPage(0); };

  // The query lives in the URL (a filtered list is worth sharing) but is typed here first: one
  // request per keystroke would re-arm the poll on every letter.
  const [qText, setQText] = useState(q);
  const pushed = useRef(q);
  useEffect(() => {
    if (q !== pushed.current) { pushed.current = q; setQText(q); }   // Back button, or a link
  }, [q]);
  useEffect(() => {
    if (qText === pushed.current) return;
    const t = setTimeout(() => { pushed.current = qText; pick("q", qText); }, 300);
    return () => clearTimeout(t);
    /* eslint-disable-next-line */
  }, [qText]);

  const counts = d?.counts;
  const all = counts ? counts.complete + counts.incomplete + counts.unreadable : 0;
  const views: [LibState, string, number][] = [
    ["incomplete", "Target not met", counts?.incomplete ?? 0],
    ["complete", "Complete", counts?.complete ?? 0],
    ["unreadable", "Unreadable", counts?.unreadable ?? 0],
    ["all", "All", all],
  ];
  const shown = d?.items.length ?? 0;
  const pages = Math.ceil((d?.total ?? 0) / PAGE);

  return (
    <>
      <div className="panel capped full">
        <div className="row panel-head">
          <b className="h2">Library</b>
          <span className="sub">every file the scanner has read, scored against its language
            target</span>
          <div className="spacer" />
          <ScanControls />
        </div>

        <div className="row toolbar libfilters panel-head">
          {/* Counts cover the whole lib+q selection, not the page, so the headers stay honest
              while paging — they are read straight off the response, never recomputed. */}
          <div className="segbtns">
            {views.map(([k, label, n]) => (
              <button key={k} className={state === k ? "active" : ""}
                onClick={() => pick("state", k)}>
                {label} <span className="segn">{fmtNum(n)}</span>
              </button>
            ))}
          </div>
          <select value={lib} onChange={e => pick("lib", e.target.value)}
            aria-label="Library folder">
            <option value="">All libraries</option>
            {(d?.libraries ?? []).map(l =>
              <option key={l.name} value={l.name}>{l.name} ({fmtNum(l.total)})</option>)}
          </select>
          <input className="search" type="search" value={qText} aria-label="Filter by title or path"
            placeholder="filter by title or path…" onChange={e => setQText(e.target.value)} />
          {(lib || q) && <button className="btn ghost small"
            onClick={() => { setQText(""); pushed.current = ""; setParam("lib", ""); pick("q", ""); }}>
            clear filters</button>}
        </div>

        {err && <div className="err">{err}</div>}

        {state === "unreadable" && d && Object.keys(d.error_kinds).length > 0 && (
          <div className="errkinds">
            {Object.entries(d.error_kinds).map(([k, n]) => (
              <span key={k} className={k === BROKEN ? "ek broken" : "ek"}>
                {k} <b>{fmtNum(n)}</b></span>
            ))}
          </div>
        )}

        {/* Keyed off the ITEMS, not the total: a page past the end (the list shrank under a
            repair, say) has a total and nothing to show, and rendering the header with
            "showing 801–800" instead of a way back is the least useful answer available. */}
        {d && shown === 0 && !err && (
          <Empty>
            {all === 0 ? <>
              Nothing has been read yet, so there is no inventory to show. The first pass costs one
              <code> mkvmerge</code> per file and almost nothing after that.
              <div style={{ marginTop: 8 }}>
                <Act cls="btn" busyLabel="starting…"
                  run={() => runAction(() => api.rescan("all", true))}>Re-read everything</Act>
              </div>
            </> : page > 0 ? <>
              This page is past the end of the results.
              <div style={{ marginTop: 8 }}>
                <button className="btn sec" onClick={() => setPage(0)}>← back to the first page</button>
              </div>
            </> : <>
              Nothing here matches those filters.
              {(lib || q) && <div style={{ marginTop: 8 }}>
                <button className="btn sec" onClick={() =>
                  { setQText(""); pushed.current = ""; setParam("lib", ""); pick("q", ""); }}>
                  Clear filters</button>
              </div>}
            </>}
          </Empty>
        )}

        {d && shown > 0 && <>
          {/* The header is a grid lining up with a body that reserves a 10px scrollbar gutter —
              .libhead.panel-head reserves the same, or every column boundary is 10px out. */}
          <div className="libhead panel-head">
            <span>title</span><span>on the file</span><span>gap</span>
          </div>
          <div className="liblist panel-body">
            {d.items.map(i => <LibRow key={i.path} i={i} />)}
          </div>
          <div className="row panel-foot">
            <span className="sub">showing {fmtNum(page * PAGE + 1)}–{fmtNum(page * PAGE + shown)}
              {" "}of {fmtNum(d.total)}</span>
            <div className="spacer" />
            <button className="btn sec" disabled={page === 0}
              onClick={() => setPage(p => p - 1)}>← prev</button>
            <span className="sub">page {page + 1} of {fmtNum(pages)}</span>
            <button className="btn sec" disabled={page + 1 >= pages}
              onClick={() => setPage(p => p + 1)}>next →</button>
          </div>
        </>}
      </div>

      {state === "unreadable" && (
        <div className="panel">
          <div className="row panel-head">
            <b className="h2">What “unreadable” means</b>
            <span className="sub">four unrelated things, and only one of them is about the file</span>
          </div>
          <table>
            <thead><tr><th>error</th><th>what happened</th><th>what to do</th></tr></thead>
            <tbody>
              {ERR_MEANING.map(([k, what, act]) => (
                <tr key={k}>
                  <td className="mono">{k}</td>
                  <td className="sub">{what}</td>
                  <td className={k === BROKEN ? "warn" : "sub"}>{act}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {(d?.repairable ?? 0) > 0 && <RepairPanel n={d!.repairable} onDone={load} />}

      <div className="section-title">Coverage</div>
      <CoverageHistory />
      <CoveragePanel onPick={name => { setParam("state", "incomplete"); pick("lib", name); }} />
    </>
  );
}
