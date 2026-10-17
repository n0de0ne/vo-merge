import { useEffect, useMemo, useState } from "react";
import {
  api, type DL, type Dash, type DashActive, type EventPage, type EventRow,
  type QueueView, type Status, type TvStatus,
} from "../api";
import { runAction, usePoll } from "../lib/poll";
import { go, setParam, useRoute } from "../lib/router";
import { fmtAgo, fmtBytes, fmtEta, fmtNum, fmtSpeed } from "../lib/format";
import { Act, DownloadBar, Empty, LiveDot, Pill, Poster, STATE_LABEL } from "../components/ui";

/** Activity, in Sonarr's sense: what is moving right now (Queue) and what has already happened
 *  (History). The old UI had neither — the merge queue was a modal hanging off one dashboard tile
 *  and nothing anywhere recorded a transition, so "why is this stuck" and "what did it do last
 *  night" were both unanswerable from the screen. */
export default function Activity({ tab }: { tab: "queue" | "history" }) {
  return (
    <>
      <div className="row toolbar">
        <div className="segbtns">
          <button className={tab === "queue" ? "active" : ""} onClick={() => go("activity")}>
            ⇅ Queue
          </button>
          <button className={tab === "history" ? "active" : ""}
            onClick={() => go("activity/history")}>
            🕘 History
          </button>
        </div>
        <div className="spacer" />
        <LiveDot />
      </div>
      {tab === "queue" ? <QueueTab /> : <HistoryTab />}
    </>
  );
}

/* ==================================================================== queue */

/** One row per DOWNLOAD, not per record: a season pack is a single torrent behind up to fifty
 *  episodes, and listing it fifty times both drowns the panel and invites bumping one episode
 *  while its twenty-nine siblings stay behind. */
interface DlGroup { hash: string | null; kind: string; key: string; count: number; rows: DashActive[]; }

