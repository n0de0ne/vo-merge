import { useState } from "react";
import {
  api, type AiHealth, type Coverage, type CoverageLib, type Dash, type DashRecent,
  type DL, type Forecast, type HistorySummary,
} from "../api";
import { runAction, usePoll, useStored } from "../lib/poll";
import { go } from "../lib/router";
import { fmtAgo, fmtBytes, fmtDay, fmtIn, fmtNum, fmtPct, fmtSpeed } from "../lib/format";
import {
  Act, AiPill, DownloadBar, Empty, lang, LiveDot, Pill, Poster, QueuedLine, Tile,
} from "../components/ui";
import {
  ChartCard, ChartEmpty, DayBars, Legend, SERIES, Sparkline, StackBar, STATUS, TimeSeries,
  type Series, type Slice,
} from "../components/charts";

/* ======================================================================
   The dashboard: is the library getting better, is anything moving, and
   is anything waiting on ME.

   Everything here answers one of those three, in that order. The tiles are
   the state of the machine right now, the chart row is the direction it is
   heading (the one question no other page can answer, because no other
   endpoint has a time axis), and the two columns below are the work.
   ====================================================================== */

/* ---------------------------------------------------------------- coverage */

/** The status palette, indexed by StackBar's segment class, so the legend swatch below a bar is
 *  the same colour as the bar — the CSS owns the segment, this owns the dot beside its label. */
const SLICE_COLOR: Record<string, string> = {
  complete: STATUS.good, subs: STATUS.warn, audio: STATUS.serious,
  both: STATUS.crit, unread: STATUS.none,
};

