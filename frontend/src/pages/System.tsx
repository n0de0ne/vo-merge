import { useMemo, useRef, useState, type ReactNode } from "react";
import {
  api, type AiHealth, type AiLog, type Dash, type RecheckState, type RepairState,
  type RescanState, type Status,
} from "../api";
import { runAction, usePoll, useStored } from "../lib/poll";
import { go, setParam, useRoute } from "../lib/router";
import { fmtAgo, fmtIn, fmtNum } from "../lib/format";
import { Act, AiPill, Empty, LiveDot, Pill } from "../components/ui";

/* ======================================================================
   System: is the machine itself well?

   Every other page is about the library. This one is about the thing that
   works on it — dependencies, the scheduler, the on-call AI, the log — and
   it exists because all of that evidence was previously either invisible or
   scattered: `deps_down` was collected and never shown, the scheduler's next
   runs were three chips on the Overview, the AI panels were bolted to the
   Review tab, and "Logs" was a tab with a <pre> in it.

   The organising question per tab is "who do I call": Health says whether
   anything outside this container has stopped answering, Tasks says whether
   the timers inside it are still firing, On-call AI says whether the fixer
   on the host is alive, and Logs is the raw evidence for all three.
   ====================================================================== */

type Tab = "health" | "ai" | "tasks" | "logs";
const TABS: [Tab, string][] = [
  ["health", "Health"], ["ai", "On-call AI"], ["tasks", "Tasks"], ["logs", "Logs"],
];

/** `deps_down` values are a DURATION in seconds, not a timestamp. That distinction is the whole
 *  point of the watchdog — the per-cycle "qB error, returning" log lines could only ever say
 *  "right now", and a blip and a two-hour outage read identically. `fmtAgo` would render this as
 *  a point in time and say the opposite of what it means. */
function fmtFor(s: number): string {
  if (s < 90) return `${Math.round(s)}s`;
  if (s < 5400) return `${Math.round(s / 60)}m`;
  if (s < 172800) return `${(s / 3600).toFixed(1)}h`;
  return `${Math.round(s / 86400)}d`;
}

const GB = (n?: number | null) => (n == null ? "?" : `${n.toFixed(1)} GB`);

/* ---------------------------------------------------------------- health */

type Tone = "good" | "warn" | "bad";
interface Check { key: string; tone: Tone; what: string; detail: ReactNode; }

/** Status marks always ship their word, never just a colour or a glyph — these are the rows
 *  someone reads when something is already wrong. */
const MARK: Record<Tone, { cls: string; label: string }> = {
  good: { cls: "ok", label: "✓ ok" },
  warn: { cls: "warn", label: "⚠ holding" },
  bad: { cls: "bad", label: "✕ problem" },
};

/** The dependency watchdog names qBittorrent `qbittorrent`; `POST /test/{which}` calls it `qb`.
 *  Two names for one service, and reading `deps_down["qb"]` silently reports it as healthy. */
const SERVICES: { id: string; label: string; dep?: string; why: string }[] = [
  { id: "prowlarr", label: "Prowlarr", dep: "prowlarr",
    why: "release search. An unreachable indexer is not a verdict — records stay pending rather "
       + "than being written no_release for a day on a decision nobody made." },
  { id: "radarr", label: "Radarr", dep: "radarr",
    why: "film metadata, original language, and the rescan after a merge replaces a file." },
  { id: "sonarr", label: "Sonarr", dep: "sonarr",
    why: "series metadata and the aired ↔ absolute episode numbering table anime needs." },
  { id: "qb", label: "qBittorrent", dep: "qbittorrent",
    why: "downloads. vo-merge fetches the .torrent itself and uploads the bytes, because qB sits "
       + "behind the VPN killswitch and cannot reach Prowlarr on the LAN." },
  { id: "plex", label: "Plex", why: "scan + analyze after a merge, on every configured server. "
       + "Not probed by the watchdog, so only a test here says whether it answers." },
];
const SERVICE_LABEL: Record<string, string> =
  Object.fromEntries(SERVICES.map(s => [s.dep ?? s.id, s.label]));