function QueueTab() {
  const [dls, setDls] = useState<Record<string, DL>>({});
  const [dlErr, setDlErr] = useState("");
  const [q, setQ] = useState<QueueView | null>(null);
  const [dash, setDash] = useState<Dash | null>(null);
  const [st, setSt] = useState<Status | null>(null);
  const [tv, setTv] = useState<TvStatus | null>(null);
  const [searchMsg, setSearchMsg] = useState("");

  const loadDls = () =>
    api.downloads().then(d => { setDls(d.items || {}); setDlErr(d.error || ""); });
  const loadQueue = () => api.queue(100).then(setQ);
  const loadSlow = async () => {
    setDash(await api.dashboard());
    setSt(await api.status());
    setTv(await api.tvStatus());
  };

  const merging = (q?.merging_now ?? 0) > 0;
  usePoll(loadDls, 4000);
  // Faster while a merge is actually running: the position numbers and the "merging" row are the
  // only feedback a half-hour remux gives, and at 5s a queue that IS draining looks frozen.
  usePoll(loadQueue, merging ? 3000 : 5000, [merging]);
  usePoll(loadSlow, 8000);

  const refresh = () => {
    void runAction(loadQueue); void runAction(loadDls); void runAction(loadSlow);
  };

  const groups: DlGroup[] = useMemo(() => {
    const by = new Map<string, DlGroup>();
    for (const a of dash?.active ?? []) {
      if (a.status !== "downloading") continue;
      const hash = a.dl_hash ? a.dl_hash.toLowerCase() : null;
      const k = hash ?? `${a.kind}:${a.key}`;
      const g = by.get(k);
      if (g) { g.rows.push(a); g.count += a.count || 1; }
      // dashboard keys are prefixed ("m603692" / "e12:1:5"); the record key is what follows.
      else by.set(k, { hash, kind: a.kind, key: a.key.slice(1), count: a.count || 1, rows: [a] });
    }
    return [...by.values()];
  }, [dash]);

  const speed = groups.reduce(
    (n, g) => n + ((g.hash && dls[g.hash]?.dlspeed) || 0), 0);
  // Torrents qB knows about that no `downloading` record claims: finished donors waiting their
  // turn to merge, mostly — worth stating, because otherwise the counts look wrong.
  const unattached = Object.keys(dls).length - groups.filter(g => g.hash && dls[g.hash]).length;

  return (
    <>
      {/* ---------------------------------------------------------- downloading */}
      <div className="panel">
        <div className="row panel-head">
          <b className="h2">⬇ Downloading</b>
          <span className="muted">
            {groups.length} download{groups.length === 1 ? "" : "s"}
            {speed > 0 && <> · {fmtSpeed(speed)}</>}
          </span>
          <div className="spacer" />
          {dash && <span className="badge" title="in-flight downloads against max_inflight_downloads">
            {dash.inflight ?? groups.length} / {dash.inflight_cap} slots
          </span>}
        </div>

        {dlErr && <div className="panel warnbar" style={{ marginBottom: 10 }}>
          ⚠ qBittorrent is unreachable — {dlErr}. Progress can't be read until it answers again;
          nothing has been lost and the pipeline picks these up when it comes back.
        </div>}

        {groups.length === 0 && <Empty>
          Nothing is downloading. A title moves here once a search finds a release that carries a
          language it is missing — use <b>Search now</b> below to start a sweep immediately instead
          of waiting for the hourly one.
        </Empty>}

        {groups.map(g => {
          const a = g.rows[0];
          const dl = g.hash ? dls[g.hash] : undefined;
          const eta = dl ? fmtEta(dl.eta) : "";
          return (
            <div className="qrow" key={g.hash ?? `${g.kind}:${g.key}`}>
              <Poster src={a.poster} alt={a.title} sm />
              <div className="qrow-main">
                <div className="qrow-title">
                  {a.title}
                  {g.count > 1 && <span className="cnt">{g.count} episodes</span>}
                </div>
                {a.sub && <div className="sub" title={a.sub}>{a.sub}</div>}
                <DownloadBar dl={dl} />
                {dl
                  ? <div className="sub">
                      {fmtBytes(dl.downloaded)} of {fmtBytes(dl.size)} · {dl.seeds || 0} seeds
                      {eta && <> · {eta}</>}
                    </div>
                  : <div className="sub warn">
                      qB has no torrent with this hash — the stall sweep reconciles it within a
                      few minutes and re-searches.
                    </div>}
                {/* the pipeline's own explanation of why a download is not moving. Rendering it
                    only while merging is what once kept a stuck row silent for 24 hours. */}
                {a.progress && <div className="sub" style={{ color: "#ffcf8f" }}>{a.progress}</div>}
              </div>
              <div className="qrow-end">
                <Pill s={a.status} />
                <div className="row tight">
                  <Act cls="btn sec small"
                    title="Move to the front of the search and merge queues (the whole pack, if this row is one)"
                    run={async () => {
                      await runAction(() => api.queueTop(
                        g.hash ? { hash: g.hash } : { kind: g.kind, key: g.key }));
                      refresh();
                    }}>⤒</Act>
                  {g.hash && <Act cls="btn sec small"
                    title="Drop this download, blocklist the release and search for another"
                    run={async () => {
                      if (!confirm(
                        `Drop this download and look for a different release?\n\n${a.title}`
                        + `${g.count > 1 ? ` (${g.count} episodes)` : ""}\n\n`
                        + `The torrent and its files are deleted, the release is blocklisted, `
                        + `and the records go back to searching. Your library files are untouched.`
                      )) return;
                      await runAction(() => api.cancelDownload(g.hash!));
                      refresh();
                    }}>⛔</Act>}
                </div>
              </div>
            </div>
          );
        })}

        {unattached > 0 && <div className="sub" style={{ marginTop: 8 }}>
          {unattached} other torrent{unattached === 1 ? "" : "s"} in vo-merge's qB categories
          {unattached === 1 ? " is" : " are"} not attached to a downloading record — usually a
          finished donor waiting its turn in the merge queue below.
        </div>}
      </div>

      {/* --------------------------------------------------------- merge queue */}
      <div className="panel">
        <div className="row panel-head">
          <b className="h2">⏳ Waiting to merge</b>
          {q && <span className="muted">
            {fmtNum(q.total)} waiting · {q.merging_now} merging
          </span>}
          <div className="spacer" />
          {q && <span className={"badge" + (q.workers === 0 ? " bad" : "")}
            title="merges running now / merge worker threads alive">
            {q.merging_now} / {q.workers} worker{q.workers === 1 ? "" : "s"}
          </span>}
        </div>

        {/* Why the queue is or is not draining. A deliberate hold (paused, scanning, low disk) and
            a dead worker thread used to look identical from the outside — both are simply "nothing
            is merging" — so each one says its own name here. */}
        {q && (q.hold || q.workers === 0 || q.disk.low) && (
          <div className="panel warnbar" style={{ marginBottom: 10, display: "block" }}>
            <div>
              ⏸ Nothing new will start merging — <b>{q.hold ?? (q.workers === 0
                ? "no merge worker is running" : "low disk")}</b>.
              {q.hold === "paused" && <> Work already in flight finishes; aborting a mux mid-write
                would leave a corrupt library file. Un-pause from the header.</>}
              {q.hold === "scanning" && <> A library re-read is running — merges resume when it
                ends.</>}
            </div>
            {q.workers === 0 && <div className="sub warn">
              The merge worker thread is not alive. The 3-minute stall sweep respawns it, so this
              should clear itself; if it does not, restart the container.
            </div>}
            {q.disk.low && <div className="sub warn">
              Free space is {q.disk.free_gb == null ? "unknown" : `${q.disk.free_gb.toFixed(0)} GB`},
              at or below the configured floor — merging is held rather than risking a half-written
              output that makes the disk-full worse.
            </div>}
            {q.disk.paths && <div className="sub muted">
              {Object.entries(q.disk.paths)
                .map(([p, gb]) => `${p} ${gb == null ? "?" : gb.toFixed(0)} GB free`)
                .join(" · ")}
            </div>}
          </div>
        )}

        {q && q.items.length === 0 && <Empty>
          Nothing is queued to merge. A download that reaches 100% lands here automatically and is
          drained one item at a time, in priority then arrival order.
        </Empty>}

        {q?.items.map(it => (
          <div className="qrow" key={`${it.kind}${it.key}`}>
            <span className="qpos">{it.pos}</span>
            <Poster src={it.poster} alt={it.title} sm />
            <div className="qrow-main">
              <div className="qrow-title">
                {it.priority > 0 && <span className="prio" title="prioritised — jumps the queue">★</span>}
                {it.title}
              </div>
              <div className="sub">
                {it.merging ? "merging now" : `⏳ queued ${fmtAgo(q.now - it.waiting_s, q.now)}`}
                {it.sub && <> · {it.sub}</>}
              </div>
            </div>
            <div className="qrow-end">
              {it.merging && <Pill s="merging" />}
              <div className="row tight">
                {!it.merging && <Act cls="btn sec small" title="Move to the front of the queue"
                  run={async () => {
                    await runAction(() => api.queueTop({ kind: it.kind, key: it.key }));
                    refresh();
                  }}>⤒</Act>}
                <Act cls="btn sec small"
                  title={it.merging
                    ? "Kill the decode/mux running right now (the record parks as review)"
                    : "Take it off the queue (the record parks as review)"}
                  run={async () => {
                    await runAction(() => it.kind === "movie"
                      ? api.abortMovie(Number(it.key)) : api.abortEpisode(it.key));
                    refresh();
                  }}>⛔</Act>
              </div>
            </div>
          </div>
        ))}

        {q && q.total > q.items.length && <div className="row panel-foot">
          <span className="muted">showing the first {q.items.length} of {fmtNum(q.total)}</span>
        </div>}
      </div>

      {/* ------------------------------------------------- searching / pending */}
      <div className="panel">
        <div className="row panel-head">
          <b className="h2">🔍 Searching &amp; pending</b>
          <span className="muted">what the next sweep will look at</span>
          <div className="spacer" />
          <Act cls="btn" title="Run a search sweep now instead of waiting for the interval"
            run={async () => {
              await runAction(async () => {
                const r = await api.searchAll();
                setSearchMsg(r.started
                  ? `Sweep started — ${fmtNum(r.pending ?? 0)} pending, `
                    + (r.slots == null
                      ? "and qB could not be reached so the free-slot count is unknown."
                      : `${r.slots} download slot${r.slots === 1 ? "" : "s"} free.`)
                  : r.note || "Nothing started.");
              });
              refresh();
            }}>Search now</Act>
        </div>

        <div className="chips">
          {PIPE_COUNTS.map(([key, label, why]) => {
            const films = st?.counts?.[key] ?? 0;
            const eps = tv?.counts?.[key] ?? 0;
            return (
              <span className="chip" key={key} title={why}>
                <b>{fmtNum(films + eps)}</b> {label}
                <span className="muted"> · {fmtNum(films)} film{films === 1 ? "" : "s"},
                  {" "}{fmtNum(eps)} episode{eps === 1 ? "" : "s"}</span>
              </span>
            );
          })}
        </div>

        {st?.hold && <div className="sub warn" style={{ marginTop: 8 }}>
          No new searches are starting — {st.hold}.
        </div>}
        {searchMsg && <div className="sub" style={{ marginTop: 8 }}>{searchMsg}</div>}
        {st && !st.enabled && <div className="sub bad" style={{ marginTop: 8 }}>
          The pipeline is disabled in Settings — nothing will be searched, grabbed or merged.
        </div>}
      </div>
    </>
  );
}