function LibBar({ l }: { l: CoverageLib }) {
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
              // Scored against the files that TARGET this language, not the whole library: the
              // anime profile's `orig` slot resolves per title, so Blue Lock wants jpn and Arcane
              // does not. Dividing by the library total understates every one of them.
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

function CoveragePanel() {
  const [c, setC] = useState<Coverage | null>(null);
  const [err, setErr] = useState("");
  // A swallowed error here renders NOTHING — the whole chart just disappears, which reads as
  // "the feature was removed" rather than "the request failed". Say which it is.
  usePoll(() => api.coverage().then(x => { setC(x); setErr(""); })
    .catch(e => setErr(e?.message || "coverage unavailable")), 60000);

  if (err) return (
    <div className="panel">
      <div className="row panel-head"><b className="h2">Language coverage</b></div>
      <div className="sub bad">{err}</div>
    </div>
  );
  if (!c) return null;
  if (!c.probed) return (
    <div className="panel">
      <div className="row panel-head"><b className="h2">Language coverage</b></div>
      <Empty>
        Nothing has been probed yet, so there is no coverage to score. Reading the library is what
        fills this in — it costs one <code>mkvmerge</code> per file the first time and almost
        nothing after that.
        <div style={{ marginTop: 8 }}>
          <Act cls="btn" busyLabel="starting…" title="Drops the probe cache and re-reads every file"
            run={() => runAction(() => api.rescan("all", true))}>Re-read everything</Act>
        </div>
      </Empty>
    </div>
  );

  const pct = Math.round((c.complete / Math.max(c.probed, 1)) * 100);
  return (
    <div className="panel capped">
      <div className="row panel-head">
        <b className="h2">Language coverage</b>
        <span className="muted">
          {fmtNum(c.complete)} of {fmtNum(c.probed)} probed files meet their target · {pct}%
          {c.unreadable > 0 && <> · <span className="bad">{fmtNum(c.unreadable)} unreadable</span></>}
        </span>
        <div className="spacer" />
        <button className="btn sec" onClick={() => go("library")}>Browse files →</button>
      </div>
      <div className="panel-body">
        {c.libraries.map(l => <LibBar key={l.name} l={l} />)}
      </div>
    </div>
  );
}

/* ---------------------------------------------------------------- forecast */

/** "When does the library hit 90%?" — answerable from what is already kept, and worth showing
 *  because the coverage percentage moves too slowly for eyeballing it to tell you anything.
 *  Deliberately shows the working, and shows NO date when the backend says the remainder is
 *  blocked rather than merely pending. */
function ForecastPanel() {
  const [stored, setStored] = useStored<number>("vo.forecastTarget", 90);
  const target = stored > 0 && stored <= 100 ? stored : 90;
  const [f, setF] = useState<Forecast | null>(null);
  const [err, setErr] = useState("");
  usePoll(() => api.forecast(target).then(x => { setF(x); setErr(""); })
    .catch(e => setErr(e?.message || "forecast unavailable")), 300000, [target]);

  if (err) return null;                 // coverage above already reports a dead backend
  if (!f || !f.total) return null;

  const eta = f.eta_days != null && f.eta_ts
    ? new Date(f.eta_ts * 1000).toLocaleDateString(undefined,
        { year: "numeric", month: "short", day: "numeric" })
    : null;
  const blocked = f.blocked.no_release + f.blocked.ignored;
  return (
    <div className="panel">
      <div className="row panel-head">
        <b className="h2">Forecast</b>
        <span className="muted">at {f.pct}% now · {f.rate_used}/day over the last 30d</span>
        <div className="spacer" />
        <div className="segbtns">
          {[80, 90, 95, 100].map(v => (
            <button key={v} className={v === target ? "active" : ""}
              onClick={() => setStored(v)}>{v}%</button>
          ))}
        </div>
      </div>
      {/* The withheld case must not look like a confident one: a date the pipeline cannot deliver
          is worse than no date, so it gets its own treatment and says why. */}
      {f.needed === 0
        ? <div className="fc-hero ok">Already at {f.pct}% — target met 🎉</div>
        : eta
          ? <div className="fc-hero">~{eta}
              <span className="muted"> · {f.eta_days} days · {fmtNum(f.needed)} file(s) to go</span>
            </div>
          : <div className="fc-hero none">No date yet
              <span className="muted"> · {f.reason}</span>
            </div>}
      {blocked > 0 && f.needed > 0 && (
        <div className="sub" style={{ marginTop: 6 }}>
          {fmtNum(blocked)} of the remaining files can’t move on their own:{" "}
          {fmtNum(f.blocked.no_release)} found no release, {fmtNum(f.blocked.ignored)} were given
          up on{f.blocked.unreadable > 0 && <> · {fmtNum(f.blocked.unreadable)} unreadable</>}.
        </div>
      )}
      <div className="sub muted" style={{ marginTop: 6 }}>
        Straight-line from the last 30 days ({f.rate["7d"]}/day over 7d). It assumes the rate holds
        and that what’s left is as findable as what’s done — neither is guaranteed.
      </div>
    </div>
  );
}

/* ---------------------------------------------------------------- on-call AI */

/** The dispatcher runs on the host, outside this container, so the only evidence we have is
 *  whether it CONSUMES the tickets and whether it CALLS BACK. A wall of "needs you" with no
 *  callback ever means it never ran — which otherwise reads exactly like "it examined all of
 *  these and gave up". Saying which it is, is this panel's whole job. */
function AiHealthPanel({ ai, now }: { ai?: AiHealth; now: number }) {
  if (!ai || !ai.enabled) return null;
  const seen = ai.pending + ai.resolved + ai.failed + ai.needs_human;
  if (seen + ai.never_sent === 0) return null;
  const verdicts = ai.resolved + ai.failed;        // outcomes it actually reported itself
  const rate = verdicts > 0 ? Math.round((ai.resolved / verdicts) * 100) : null;
  const silent = verdicts === 0 && (ai.needs_human > 0 || ai.pending > 0);
  const ag = ai.agent;
  // The dispatcher is expected to remove each ticket the moment it claims it, so a ticket sitting
  // in the directory for hours is the most direct evidence in here that nothing is reading them.
  const stuck = !!ag && ag.waiting > 0 && (ag.oldest_age ?? 0) > ai.stale_min * 60;
  const stats: [string, number, string][] = [
    ["resolved", ai.resolved, "it fixed these"],
    ["failed", ai.failed, "it examined these and could not fix them"],
    ["needs_human", ai.needs_human,
      `flagged for you (includes the ${ai.stale_min}m no-callback flip)`],
    ["pending", ai.pending, "sent, still waiting for a verdict"],
    ["never_sent", ai.never_sent, "failed before AI review was on, or never ticketed"],
  ];
  return (
    <div className={"panel" + (stuck || silent ? " warnbar-soft" : "")}>
      <div className="row panel-head">
        <b className="h2">🤖 On-call AI</b>
        <span className="muted" title="a resident dispatcher on the host runs the Claude Code CLI
          on the tickets vo-merge writes to /config/ai-tickets">host dispatcher · Claude Code CLI</span>
        {ag && ag.waiting > 0 && (
          <span className={stuck ? "bad" : "muted"}>
            {ag.waiting} ticket{ag.waiting > 1 ? "s" : ""} waiting
            {ag.oldest_age != null && <>, oldest {fmtAgo(now - ag.oldest_age, now)}</>}
          </span>
        )}
        <span className="muted">{ai.last_callback
          ? `last verdict ${fmtAgo(ai.last_callback, now)}`
          : "no verdict ever received"}</span>
      </div>
      <div className="aistats">
        {stats.filter(([, n]) => n > 0).map(([k, n, tip]) => (
          <span className={`aistat ${k}`} key={k} title={tip}>
            {fmtNum(n)} <i>{k.replace("_", " ")}</i></span>
        ))}
      </div>
      {stuck || silent
        ? <div className="sub bad" style={{ marginTop: 6 }}>
            {silent ? "Never called back. " : ""}
            {stuck && ag
              ? <>Tickets are sitting unread in <code>{ag.dir}</code> — the dispatcher removes each
                one when it picks it up, so the host script almost certainly isn’t running. Check
                the loop in <b>Settings → User Scripts</b> on the host. </>
              : <>The host script that runs the Claude Code CLI on <code>{ag?.dir}</code> isn’t
                reporting, and nothing outside it can raise an error when it stops. </>}
            Until it runs, “needs you” here means the dispatcher went quiet — not that it examined
            these and gave up.
          </div>
        : rate != null && <div className="sub muted" style={{ marginTop: 6 }}>
            {rate}% of the {fmtNum(verdicts)} it reported on were resolved.</div>}
    </div>
  );
}

/* ---------------------------------------------------------------- recently merged */

/** `merged` is the terminal state for three different outcomes, so a row says which one it was:
 *  what was grafted in, or that the download replaced the file outright. */
function MergedRow({ r, now }: { r: DashRecent; now: number }) {
  return (
    <div className="dashrow">
      <Poster src={r.poster} alt={r.title} />
      <div className="dashrow-main">
        <div className="dashrow-title">{r.title}</div>
        <div className="sub addrow">
          {r.langs && <span className="lang-badge">+{r.langs} audio</span>}
          {r.subs && <span className="lang-badge subs">+{r.subs} subs</span>}
          {r.how === "replaced" && <span className="lang-badge repl">used the release</span>}
          {!r.langs && !r.subs && r.how !== "replaced" && <span className="muted">merged</span>}
        </div>
      </div>
      <span className="muted nowrap" style={{ fontSize: 12 }}>{fmtAgo(r.ts, now)}</span>
    </div>
  );
}

/* ---------------------------------------------------------------- the page */

const WINDOWS = [7, 30, 90];

export default function Overview() {
  const [d, setD] = useState<Dash | null>(null);
  const [dls, setDls] = useState<Record<string, DL>>({});
  const [logLines, setLogLines] = useState<string[]>([]);
  const [days, setDays] = useStored<number>("vo.histDays", 30);
  const [hist, setHist] = useState<HistorySummary | null>(null);
  const [histErr, setHistErr] = useState("");

  const loadDash = () => api.dashboard().then(setD);
  // No local catch on these three: usePoll reports centrally, so a dead backend shows up in the
  // banner instead of behind a screen that has quietly stopped updating.
  usePoll(loadDash, 6000);
  usePoll(() => api.downloads().then(x => setDls(x.items || {})), 4000);
  usePoll(() => api.logs().then(x => setLogLines(x.lines.slice(-14))), 10000);
  usePoll(() => api.history(days).then(x => { setHist(x); setHistErr(""); })
    .catch(e => setHistErr(e?.message || "history unavailable")), 60000, [days]);

  /** Every row action: run it, surface any failure, then re-read the dashboard so the row it
   *  acted on reflects the result rather than waiting out the next poll. */
  const act = (fn: () => Promise<unknown>) => async () => {
    await runAction(fn);
    await runAction(loadDash);
  };

  if (!d) return <div className="panel muted">loading overview…</div>;

  const n = (c: Record<string, number>, ...ss: string[]) => ss.reduce((a, s) => a + (c[s] || 0), 0);
  const both = (...ss: string[]) => n(d.movies, ...ss) + n(d.episodes, ...ss);
  const merged = both("merged");
  const attention = both("review", "sync_fail", "error");
  const backlog = both("pending", "searching", "no_release");
  const downloading = d.active.filter(a => a.status === "downloading");
  const merging = d.active.filter(a => a.status === "merging");
  const queued = d.active.filter(a => a.status === "ready");
  const totalSpeed = Object.values(dls).reduce((a, x) => a + (x.dlspeed || 0), 0);
  const diskPct = d.disk ? Math.round((1 - d.disk.free / d.disk.total) * 100) : 0;
  const dlOf = (a: { dl_hash?: string | null }) => dls[(a.dl_hash || "").toLowerCase()];

  const covDays = hist?.coverage.map(p => p.day) ?? [];
  const covSeries: Series[] = [{
    key: "pct", label: "meets target", color: SERIES[0],
    values: hist?.coverage.map(p => p.pct) ?? [],
  }];
  const thruDays = hist?.throughput.map(p => p.day) ?? [];
  const thruSeries: Series[] = [
    { key: "grafted", label: "grafted", color: SERIES[0],
      values: hist?.throughput.map(p => p.grafted) ?? [] },
    { key: "replaced", label: "replaced", color: SERIES[2],
      values: hist?.throughput.map(p => p.replaced) ?? [] },
    { key: "failed", label: "failed", color: STATUS.crit,
      values: hist?.throughput.map(p => p.failed) ?? [] },
  ];
  const t = hist?.totals;
  const didAnything = !!t && t.grafted + t.replaced + t.failed + t.already > 0;

  return (
    <>
      <div className="tilegrid">
        {/* `merged` is the terminal state for three outcomes; the sub-line counts only the work
            vo-merge actually did, so a library re-read can't read as thousands of merges. */}
        <Tile label="Correct now" value={fmtNum(merged)} tone="good"
          sub={<>{d.merged_24h} merged in 24 h · {d.merged_7d} in 7 d</>}
          spark={<Sparkline values={hist?.throughput.slice(-30).map(p => p.merged) ?? []} />} />
        <Tile label="Downloading"
          value={<>{d.inflight ?? downloading.length}
            <span className="t-cap"> / {d.inflight_cap}</span></>}
          tone={d.inflight == null ? "warn" : undefined}
          sub={totalSpeed > 0 ? "↓ " + fmtSpeed(totalSpeed)
            : d.inflight == null ? "qB unreachable" : "slots in use"} />
        {/* "0/2 · idle" beside five finished downloads is indistinguishable from a broken app, so
            the tile carries the REASON the worker isn't running and opens the queue itself. */}
        <Tile label="Merging"
          value={<>{merging.length}<span className="t-cap"> / {d.merge_cap}</span></>}
          tone={d.merge_hold ? "warn" : undefined}
          onClick={() => go("activity")}
          sub={d.merge_hold ? `held · ${d.merge_hold}`
            : merging.length ? merging[0].title
            : queued.length ? `${queued.length} queued — open` : "idle"} />
        <Tile label="Attention" value={fmtNum(attention)} tone={attention ? "bad" : undefined}
          onClick={() => go("problems")}
          sub={<>{both("review")} review · {both("sync_fail")} sync · {both("error")} error</>} />
        <Tile label="Backlog" value={fmtNum(backlog)}
          sub={<>{both("pending")} pending · {both("no_release")} no release</>} />
        {d.disk && (
          <Tile label="Donor disk" value={fmtBytes(d.disk.free)}
            tone={diskPct >= 90 ? "bad" : diskPct >= 80 ? "warn" : undefined}
            sub={<>free · {diskPct}% used</>} />
        )}
      </div>

      <div className="row" style={{ margin: "0 2px 10px" }}>
        <b className="h2">Trend</b>
        <span className="sub">the only view with a time axis — everything else is right now</span>
        <div className="spacer" />
        <div className="segbtns">
          {WINDOWS.map(v => (
            <button key={v} className={v === days ? "active" : ""}
              onClick={() => setDays(v)}>{v}d</button>
          ))}
        </div>
      </div>

      {histErr && <div className="panel"><div className="sub bad">{histErr}</div></div>}

      <div className="chartgrid" style={{ marginBottom: 14 }}>
        <ChartCard title="Library coverage"
          note={hist?.gained_pct != null
            ? <>{hist.gained_pct >= 0 ? "+" : ""}{hist.gained_pct} points
              {hist.first_sample && <> since {fmtDay(hist.first_sample)}</>}</>
            : undefined}
          table={
            <table>
              <thead><tr><th>Day</th><th>Complete</th><th>Files</th><th>Meets target</th></tr></thead>
              <tbody>
                {(hist?.coverage ?? []).map(p => (
                  <tr key={p.day}>
                    <td className="nowrap">{fmtDay(p.day)}</td>
                    <td>{p.complete == null ? "—" : fmtNum(p.complete)}</td>
                    <td>{p.total == null ? "—" : fmtNum(p.total)}</td>
                    <td>{p.pct == null
                      ? <span className="muted">no sample</span> : fmtPct(p.pct)}</td>
                  </tr>
                ))}
              </tbody>
            </table>}>
          {/* A sampled series with no samples is not a library at 0% — it is a library nobody has
              read yet, which has a different fix and therefore a different empty state. */}
          {hist && !hist.have_coverage
            ? <ChartEmpty>
                <div>
                  No scan has sampled the library yet, so there is nothing to plot. A read records
                  one point per day from then on.
                  <div style={{ marginTop: 8 }}>
                    <Act cls="btn" busyLabel="starting…"
                      title="Drops the probe cache and re-reads every file"
                      run={() => runAction(() => api.rescan("all", true))}>
                      Re-read everything</Act>
                  </div>
                </div>
              </ChartEmpty>
            : <TimeSeries area days={covDays} series={covSeries} unit="%" yMax={100}
                valueFmt={v => v.toFixed(1) + "%"} />}
        </ChartCard>

        <ChartCard title="What the pipeline did"
          /* `already` is a scan closing out a file that was correct on its own — not work this
             app did — so it is never stacked with the two that are. It still belongs on the
             card, because it explains a merged count far larger than these bars. */
          note={t && t.already > 0
            ? <>plus {fmtNum(t.already)} files found already correct</> : undefined}
          table={
            <table>
              <thead><tr><th>Day</th><th>Grafted</th><th>Replaced</th><th>Failed</th>
                <th>Already correct</th></tr></thead>
              <tbody>
                {(hist?.throughput ?? []).map(p => (
                  <tr key={p.day}>
                    <td className="nowrap">{fmtDay(p.day)}</td>
                    <td>{fmtNum(p.grafted)}</td><td>{fmtNum(p.replaced)}</td>
                    <td>{fmtNum(p.failed)}</td><td>{fmtNum(p.already)}</td>
                  </tr>
                ))}
              </tbody>
            </table>}>
          {hist && !didAnything
            ? <ChartEmpty>
                <div>
                  Nothing merged or failed in the last {days} days.
                  {days < 90 && <div style={{ marginTop: 8 }}>
                    <button className="btn sec" onClick={() => setDays(90)}>Look back 90 days</button>
                  </div>}
                </div>
              </ChartEmpty>
            : <>
                <DayBars days={thruDays} series={thruSeries} />
                <Legend series={thruSeries} values={t
                  ? [fmtNum(t.grafted), fmtNum(t.replaced), fmtNum(t.failed)] : undefined} />
              </>}
        </ChartCard>
      </div>

      <CoveragePanel />
      <ForecastPanel />

      <div className="dash-cols">
        <div className="panel capped tall">
          <div className="row panel-head">
            <b className="h2">Active now</b>
            <span className="muted">{d.active.length} item{d.active.length === 1 ? "" : "s"}</span>
            <div className="spacer" /><LiveDot />
          </div>
          <div className="panel-body">
            {d.active.length === 0 && <Empty>Nothing in flight.</Empty>}
            {d.active.map(a => (
              <div className="dashrow" key={a.key}>
                <Poster src={a.poster} alt={a.title} />
                <div className="dashrow-main">
                  <div className="dashrow-title">{a.title}
                    {a.count > 1 && <span className="muted"> · {a.count} eps</span>}</div>
                  {a.sub && <div className="sub" title={a.sub}>{a.sub}</div>}
                  {a.status === "downloading" && <DownloadBar dl={dlOf(a)} />}
                  {a.status === "ready" && <QueuedLine pos={a.queue_pos} />}
                  {/* `progress` is the pipeline's own explanation — the sync ticker while merging,
                      and WHY a finished download is not moving otherwise. Rendering it only for
                      `merging` is what kept a stuck row silent for 24 h. */}
                  {a.progress && (
                    <div className="sub"
                      style={{ color: a.status === "merging" ? "#5ee9a0" : "#ffcf8f" }}>
                      {a.progress}</div>
                  )}
                </div>
                <div className="col-end">
                  <Pill s={a.status} />
                  {/* Both act on the WHOLE row: a folded pack is one torrent behind 28 episodes,
                      and bumping one of them would leave the other 27 behind. */}
                  <div className="row tight">
                    {a.status !== "merging" && (
                      <Act cls="btn sec small"
                        title="Move to the front of the queue (the whole pack, if this row is one)"
                        run={act(() => api.queueTop(a.dl_hash
                          ? { hash: a.dl_hash }
                          : { kind: a.kind, key: a.key.slice(1) }))}>⤒</Act>
                    )}
                    <Act cls="btn sec small"
                      title={a.status === "merging"
                        ? "Kill the decode/mux running now"
                        : "Drop this download, blocklist the release and search for another"}
                      run={async () => {
                        if (a.status === "merging") {
                          await act(() => a.kind === "movie"
                            ? api.abortMovie(Number(a.key.slice(1)))
                            : api.abortEpisode(a.key.slice(1)))();
                        } else if (a.dl_hash && window.confirm(
                          `Drop this download and look for a different release?\n\n${a.title}`
                          + `${a.count > 1 ? ` (${a.count} episodes)` : ""}\n\n`
                          + `The torrent and its files are deleted, the release is blocklisted, `
                          + `and the records go back to searching. Your library files are `
                          + `untouched.`)) {
                          await act(() => api.cancelDownload(a.dl_hash!))();
                        }
                      }}>⛔</Act>
                  </div>
                </div>
              </div>
            ))}
          </div>
        </div>

        <div>
          <div className="panel capped">
            {/* Only what the on-call AI could not resolve, plus `review` (a human decision by
                definition). Everything else that failed is still with the AI and is counted, not
                listed — otherwise this is a list of things already being worked on. */}
            <div className="row panel-head">
              <b className="h2">Needs attention</b>
              {(d.ai_working ?? 0) > 0 && (
                <span className="muted"
                  title="failed records the AI is still working on — they appear here only if it can't fix them">
                  {d.ai_working} with the AI</span>
              )}
              <div className="spacer" />
              {attention > 0 && (
                <button className="btn sec small" onClick={() => go("problems")}>
                  Open problems →</button>
              )}
            </div>
            <div className="panel-body">
              {d.attention.length === 0 && (
                <Empty>{(d.ai_working ?? 0) > 0
                  ? `Nothing for you — ${d.ai_working} failure(s) are with the AI.`
                  : attention > 0
                    ? `${attention} failure(s), none flagged for you yet.`
                    : "All clear 🎉"}</Empty>
              )}
              {d.attention.map(a => (
                // Title on ONE truncated line with the pills pinned beside it, then the message
                // below at full width. The old shape put the pills in a right-hand COLUMN, which
                // stole ~110px from the text and stacked them as the panel narrowed.
                <div className="attn" key={a.key}>
                  <div className="attn-head">
                    <span className="attn-title" title={a.title}>{a.title}</span>
                    {(a.count ?? 1) > 1 && <span className="cnt">{a.count}</span>}
                    <Pill s={a.status} />
                    <AiPill s={a.ai_status} />
                  </div>
                  {a.error && <div className="sub bad clamp2" title={a.error}>{a.error}</div>}
                  {a.sync_delta != null && !a.error &&
                    <div className="sub">Δ {a.sync_delta.toFixed(1)}s</div>}
                  {a.ai_verdict &&
                    <div className="sub clamp2" title={a.ai_verdict}>🤖 {a.ai_verdict}</div>}
                </div>
              ))}
            </div>
          </div>

          <AiHealthPanel ai={d.ai} now={d.now} />

          <div className="panel capped short">
            <div className="row panel-head">
              <b className="h2">Recently merged</b>
              {d.merged_kinds && (
                <span className="muted"
                  title="'already correct' files were never touched by vo-merge — a scan found they met their target and closed the record out">
                  {fmtNum(d.merged_kinds.grafted)} grafted
                  {d.merged_kinds.replaced > 0 && <> · {fmtNum(d.merged_kinds.replaced)} replaced</>}
                  {d.merged_kinds.already > 0 &&
                    <> · {fmtNum(d.merged_kinds.already)} already correct</>}
                </span>
              )}
            </div>
            <div className="panel-body">
              {d.recent.length === 0 && <Empty>No merges yet.</Empty>}
              {d.recent.map((r, i) => <MergedRow key={i} r={r} now={d.now} />)}
            </div>
            {/* This panel is a ten-row window; everything older is only reachable through here. */}
            {d.recent.length > 0 && (
              <div className="row panel-foot">
                <span className="sub">the 10 most recent</span>
                <div className="spacer" />
                <button className="btn sec small" onClick={() => go("activity/history")}>
                  See all →</button>
              </div>
            )}
          </div>
        </div>
      </div>

      <div className="panel">
        <div className="row panel-head">
          <b className="h2">Activity</b>
          <div className="spacer" />
          <button className="btn sec small" onClick={() => go("system")}>Full log →</button>
        </div>
        <pre className="logs mini">{logLines.join("")}</pre>
        <div className="chips" style={{ marginTop: 10 }}>
          <span className="chip">grab <b>{d.grab_mode}</b></span>
          {d.next_runs.search != null &&
            <span className="chip">next search <b>{fmtIn(d.next_runs.search, d.now)}</b></span>}
          {d.next_runs.finish != null &&
            <span className="chip">next merge check <b>{fmtIn(d.next_runs.finish, d.now)}</b></span>}
          {d.next_runs.stall != null &&
            <span className="chip">next stall sweep <b>{fmtIn(d.next_runs.stall, d.now)}</b></span>}
        </div>
      </div>
    </>
  );
}