function checksOf(st: Status): Check[] {
  const out: Check[] = [];

  out.push(st.enabled
    ? { key: "enabled", tone: "good", what: "Pipeline",
        detail: <>Enabled · grab mode <b>{st.grab_mode}</b>
          {st.grab_mode === "approval" && <> — nothing is grabbed until you approve it</>}.</> }
    : { key: "enabled", tone: "bad", what: "Pipeline",
        detail: <>The master switch is <b>off</b>: nothing is searched, grabbed or merged. Note
          that an unparseable <code>config.json</code> also lands here, because the fallback
          config is <code>enabled: false</code> — check the log if you did not turn this off.</> });

  if (st.paused)
    out.push({ key: "paused", tone: "warn", what: "Paused",
      detail: <>No new search, grab or merge starts.{" "}
        {st.merging_now
          ? <>{st.merging_now} merge(s) already running will finish — aborting a mux mid-write
              would leave a corrupt library file.</>
          : <>Nothing is left in flight.</>}{" "}
        Scans keep running, which is the point of pausing.</> });
  else if (st.hold === "scanning")
    out.push({ key: "hold", tone: "good", what: "Grabs held", detail:
      <>A library scan is running, so searches wait for it. Grabbing off a half-finished scan
        picks releases for gaps that may not exist and burns slots the scan is about to
        re-price.</> });
  else if (st.hold)
    out.push({ key: "hold", tone: "warn", what: "On hold", detail: <>{st.hold}</> });

  const down = Object.entries(st.deps_down || {});
  if (down.length === 0)
    out.push({ key: "deps", tone: "good", what: "Dependencies",
      detail: <>Every configured service answered the watchdog's last probe (it sweeps every
        3 minutes).</> });
  for (const [name, secs] of down)
    out.push({ key: "dep-" + name, tone: "bad", what: SERVICE_LABEL[name] ?? name,
      detail: <>Unreachable for <b>{fmtFor(secs)}</b> — continuously, not a blip. The pipeline
        degrades safely meanwhile (nothing is mis-recorded), but no new work that needs it can
        start. Test it below once you think it is back.</> });

  const disk = st.disk;
  const holdLeft = disk?.hold_until && disk.hold_until > Date.now() / 1000 ? disk.hold_until : null;
  if (disk?.low)
    out.push({ key: "disk", tone: "bad", what: "Disk space",
      detail: <><b>{GB(disk.free_gb)}</b> free — below <code>disk_floor_gb</code>, so
        <b> all merging is held</b> until space is freed. Downloads and scans continue; a mux is
        the one thing here that writes gigabytes, and a half-written output compounds the very
        disk-full that caused it.</> });
  else if (disk)
    out.push({ key: "disk", tone: holdLeft ? "warn" : "good", what: "Disk space",
      detail: holdLeft
        ? <><b>{GB(disk.free_gb)}</b> free, above the floor, but a pair that would not fit cooled
            the merge worker off for another {fmtIn(holdLeft)}. It retries on its own.</>
        : <><b>{GB(disk.free_gb)}</b> free on the merge target, above the floor.</> });

  return out;
}