const PIPE_COUNTS: [string, string, string][] = [
  ["pending", "pending", "scanned, gap recorded, waiting for a search"],
  ["searching", "searching", "a Prowlarr query is running for these right now"],
  ["grabbed", "grabbed", "handed to qB, waiting for it to report the torrent"],
  ["no_release", "no release", "nothing suitable existed when we last looked; retried after the cooldown"],
];

/* ================================================================== history */

const PAGE = 100;
// The states worth filtering to. Every transition is logged, but `pending` and `grabbed` are
// plumbing — a list of them buries the four outcomes anyone actually goes looking for.
const STATUS_OPTS = ["merged", "error", "sync_fail", "review", "no_release", "downloading", "ignored"];

function HistoryTab() {
  const route = useRoute();
  const kind = route.query.get("kind") ?? "";
  const status = route.query.get("status") ?? "";
  const urlQ = route.query.get("q") ?? "";
  const offset = Math.max(0, Number(route.query.get("off") ?? 0) || 0);

  const [page, setPage] = useState<EventPage | null>(null);
  const [text, setText] = useState(urlQ);

  // The URL is the source of truth (rule: any filter worth sharing is in the link), so the box
  // follows it when it changes underneath — a Back press, or someone else's shared link.
  useEffect(() => { setText(urlQ); }, [urlQ]);
  useEffect(() => {
    if (text === urlQ) return;
    const t = setTimeout(() => { setParam("off", null); setParam("q", text || null); }, 300);
    return () => clearTimeout(t);
  }, [text, urlQ]);

  usePoll(() => api.events({
    limit: PAGE, offset,
    kind: kind || undefined, status: status || undefined, q: urlQ || undefined,
  }).then(setPage), 20000, [kind, status, urlQ, offset]);

  const setFilter = (k: string, v: string | null) => { setParam("off", null); setParam(k, v); };
  const sel = status ? status.split(",") : [];
  const filtered = !!(kind || status || urlQ);
  const total = page?.total ?? 0;
  const shown = page?.items.length ?? 0;

  return (
    <>
      <div className="row toolbar">
        <select value={kind} onChange={e => setFilter("kind", e.target.value || null)}
          aria-label="Record kind">
          <option value="">All records</option>
          <option value="movie">Films</option>
          <option value="episode">Episodes</option>
        </select>

        <select multiple size={3} value={sel} aria-label="Filter by outcome"
          title="Ctrl/⌘-click to pick more than one; nothing selected means every transition"
          onChange={e => setFilter("status",
            Array.from(e.target.selectedOptions, o => o.value).join(",") || null)}>
          {STATUS_OPTS.map(s =>
            <option key={s} value={s}>{STATE_LABEL[s] ?? s.replace("_", " ")}</option>)}
        </select>

        <input className="search" type="search" placeholder="Search title or detail…"
          value={text} onChange={e => setText(e.target.value)} aria-label="Search history" />

        {filtered && <button className="btn ghost" onClick={() => {
          setParam("off", null); setParam("kind", null);
          setParam("status", null); setParam("q", null);
        }}>Clear filters</button>}

        <div className="spacer" />
        <span className="muted">{fmtNum(total)} transition{total === 1 ? "" : "s"}</span>
      </div>

      <div className="panel">
        <div className="scroll-x">
          <table>
            <thead>
              <tr>
                <th style={{ width: 110 }}>When</th>
                <th>Title</th>
                <th style={{ width: 260 }}>Change</th>
                <th>Detail</th>
              </tr>
            </thead>
            <tbody>
              {page?.items.map(e => <EventLine key={e.id} e={e} now={page.now} />)}
            </tbody>
          </table>
        </div>

        {page && shown === 0 && (filtered
          ? <Empty>
              No transition matches those filters. The log keeps every state change for{" "}
              <code>history_keep_days</code> (90 by default), so an old one may simply have been
              pruned.
            </Empty>
          : <Empty>
              Nothing here yet. History starts at the app's first recorded state transition — a
              fresh install is legitimately empty, and it fills on its own as titles are scanned,
              searched, grabbed and merged. Nothing needs doing.
            </Empty>)}

        {total > PAGE && <div className="row panel-foot">
          <span className="muted">
            {fmtNum(offset + 1)}–{fmtNum(offset + shown)} of {fmtNum(total)}
          </span>
          <div className="spacer" />
          <button className="btn sec" disabled={offset <= 0}
            onClick={() => setParam("off", offset - PAGE <= 0 ? null : String(offset - PAGE))}>
            ← Prev
          </button>
          <button className="btn sec" disabled={offset + PAGE >= total}
            onClick={() => setParam("off", String(offset + PAGE))}>
            Next →
          </button>
        </div>}
      </div>
    </>
  );
}