function HealthTab({ st, checks, scopeSeries }:
  { st: Status; checks: Check[]; scopeSeries?: boolean }) {
  const [tests, setTests] =
    useState<Record<string, { ok: boolean; msg: string; at: number }>>({});
  // A failed test answers HTTP 200 with {ok:false,error}, so it is a result to render rather than
  // an error to throw — only an unreachable vo-merge itself reaches the global banner.
  const test = (id: string) => runAction(async () => {
    const r = await api.test(id);
    setTests(t => ({ ...t, [id]: { ok: r.ok, msg: r.error || "", at: Date.now() / 1000 } }));
  });
  const clean = checks.every(c => c.tone === "good");

  return (
    <>
      <div className="panel flush">
        <table>
          <thead><tr><th style={{ width: 96 }}>State</th><th style={{ width: 170 }}>Check</th>
            <th>What it means</th></tr></thead>
          <tbody>
            {clean && (
              <tr>
                <td className="ok nowrap">✓ ok</td>
                <td><b>All checks passing</b></td>
                <td className="muted">Every dependency answers, the disk is above the floor and
                  the pipeline is running. Nothing here needs you.</td>
              </tr>
            )}
            {checks.map(c => (
              <tr key={c.key}>
                <td className={MARK[c.tone].cls + " nowrap"}>{MARK[c.tone].label}</td>
                <td><b>{c.what}</b></td>
                <td>{c.detail}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="section-title">Services</div>
      <div className="panel flush">
        <table>
          <thead><tr><th style={{ width: 170 }}>Service</th><th>What it is for</th>
            <th style={{ width: 210 }}>Result</th><th style={{ width: 90 }} /></tr></thead>
          <tbody>
            {SERVICES.map(s => {
              const secs = s.dep ? st.deps_down?.[s.dep] : undefined;
              const r = tests[s.id];
              const untracked = s.id === "sonarr" && scopeSeries === false;
              return (
                <tr key={s.id}>
                  <td><b>{s.label}</b>
                    {secs != null && <div className="sub bad">down {fmtFor(secs)}</div>}
                    {untracked && <div className="sub muted">TV scope off — not probed</div>}
                  </td>
                  <td className="muted">{s.why}</td>
                  <td>
                    {!r && <span className="muted">not tested</span>}
                    {r && r.ok && <span className="ok">✓ answered · {fmtAgo(r.at)}</span>}
                    {r && !r.ok && <span className="bad">✕ {r.msg || "failed"}</span>}
                  </td>
                  <td><div className="row">
                    <Act cls="btn sec small" run={() => test(s.id)} busyLabel="testing…"
                      title={`Ask ${s.label} for its status right now`}>Test</Act></div></td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <div className="row" style={{ marginTop: -4 }}>
        <Act cls="btn sec" busyLabel="testing…"
          run={async () => { for (const s of SERVICES) await test(s.id); }}>Test all</Act>
        <span className="muted">Credentials and URLs live in{" "}
          <a href="#/settings" onClick={() => go("settings")}>Settings</a>.</span>
      </div>
    </>
  );
}

/* ---------------------------------------------------------------- on-call AI */

/** Ported from the old Review tab. The panel's job is to distinguish two states that look
 *  identical from a list of records: an agent that examined every failure and gave up, and a
 *  dispatcher that never ran at all. Only the callback and the ticket directory can tell them
 *  apart, so both are stated in words. */
function AiHealthPanel({ ai, now }: { ai?: AiHealth; now: number }) {
  if (!ai || !ai.enabled)
    return (
      <div className="panel">
        <div className="row" style={{ marginBottom: 6 }}><b>🤖 On-call AI</b></div>
        <Empty>
          AI review is off (<code>ai_tickets</code>). Failures park in{" "}
          <a href="#/problems" onClick={() => go("problems")}>Problems</a> and wait for a human
          instead of being escalated to the dispatcher within 3 minutes.
          <div className="row" style={{ marginTop: 8 }}>
            <button className="btn sec" onClick={() => go("settings")}>Open Settings</button>
          </div>
        </Empty>
      </div>
    );

  const seen = ai.pending + ai.resolved + ai.failed + ai.needs_human;
  const verdicts = ai.resolved + ai.failed;        // outcomes it actually reported itself
  const rate = verdicts > 0 ? Math.round((ai.resolved / verdicts) * 100) : null;
  const silent = verdicts === 0 && (ai.needs_human > 0 || ai.pending > 0);
  const ag = ai.agent;
  // core.ticket() will not overwrite a ticket of the same kind, so the dispatcher is expected to
  // remove each file as it picks it up. A ticket sitting there for hours is the most direct
  // evidence available in here that nothing on the host is reading them.
  const stuck = !!ag && ag.waiting > 0 && (ag.oldest_age ?? 0) > ai.stale_min * 60;

  return (
    <div className={`panel ${stuck || silent ? "warnbar-soft" : ""}`}>
      <div className="row" style={{ marginBottom: 6 }}>
        <b>🤖 On-call AI</b>
        <span className="muted" title="deploy/dispatcher runs the Claude Code CLI over the tickets
          vo-merge writes to /config/ai-tickets">resident dispatcher · Claude Code CLI</span>
        {ag && ag.waiting > 0 && <span className={stuck ? "bad" : "muted"}>
          {ag.waiting} ticket{ag.waiting > 1 ? "s" : ""} waiting
          {ag.oldest_age != null && <>, oldest {fmtAgo(now - ag.oldest_age, now)}</>}
        </span>}
        <span className="muted">{ai.last_callback
          ? `last verdict ${fmtAgo(ai.last_callback, now)}`
          : "no verdict ever received"}</span>
      </div>

      {seen + ai.never_sent === 0
        ? <Empty>Nothing has been escalated yet — no record has reached error, review or
            sync_fail since AI review was turned on. That is the good case.</Empty>
        : <div className="aistats">
            {([["resolved", ai.resolved, "it fixed these"],
               ["failed", ai.failed, "it examined these and could not fix them"],
               ["needs_human", ai.needs_human,
                `flagged for you (includes the ${ai.stale_min}m no-callback flip)`],
               ["pending", ai.pending, "sent, still waiting for a verdict"],
               ["never_sent", ai.never_sent,
                "failed before AI review was on, or over the ticket cap and not yet sent"],
              ] as [string, number, string][]).filter(([, n]) => n > 0).map(([k, n, tip]) => (
              <span className={`aistat ${k}`} key={k} title={tip}>
                {fmtNum(n)} <i>{k.replace("_", " ")}</i></span>))}
          </div>}

      {stuck || silent
        ? <div className="sub bad" style={{ marginTop: 6 }}>
            {silent ? "Never called back. " : ""}
            {stuck && ag
              ? <>Tickets are sitting unread in <code>{ag.dir}</code> — the dispatcher removes each
                one when it picks it up, so the host script almost certainly isn't running. Check
                it in <b>Settings → User Scripts</b> on the host. </>
              : <>The host script that runs the Claude Code CLI on <code>{ag?.dir}</code> isn't
                reporting, and nothing outside it can raise an error when it stops. </>}
            Until it runs, "needs you" here means the dispatcher went quiet — not that it examined
            these and gave up.</div>
        : rate != null && <div className="sub muted" style={{ marginTop: 6 }}>
            {rate}% of the {fmtNum(verdicts)} it reported on were resolved.</div>}
    </div>
  );
}

/** What the AI actually did. This cannot be built from any list selected by pipeline status: a
 *  callback deliberately leaves `status` untouched, so a record the AI FIXED has usually moved on
 *  (back to pending, or downloading, or merged). Without this query its work is invisible exactly
 *  when it succeeds and the only trace left in the UI is its failures. */
function AiSolvedPanel() {
  const route = useRoute();
  const outcome = (["resolved", "failed", "needs_human", "all"] as const)
    .find(k => k === route.query.get("ai")) ?? "resolved";
  const [d, setD] = useState<AiLog | null>(null);
  const [open, setOpen] = useState(true);
  usePoll(() => api.aiLog(outcome, 50).then(setD), 15000, [outcome]);

  const tabs: [typeof outcome, string][] = [
    ["resolved", "Solved"], ["failed", "Couldn’t fix"],
    ["needs_human", "Handed back"], ["all", "All"]];
  const total = d ? Object.values(d.counts).reduce((a, b) => a + b, 0) : 0;

  return (
    <div className="panel capped tall">
      <div className="row panel-head" style={{ marginBottom: open ? 8 : 0 }}>
        <button className="btn sec" style={{ padding: "2px 8px" }} aria-expanded={open}
          onClick={() => setOpen(o => !o)}>{open ? "▾" : "▸"}</button>
        <b>🤖 What the AI did</b>
        {d && <span className="muted">{fmtNum(d.counts.resolved ?? 0)} solved of {fmtNum(total)}
          {" "}it reported on</span>}
        <div className="spacer" />
        {open && <div className="segbtns">
          {tabs.map(([k, label]) => (
            <button key={k} className={outcome === k ? "active" : ""}
              onClick={() => setParam("ai", k)}>
              {label}{k !== "all" && <span className="segn">{fmtNum(d?.counts[k] ?? 0)}</span>}
            </button>))}
        </div>}
      </div>
      {open && <div className="panel-body">
        {!d ? <div className="muted">loading…</div>
          : d.items.length === 0
            ? <Empty>Nothing in this category. A verdict only appears here once the dispatcher
                POSTs <code>/ai_result</code> back — if this stays empty while tickets pile up
                above, the dispatcher is not running.</Empty>
            : <table>
              <thead><tr><th>Title</th><th>What it did</th><th>Now</th><th>When</th></tr></thead>
              <tbody>
                {d.items.map(i => (
                  <tr key={`${i.kind}${i.key}`}>
                    <td><b>{i.title}</b>{i.sub && <span className="muted"> {i.sub}</span>}</td>
                    <td>
                      <AiPill s={i.ai_status} />
                      {i.ai_verdict && <div className="sub" title={i.ai_verdict}>{i.ai_verdict}</div>}
                    </td>
                    <td><Pill s={i.status} />
                      {i.error && i.ai_status !== "resolved" &&
                        <div className="sub bad clamp2" title={i.error}>{i.error}</div>}</td>
                    <td className="muted nowrap">{fmtAgo(i.ai_at, d.now)}</td>
                  </tr>))}
              </tbody>
            </table>}
      </div>}
    </div>
  );
}

/* ---------------------------------------------------------------- tasks */

/** Every scheduled job, named by what it actually does — `finish` and `promote` mean nothing
 *  from their ids, and the whole value of listing them is being able to tell "the timer is late"
 *  from "the timer is not registered at all". Unknown ids fall through to the raw id rather than
 *  being dropped, so a job added to the scheduler shows up here without a frontend change. */
const JOBS: Record<string, string> = {
  search: "Re-scans for gaps, then searches Prowlarr for the pending backlog and grabs what fits "
        + "the free download slots.",
  finish: "Reconciles vanished torrents, drops stalled ones (blocklisting the release), and "
        + "re-queues merges an interrupted container left behind.",
  promote: "Moves downloads that reached 100% out of `downloading` and onto the merge queue, "
         + "freeing their in-flight slot immediately.",
  stall: "Watchdog: crash recovery for searching/grabbed records, AI escalation, and the "
       + "dispatcher / dependency / disk alarms.",
  housekeeping: "Daily: prunes files that vanished from the library, purges the recycle bin and "
              + "frees donors kept for parked failures.",
  backup: "Nightly VACUUM INTO /config/backup, plus a copy of config.json — the pair the startup "
        + "integrity check restores from.",
};

function TasksTab({ d }: { d: Dash | null }) {
  const [scan, setScan] = useState<RescanState | null>(null);
  const [rech, setRech] = useState<RecheckState | null>(null);
  const [rep, setRep] = useState<RepairState | null>(null);
  const [note, setNote] = useState("");
  const busy = !!(scan?.running || rech?.running || rep?.running);
  // Poll hard only while a pass is actually moving; these are three requests a tick.
  usePoll(() => Promise.all([
    api.rescanState().then(setScan),
    api.recheckState().then(setRech),
    api.repairState().then(setRep),
  ]), busy ? 3000 : 30000, [busy]);

  const runs = Object.entries(d?.next_runs || {}).sort((a, b) => a[1] - b[1]);
  const scanned = (scan?.films ?? 0) + (scan?.episodes ?? 0);
  const dropped = (scan?.pruned ?? 0) + (scan?.pruned_records ?? 0);

  return (
    <>
      <div className="panel flush">
        <table>
          <thead><tr><th style={{ width: 180 }}>Job</th><th>What it does</th>
            <th style={{ width: 100 }}>Next run</th><th style={{ width: 110 }} /></tr></thead>
          <tbody>
            {runs.length === 0 && (
              <tr><td colSpan={4}>
                <Empty>The scheduler reported no jobs. <code>next_runs</code> is <code>{"{}"}</code>{" "}
                  when the call into APScheduler fails, so this means the scheduler is not running
                  — not that nothing is due. The log says why.</Empty>
              </td></tr>
            )}
            {runs.map(([id, ts]) => (
              <tr key={id}>
                <td><b>{id}</b></td>
                <td className="muted">{JOBS[id] ?? "—"}</td>
                <td className="nowrap">in {fmtIn(ts, d?.now)}</td>
                <td><div className="row">
                  {id === "search" && (
                    <Act cls="btn sec small" busyLabel="starting…"
                      title="Run the search sweep now instead of waiting for its timer"
                      run={() => runAction(async () => {
                        const r = await api.searchAll();
                        setNote(r.started
                          ? `search sweep started · ${fmtNum(r.pending ?? 0)} pending, `
                            + `${r.slots ?? "?"} slot(s) free`
                          : r.note || "the sweep did not start");
                      })}>Run now</Act>
                  )}
                </div></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {note && <div className="row"><span className="muted">{note}</span></div>}
      <div className="sub muted">Intervals live in Settings → Queues &amp; limits
        (<code>search_interval_min</code>, <code>finish_interval_min</code>,{" "}
        <code>promote_interval_min</code>). They are clamped to a minimum on save: a zero interval
        became a one-second full sweep.</div>

      <div className="section-title">Library passes</div>

      <div className="panel">
        <div className="row">
          <b>Read the library</b>
          <span className="muted">mkvmerge decides the gap; Radarr and Sonarr only supply
            metadata.</span>
          <div className="spacer" />
          <Act cls="btn sec" busyLabel="starting…" disabled={busy}
            title="Read only the files with no result yet — new imports, files changed on disk, and
              anything an interrupted pass never reached. Cheap to run any time."
            run={() => runAction(async () => {
              const r = await api.rescan("all");
              if (!r.started) setNote(r.note || "a scan is already running");
              setScan(await api.rescanState());
            })}>Scan new files</Act>
        </div>
        {scan?.running
          ? <div className="muted" style={{ marginTop: 6 }}>
              {scan.scope} · {scan.phase}…
              {(scan.read ?? 0) > 0 && <> · read {fmtNum(scan.read)}</>}
              {(scan.reused ?? 0) > 0 && <> · reused {fmtNum(scan.reused)}</>}
            </div>
          : scan && scan.finished > 0
            // The full and progressive modes share one state object, so the line names the mode
            // that actually RAN rather than assuming the button someone pressed here.
            ? <div className="muted" style={{ marginTop: 6 }}>
                last {scan.full ? "full re-read" : "progressive pass"} of {scan.scope},{" "}
                {fmtAgo(scan.finished, d?.now)} · read {fmtNum(scan.read ?? 0)} file(s)
                {(scan.reused ?? 0) > 0 && <> · {fmtNum(scan.reused)} already cached</>}
                {" "}· {fmtNum(scanned)} gap(s)
                {dropped > 0 && <> · {dropped} deleted entr{dropped === 1 ? "y" : "ies"} removed</>}
                {scan.probes.unreadable > 0 &&
                  <span className="bad"> · {fmtNum(scan.probes.unreadable)} unreadable</span>}
                {scan.error && <span className="bad"> · failed: {scan.error}</span>}
              </div>
            : <div className="muted" style={{ marginTop: 6 }}>No scan has run since this container
                started. Coverage stays empty until one has.</div>}
        <div className="sub muted" style={{ marginTop: 4 }}>A full re-read (which drops the probe
          cache first) is on{" "}
          <a href="#/library" onClick={() => go("library")}>All files</a> — that is the one to use
          when you don't trust the cached answer.</div>
      </div>

      <div className="panel">
        <div className="row">
          <b>Re-check finished titles</b>
          <span className="muted">"merged" only ever meant a merge ran, and "no release" only
            meant nothing existed when we last looked.</span>
          <div className="spacer" />
          <Act cls="btn sec" busyLabel="starting…" disabled={busy}
            title="Re-probe every record parked in a settled state and re-open the ones still
              below target, ignoring the no_release cooldown. The release already tried stays
              blocklisted, so a re-opened record searches for a different one; ignored titles are
              left alone."
            run={() => runAction(async () => {
              const r = await api.recheck("all");
              if (!r.started) setNote(r.note || "a re-check is already running");
              setRech(await api.recheckState());
            })}>Re-check finished</Act>
        </div>
        {rech?.running
          ? <div className="muted" style={{ marginTop: 6 }}>
              re-probed {fmtNum(rech.checked)} of {fmtNum(rech.total)} · re-opened{" "}
              {fmtNum(rech.reopened)}</div>
          : rech && rech.finished > 0
            ? <div className="muted" style={{ marginTop: 6 }}>
                last pass {fmtAgo(rech.finished, d?.now)} · {fmtNum(rech.reopened)} re-opened of{" "}
                {fmtNum(rech.total)} · {fmtNum(rech.complete)} genuinely complete
                {rech.gone > 0 && <> · {rech.gone} file(s) gone</>}
                {rech.unreadable > 0 && <span className="bad"> · {rech.unreadable} unreadable</span>}
                {rech.error && <span className="bad"> · failed: {rech.error}</span>}
              </div>
            : <div className="muted" style={{ marginTop: 6 }}>Never run.</div>}
      </div>

      <div className="panel">
        <div className="row">
          <b>Replace audio-less files</b>
          <span className="muted">Deletes media, so it is only ever started from the plan on{" "}
            <a href="#/library" onClick={() => go("library")}>All files</a> — this is the running
            state.</span>
        </div>
        {rep?.running
          ? <div className="muted" style={{ marginTop: 6 }}>
              {rep.phase} · re-probed {fmtNum(rep.checked)} of {fmtNum(rep.total)} · deleted{" "}
              {fmtNum(rep.deleted)}</div>
          : rep && rep.finished > 0
            ? <div className="muted" style={{ marginTop: 6 }}>
                last run {fmtAgo(rep.finished, d?.now)} · {fmtNum(rep.deleted)} deleted and
                re-searched
                {rep.skipped.length > 0 && <> · {fmtNum(rep.skipped.length)} skipped</>}
                {rep.error && <span className="bad"> · failed: {rep.error}</span>}
              </div>
            : <div className="muted" style={{ marginTop: 6 }}>Never run.</div>}
      </div>
    </>
  );
}

/* ---------------------------------------------------------------- logs */

function LogsTab() {
  const route = useRoute();
  const q = route.query.get("q") || "";
  const [lines, setLines] = useState<string[]>([]);
  const [auto, setAuto] = useStored("vo.logs.auto", true);
  // The log must load once even with auto off, or turning it off and reloading shows an empty
  // panel that reads as "nothing was logged".
  const loaded = useRef(false);
  usePoll(() => {
    if (!auto && loaded.current) return;
    loaded.current = true;
    return api.logs().then(x => setLines(x.lines));
  }, 5000, [auto]);

  const needle = q.toLowerCase();
  const shown = useMemo(
    () => (needle ? lines.filter(l => l.toLowerCase().includes(needle)) : lines),
    [lines, needle]);

  // A download the browser builds from what is on screen: the filter is usually the point of
  // saving it, so it saves the VISIBLE lines rather than silently the whole tail.
  function save() {
    const url = URL.createObjectURL(new Blob([shown.join("")], { type: "text/plain" }));
    const a = document.createElement("a");
    a.href = url;
    a.download = `vo-merge-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-")}.log`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 10000);   // revoking immediately cancels the save
  }

  return (
    <div className="panel capped full">
      <div className="row panel-head">
        <input className="search" type="search" placeholder="filter lines…" value={q}
          onChange={e => setParam("q", e.target.value)} />
        <Act cls="btn sec" busyLabel="…"
          run={() => runAction(() => api.logs().then(x => setLines(x.lines)))}>Refresh</Act>
        <label className="muted">
          <input type="checkbox" checked={auto} onChange={e => setAuto(e.target.checked)} /> auto
        </label>
        {auto && <LiveDot />}
        <div className="spacer" />
        <span className="muted">
          {q ? <>{fmtNum(shown.length)} of {fmtNum(lines.length)} lines</>
             : <>{fmtNum(lines.length)} lines (the server keeps the last 300)</>}
        </span>
        <button className="btn sec" onClick={save} disabled={shown.length === 0}>Download</button>
      </div>
      {shown.length === 0
        ? <div className="panel-body">
            <Empty>{lines.length === 0
              ? <>Nothing logged yet — the file is written as the pipeline works. If this stays
                  empty while jobs are due on the Tasks tab, the container is not writing to
                  <code> /config</code>.</>
              : <>No line matches <b>{q}</b>. The tail is only the last 300 lines, so an older
                  event has already scrolled off.{" "}
                  <button className="btn sec small" onClick={() => setParam("q", null)}>
                    Clear filter</button></>}</Empty>
          </div>
        : <pre className="logs panel-body">{shown.join("")}</pre>}
    </div>
  );
}

/* ---------------------------------------------------------------- page */

export default function System() {
  const route = useRoute();
  const tab: Tab = TABS.find(([k]) => k === route.query.get("t"))?.[0] ?? "health";

  const [st, setSt] = useState<Status | null>(null);
  const [d, setD] = useState<Dash | null>(null);
  usePoll(() => api.status().then(setSt), 5000);
  usePoll(() => api.dashboard().then(setD), 10000);

  const checks = useMemo(() => (st ? checksOf(st) : []), [st]);
  const bad = checks.filter(c => c.tone !== "good").length;
  const waiting = d?.ai?.agent?.waiting ?? 0;

  return (
    <>
      <div className="row toolbar">
        <div className="segbtns">
          {TABS.map(([k, label]) => (
            <button key={k} className={tab === k ? "active" : ""} onClick={() => setParam("t", k)}>
              {label}
              {k === "health" && bad > 0 && <span className="segn">{bad}</span>}
              {k === "ai" && waiting > 0 && <span className="segn">{waiting}</span>}
            </button>))}
        </div>
        <div className="spacer" />
        <LiveDot />
      </div>

      {tab === "health" && (st
        ? <HealthTab st={st} checks={checks} scopeSeries={d?.scope_series} />
        : <div className="panel muted">reading the machine's own state…</div>)}

      {tab === "ai" && <>
        {d ? <AiHealthPanel ai={d.ai} now={d.now} />
           : <div className="panel muted">asking the dispatcher how it is doing…</div>}
        <AiSolvedPanel />
      </>}

      {tab === "tasks" && <TasksTab d={d} />}

      {tab === "logs" && <LogsTab />}
    </>
  );
}