function EventLine({ e, now }: { e: EventRow; now: number }) {
  return (
    <tr>
      <td className="nowrap" title={new Date(e.ts * 1000).toLocaleString()}>{fmtAgo(e.ts, now)}</td>
      <td>
        <div>
          <span className="muted" title={e.kind}>{e.kind === "movie" ? "🎬" : "📺"} </span>
          {e.title || <span className="muted">(record since deleted)</span>}
        </div>
        {e.sub && <div className="sub">{e.sub}</div>}
      </td>
      <td className="nowrap">
        {e.frm ? <Pill s={e.frm} /> : <span className="muted">new</span>}
        <span className="muted"> → </span>
        <Pill s={e.sts} />
        {e.sts === "merged" && <MergeTag tag={e.tag} />}
      </td>
      <td><span className="sub">{e.detail || "—"}</span></td>
    </tr>
  );
}

/** `merged` is the terminal state for three different outcomes and only two of them are work this
 *  app did — `already` means a scan found the file was correct on its own. Rendering the three
 *  identically is how a library re-read once read as thousands of merges. */
function MergeTag({ tag }: { tag: string | null }) {
  if (!tag) return null;
  if (tag === "already")
    return <span className="badge" style={{ marginLeft: 8 }}
      title="the file already met its language profile — the scan just closed the record out, vo-merge changed nothing">
      already correct
    </span>;
  if (tag === "replaced")
    return <span className="lang-badge repl"
      title="the download's video was at least as good as the library file's, so it became the library file">
      replaced
    </span>;
  return <span className="lang-badge" title="tracks were muxed into the existing library file">
    {tag}
  </span>;
}
