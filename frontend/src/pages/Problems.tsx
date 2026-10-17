import { useEffect, useMemo, useState } from "react";
import {
  api, type BulkState, type EventRow, type Movie, type ProblemGroup, type ProblemPage,
  type ProblemRecord, type ProblemsView, type Remedy,
} from "../api";
import { runAction, usePoll } from "../lib/poll";
import { go, setParam, useRoute } from "../lib/router";
import { fmtAgo, fmtNum, recKey } from "../lib/format";
import {
  Act, AiPill, aiUnfixed, DriftBadge, Empty, LiveDot, Modal, Pill, Poster, RowMenu,
} from "../components/ui";
import { LipSyncModal, ReleaseModal, SyncEditor } from "../components/modals";

/* ======================================================================
   Problems — failures grouped by CAUSE, each with the remedy that fixes it.

   The old Review tab listed failures one per row, newest first, with the raw error string and
   four generic buttons. That is fine for five failures and useless for four hundred: one bad
   season pack writes the same sentence forty times, and the decision that actually empties the
   backlog — "this whole group is a PAL transfer, apply the ratio" — is invisible because nothing
   ever puts the forty rows next to each other.

   Everything shown here is computed by backend/app/problems.py: the taxonomy, the per-cause
   remedies, and whether a remedy may be applied to a whole group (`bulk`) or needs a decision per
   record. The page renders that and never second-guesses it — a rule added on the server appears
   here with no frontend change.
   ====================================================================== */

export default function Problems({ code }: { code?: string }) {
  return code ? <Drill code={code} /> : <Groups />;
}

const SEV_LABEL: Record<string, string> = {
  fixable: "mechanical fix", judgement: "your call", external: "outside vo-merge",
};
const SEV_HELP: Record<string, string> = {
  fixable: "a remedy exists that fixes this without a judgement call",
  judgement: "someone has to choose — which release, which mapping, or whether to give up",
  external: "the cause is outside vo-merge: a mount, the disk, an indexer, or a broken file",
};

/* ---------------------------------------------------------------- the bulk pass
   A remedy that probes files is minutes of work on a big group, so the server runs it in a thread
   and exposes one global progress record. One pass at a time, by construction: two passes over the
   same records would race each other's writes. */

function useBulk(onDone: () => void) {
  const [state, setState] = useState<BulkState | null>(null);
  const [track, setTrack] = useState(false);
  const [note, setNote] = useState("");

  usePoll(() => {
    if (!track) return;
    return api.problemActState().then(s => {
      setState(s);
      // The pass has ended: stop the fast poll and re-read the list, because the whole point is
      // that most of these records have just left the group.
      if (!s.running) { setTrack(false); onDone(); }
    });
  }, 1500, [track]);

  // The progress record lives on the SERVER, so a pass started from another tab (or before this
  // page was opened) is still this page's business — it is the reason the buttons are disabled.
  useEffect(() => {
    api.problemActState()
      .then(s => { if (s.running) { setState(s); setTrack(true); } })
      .catch(() => { /* the poll below reports anything persistent */ });
  }, []);

  const run = async (r: Remedy, target: { code?: string; keys?: string[] }, n: number) => {
    const params = askParams(r, n);
    if (!params) return;
    await runAction(async () => {
      const res = await api.problemAct({ action: r.code, ...target, ...params });
      if (res.started) { setNote(""); setState(null); setTrack(true); }
      else setNote(res.note || "nothing was started");
    });
  };

  return { state, running: track, note, run };
}

/** Confirmation and parameters, per remedy. `unfixable` records a REASON — an ignored record with
 *  no reason reads as an unexamined skip, which is exactly what the state is meant not to be. */
function askParams(r: Remedy, n: number): { reason?: string } | null {
  const many = `${n} record${n === 1 ? "" : "s"}`;
  if (r.code === "unfixable") {
    const reason = window.prompt(
      `Give up on ${many} — why?\n\nThe reason is recorded on each record, so it doesn't read as `
      + `an unexamined skip. Ignored titles are still re-examined periodically.`, "");
    return reason && reason.trim() ? { reason: reason.trim() } : null;
  }
  const slow = r.slow
    ? "\n\nThis one decodes video: expect minutes per record. It runs in the background — you can "
      + "leave the page."
    : "";
  if (n > 1 && !window.confirm(`${r.label} — apply to ${many}?\n\n${r.note}${slow}`)) return null;
  return {};
}

function BulkPanel({ s, running, note, specs }:
  { s: BulkState | null; running: boolean; note: string; specs: Record<string, Remedy> }) {
  if (note) return <div className="panel warnbar">⏳ {note}</div>;
  if (!s || (!running && !s.total)) return null;
  const spec = specs[s.action];
  const pct = s.total ? Math.round((s.done / s.total) * 100) : 0;
  return (
    <div className="panel">
      <div className="row panel-head">
        <b className="h2">{running ? "Applying" : "Finished"}: {spec?.label ?? s.action}</b>
        <span className="muted">
          {fmtNum(s.done)} of {fmtNum(s.total)} · {fmtNum(s.ok)} ok · {fmtNum(s.failed)} failed
        </span>
        <div className="spacer" />
        {running && <LiveDot />}
      </div>

      {running && spec?.slow && <div className="sub warn" style={{ marginBottom: 6 }}>
        This remedy decodes video — expect minutes per record. It keeps running if you navigate
        away; nothing is lost.
      </div>}

      <div className="dlbar-track">
        <div className={"dlbar-fill" + (running ? " live" : "")} style={{ width: pct + "%" }} />
      </div>
      {s.error && <div className="err" style={{ marginTop: 8 }}>{s.error}</div>}

      {s.results.length > 0 && (
        <div className="scroll-y short" style={{ marginTop: 8 }}>
          {s.results.map((x, i) => (
            <div className="dashrow" key={`${x.kind}:${x.key}:${i}`}>
              <span className={x.ok ? "ok" : "bad"}>{x.ok ? "✓" : "✕"}</span>
              <div className="dashrow-main">
                <div className="dashrow-title">
                  {x.title ?? x.key}
                  {x.sub && <span className="muted"> · {x.sub}</span>}
                </div>
                <div className={"sub" + (x.ok ? "" : " bad")}>{x.message}</div>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

const specsOf = (rs: Remedy[]): Record<string, Remedy> =>
  Object.fromEntries(rs.map(r => [r.code, r]));

/* ---------------------------------------------------------------- group view */

function Groups() {
  const [view, setView] = useState<ProblemsView | null>(null);
  const load = () => api.problems().then(setView);
  const bulk = useBulk(() => { void runAction(load); });
  // Faster while a pass is running: records leave their group as it works, and at 20s a page that
  // IS emptying looks frozen.
  usePoll(load, bulk.running ? 5000 : 20000, [bulk.running]);

  const specs = useMemo(
    () => specsOf((view?.groups ?? []).flatMap(g => g.remedies)), [view]);
  const bySev = (s: string) =>
    (view?.groups ?? []).filter(g => g.severity === s).reduce((n, g) => n + g.count, 0);

  return (
    <>
      <div className="row toolbar">
        <b className="h2">
          {view ? `${fmtNum(view.total)} record${view.total === 1 ? "" : "s"} need attention`
                : "Reading the failures…"}
        </b>
        {view && view.total > 0 && (
          <div className="chips">
            {(["fixable", "judgement", "external"] as const).map(s => bySev(s) > 0 && (
              <span className="chip" key={s} title={SEV_HELP[s]}>
                <b>{fmtNum(bySev(s))}</b> {SEV_LABEL[s]}
              </span>
            ))}
          </div>
        )}
        <div className="spacer" />
        <LiveDot />
      </div>

      <BulkPanel s={bulk.state} running={bulk.running} note={bulk.note} specs={specs} />

      {view && view.groups.length === 0 && <div className="panel"><Empty>
        <b>All clear 🎉</b><br />
        Nothing is in <code>error</code>, <code>sync_fail</code>, <code>review</code> or{" "}
        <code>no_release</code>. Failures land here within minutes of happening — the escalation
        ladder retries what a retry fixes and pages the on-call AI before anything reaches this
        page, so an empty list means the machine is coping.{" "}
        <a href="#/activity">Watch the queue</a> to see what it is doing instead.
      </Empty></div>}

      {/* Order comes from the API — fixable first, then by size. Burying a one-click group under
          a big judgement bucket is how a backlog stops looking actionable. */}
      <div className="probgrid">
        {view?.groups.map(g =>
          <GroupCard key={g.code} g={g} bulk={bulk} now={view.now} />)}
      </div>
    </>
  );
}

function GroupCard({ g, bulk, now }:
  { g: ProblemGroup; bulk: ReturnType<typeof useBulk>; now: number }) {
  const madeOf = [
    g.movies > 0 && `${fmtNum(g.movies)} film${g.movies === 1 ? "" : "s"}`,
    g.episodes > 0 && `${fmtNum(g.episodes)} episode${g.episodes === 1 ? "" : "s"}`
      + (g.shows > 0 ? ` across ${fmtNum(g.shows)} show${g.shows === 1 ? "" : "s"}` : ""),
  ].filter(Boolean).join(" · ");

  return (
    <div className={"probcard " + g.severity}>
      <div className="probhead">
        <span className="n">{fmtNum(g.count)}</span>
        <span className="lbl">{g.label}</span>
        <span className={"probsev " + g.severity} title={SEV_HELP[g.severity]}>
          {SEV_LABEL[g.severity]}
        </span>
      </div>

      <div className="sub">
        {madeOf}
        {g.with_ai > 0 && <> · <b className="warn">{fmtNum(g.with_ai)}</b> the AI couldn’t fix</>}
        {g.newest != null && <> · newest {fmtAgo(g.newest, now)}</>}
      </div>

      <div className="prob-why">{g.why}</div>

      {/* The single biggest win on this page: the ratio is already in the message, put there by
          `_sync_fail_reason` for the on-call AI. Applying it is arithmetic, so a whole PAL season
          is one click rather than a research task. */}
      {g.drifts.length > 0 && (
        <div className="prob-fix">
          ⏩ The pipeline <b>already computed the rate</b>{" "}
          {g.drifts.map(d => <span className="drift" key={d}>×{d.toFixed(7)}</span>)}
          {" "}— applying it is arithmetic, not a guess.
        </div>
      )}

      <div className="prob-fix"><b>Fix: </b>{g.fix}</div>

      {g.sample_error && (
        <div className="prob-sample" title="the literal message the pipeline wrote on one of these records">
          {g.sample_error}
        </div>
      )}

      <div className="prob-acts">
        {g.remedies.map((r, i) => r.bulk
          ? <Act key={r.code} cls={i === 0 ? "btn" : "btn sec"} disabled={bulk.running}
              busyLabel="starting…"
              title={r.note + (r.slow ? " Decodes video — minutes per record." : "")
                + `\n\nApplies to all ${g.count} record(s) in this group.`}
              run={() => bulk.run(r, { code: g.code }, g.count)}>
              {r.label}{r.slow && " ⏳"}
            </Act>
          : <button key={r.code} className="btn sec" disabled
              title={`${r.note}\n\nThis one needs a decision per record, so it can't be applied to `
                + `a group — open the records and use the row menu.`}>
              {r.label}
            </button>)}
        <button className="btn ghost" onClick={() => go("problems/" + g.code)}>
          Open {fmtNum(g.count)} record{g.count === 1 ? "" : "s"} →
        </button>
      </div>
    </div>
  );
}

/* ---------------------------------------------------------------- drill-down */

const PAGE = 200;

type RowModal =
  | { what: "release" | "lips" | "timeline" | "assign" | "tune"; rec: ProblemRecord }
  | null;

function Drill({ code }: { code: string }) {
  const route = useRoute();
  const offset = Math.max(0, Number(route.query.get("off") ?? 0) || 0);
  const urlQ = route.query.get("q") ?? "";
  const [page, setPage] = useState<ProblemPage | null>(null);
  const [sel, setSel] = useState<Set<string>>(new Set());
  const [text, setText] = useState(urlQ);
  const [modal, setModal] = useState<RowModal>(null);

  const load = () => api.problemRecords(code, PAGE, offset).then(setPage);
  const bulk = useBulk(() => { setSel(new Set()); void runAction(load); });
  usePoll(load, bulk.running ? 5000 : 20000, [code, offset, bulk.running]);

  // The URL is the source of truth for the filter, so the box follows it when it changes
  // underneath — a Back press, or someone else's shared link.
  useEffect(() => { setText(urlQ); }, [urlQ]);
  useEffect(() => {
    if (text === urlQ) return;
    const t = setTimeout(() => { setParam("off", null); setParam("q", text || null); }, 300);
    return () => clearTimeout(t);
  }, [text, urlQ]);

  const rule = page?.rule ?? null;
  const specs = useMemo(() => specsOf(rule?.remedies ?? []), [rule]);
  const items = page?.items ?? [];
  const shown = useMemo(() => {
    const q = urlQ.trim().toLowerCase();
    if (!q) return items;
    return items.filter(r => `${r.title ?? ""} ${r.sub ?? ""} ${r.error ?? ""}`
      .toLowerCase().includes(q));
  }, [items, urlQ]);

  // Only ever act on records that are on screen: a selection kept across a reload could name
  // records that have since been fixed and left the group.
  const picked = shown.filter(r => sel.has(recKey(r.kind, r.key)));
  const allPicked = shown.length > 0 && picked.length === shown.length;
  const toggle = (k: string) => {
    const next = new Set(sel);
    next.has(k) ? next.delete(k) : next.add(k);
    setSel(next);
  };

  return (
    <>
      <div className="row toolbar">
        <button className="btn ghost" onClick={() => go("problems")}>← All problems</button>
        <b className="h2">{rule?.label ?? code}</b>
        {rule && <span className={"probsev " + rule.severity} title={SEV_HELP[rule.severity]}>
          {SEV_LABEL[rule.severity]}
        </span>}
        <input className="search" type="search" placeholder="Filter these records…"
          value={text} onChange={e => setText(e.target.value)}
          aria-label="Filter the records in this group" />
        <div className="spacer" />
        <span className="muted">
          {fmtNum(page?.total ?? 0)} record{(page?.total ?? 0) === 1 ? "" : "s"}
          {urlQ && <> · {fmtNum(shown.length)} match the filter</>}
        </span>
        <LiveDot />
      </div>

      {rule && (
        <div className={"probcard " + rule.severity} style={{ marginBottom: 14 }}>
          <div className="prob-why">{rule.why}</div>
          <div className="prob-fix"><b>Fix: </b>{rule.fix}</div>
          <div className="sub">
            Tick rows to apply a remedy to just those, or use a row’s <b>Actions</b> menu — the
            per-record tools (map a donor file, search releases, read the lips, the timeline) live
            there.
          </div>
        </div>
      )}

      <BulkPanel s={bulk.state} running={bulk.running} note={bulk.note} specs={specs} />

      {/* Bulk bar: only ever the SELECTION. The API deliberately lets explicit keys win over the
          group code, so a partial selection can never be widened to the whole group. */}
      {picked.length > 0 && rule && (
        <div className="bulkbar">
          <b>{fmtNum(picked.length)} selected</b>
          <button className="btn ghost small" onClick={() => setSel(new Set())}>clear</button>
          {rule.remedies.filter(r => r.bulk).map(r => (
            <Act key={r.code} cls="btn sec" disabled={bulk.running} busyLabel="starting…"
              title={r.note + (r.slow ? " Decodes video — minutes per record." : "")}
              run={() => bulk.run(r, { keys: picked.map(x => recKey(x.kind, x.key)) },
                                  picked.length)}>
              {r.label}{r.slow && " ⏳"}
            </Act>
          ))}
          <div className="spacer" />
          <span className="sub">applies to the {fmtNum(picked.length)} ticked, nothing else</span>
        </div>
      )}

      <div className="panel">
        <div className="scroll-x">
          <table>
            <thead>
              <tr>
                <th className="pick">
                  <input type="checkbox" checked={allPicked} disabled={shown.length === 0}
                    aria-label="Select every record shown"
                    ref={el => { if (el) el.indeterminate = picked.length > 0 && !allPicked; }}
                    onChange={() => setSel(allPicked ? new Set()
                      : new Set(shown.map(r => recKey(r.kind, r.key))))} />
                </th>
                <th>Title</th>
                <th>Reason</th>
                <th style={{ width: 90 }}>Age</th>
                <th style={{ width: 120 }} />
              </tr>
            </thead>
            <tbody>
              {shown.map(r => (
                <Row key={recKey(r.kind, r.key)} r={r} now={page?.now ?? 0} rule={rule}
                  bulk={bulk} picked={sel.has(recKey(r.kind, r.key))}
                  onPick={() => toggle(recKey(r.kind, r.key))}
                  onModal={what => setModal({ what, rec: r })} />
              ))}
            </tbody>
          </table>
        </div>

        {page && shown.length === 0 && (urlQ
          ? <Empty>
              Nothing in this group matches “{urlQ}”.{" "}
              <button className="btn ghost small" onClick={() => setText("")}>Clear the filter</button>
            </Empty>
          : <Empty>
              <b>Nothing left in this group.</b> Every record that was here has been fixed or has
              moved on to another state — a remedy that works removes rows from under you.{" "}
              <button className="btn sec" onClick={() => go("problems")}>Back to all problems</button>
            </Empty>)}

        {page && page.total > PAGE && (
          <div className="row panel-foot">
            <span className="muted">
              {fmtNum(offset + 1)}–{fmtNum(offset + items.length)} of {fmtNum(page.total)}
            </span>
            <div className="spacer" />
            <button className="btn sec" disabled={offset <= 0}
              onClick={() => setParam("off", offset - PAGE <= 0 ? null : String(offset - PAGE))}>
              ← Prev
            </button>
            <button className="btn sec" disabled={offset + PAGE >= page.total}
              onClick={() => setParam("off", String(offset + PAGE))}>Next →</button>
          </div>
        )}
      </div>

      {modal?.what === "release" && <ReleaseModal
        title={`${modal.rec.title ?? modal.rec.key}${modal.rec.sub ? ` · ${modal.rec.sub}` : ""}`}
        load={() => modal.rec.kind === "movie"
          ? api.candidates(Number(modal.rec.key)) : api.episodeCandidates(modal.rec.key)}
        onGrab={c => modal.rec.kind === "movie"
          ? api.grab(Number(modal.rec.key), c.link, c.rid, c.title)
          : api.episodeGrab(modal.rec.key, c.link, c.rid, c.title)}
        onClose={() => setModal(null)} onGrabbed={() => { void runAction(load); }} />}

      {modal?.what === "lips" && <LipSyncModal
        kind={modal.rec.kind} id={modal.rec.key} title={modal.rec.title ?? modal.rec.key}
        onClose={() => setModal(null)} onApplied={() => { void runAction(load); }} />}

      {modal?.what === "timeline" &&
        <TimelineModal rec={modal.rec} onClose={() => setModal(null)} />}

      {/* The tuner wants a Movie, and this page holds the flat problem record — so re-read the
          real one rather than faking the shape. It only ever opens for `kind === "movie"`. */}
      {modal?.what === "tune" && <TuneGate id={Number(modal.rec.key)}
        onClose={() => { setModal(null); void runAction(load); }} />}

      {modal?.what === "assign" && <AssignModal rec={modal.rec}
        onClose={() => setModal(null)} onDone={() => { void runAction(load); }} />}
    </>
  );
}

function Row({ r, now, rule, bulk, picked, onPick, onModal }: {
  r: ProblemRecord; now: number; rule: ProblemGroup | null;
  bulk: ReturnType<typeof useBulk>; picked: boolean; onPick: () => void;
  onModal: (what: "release" | "lips" | "timeline" | "assign" | "tune") => void;
}) {
  const key = recKey(r.kind, r.key);
  const remedies = rule?.remedies ?? [];
  // `search` already opens the release list from the remedy row; offering it twice in one menu is
  // just noise.
  const hasSearch = remedies.some(x => x.code === "search");

  return (
    <tr className={(picked ? "sel " : "") + (aiUnfixed(r.ai_status) ? "needshuman" : "")}>
      <td className="pick">
        <input type="checkbox" checked={picked} onChange={onPick}
          aria-label={`Select ${r.title ?? r.key}`} />
      </td>

      <td>
        <div className="titlecell">
          <Poster src={r.poster} alt={r.title ?? "record"} sm />
          <div>
            <div>
              {(r.priority ?? 0) > 0 && <span className="prio" title="prioritised">★</span>}
              {r.title ?? r.key}
            </div>
            {r.sub && <div className="sub">{r.sub}</div>}
            <div className="row tight" style={{ marginTop: 4 }}>
              <Pill s={r.status} /><AiPill s={r.ai_status} />
            </div>
          </div>
        </div>
      </td>

      <td>
        <div className="sub bad clamp2" title={r.error ?? ""}>{r.error || "—"}</div>
        <DriftBadge d={r.drift} />
        {r.ai_verdict && <div className="sub clamp2" title={r.ai_verdict}>🤖 {r.ai_verdict}</div>}
        {!r.has_donor && <div className="sub muted">no donor on disk</div>}
      </td>

      <td className="nowrap sub"
        title={r.updated ? new Date(r.updated * 1000).toLocaleString() : ""}>
        {fmtAgo(r.updated, now)}
      </td>

      <td>
        <div className="row">
          <RowMenu>
            {remedies.map(x => x.code === "search"
              ? <button className="btn sec" key={x.code} title={x.note}
                  onClick={() => onModal("release")}>{x.label}</button>
              : x.code === "assign"
                ? (r.kind === "episode" && <button className="btn sec" key={x.code} title={x.note}
                    onClick={() => onModal("assign")}>{x.label}</button>)
                : <Act key={x.code} cls="btn sec" disabled={bulk.running} busyLabel="starting…"
                    title={x.note + (x.slow ? " Decodes video — minutes." : "")}
                    run={() => bulk.run(x, { keys: [key] }, 1)}>
                    {x.label}{x.slow && " ⏳"}
                  </Act>)}

            <div className="sub" style={{ padding: "6px 8px 2px" }}>Always</div>
            <button className="btn sec" onClick={() => onModal("lips")}
              title={"Correlate mouth movement in the picture against the speech in each audio "
                + "track — the only measurement here that doesn't depend on the two files "
                + "agreeing with each other."}>
              Read the lips…
            </button>
            {!hasSearch && <button className="btn sec" onClick={() => onModal("release")}
              title="Run an indexer query for this title and grab a release by hand">
              Search releases…
            </button>}
            {/* The manual last resort, and the only one a person does better than the machine:
                play the film and slide the offset until it looks right. Films only — the tuner
                streams a preview clip, which exists for movies. */}
            {r.kind === "movie" && <button className="btn sec" onClick={() => onModal("tune")}
              title="Play a scene and slide the offset until it lands, hearing the change live">
              Tune sync by hand…
            </button>}
            <button className="btn sec" onClick={() => onModal("timeline")}
              title="Every state this record has been through — what makes a parked failure legible">
              Timeline
            </button>
          </RowMenu>
        </div>
      </td>
    </tr>
  );
}

/* ---------------------------------------------------------------- per-record modals */

/** One record's own history. "Grabbed three releases, each rejected for a different reason" is a
 *  different problem from "grabbed once, sync failed once", and the error string alone never says
 *  which of the two you are looking at. */
function TimelineModal({ rec, onClose }: { rec: ProblemRecord; onClose: () => void }) {
  const [d, setD] = useState<{ items: EventRow[]; now: number } | null>(null);
  const [err, setErr] = useState("");
  useEffect(() => {
    let alive = true;
    api.timeline(rec.kind, rec.key)
      .then(x => { if (alive) setD(x); })
      .catch(e => { if (alive) setErr(e.message || "could not read the timeline"); });
    return () => { alive = false; };
  }, [rec.kind, rec.key]);

  return (
    <Modal title={`Timeline — ${rec.title ?? rec.key}${rec.sub ? ` · ${rec.sub}` : ""}`}
      onClose={onClose}>
      {err && <div className="err">{err}</div>}
      {!d && !err && <div className="muted">reading…</div>}
      {d && d.items.length === 0 && <Empty>
        No transitions recorded for this record. History starts at the app's first logged state
        change, so a record that has not moved since then is legitimately empty.
      </Empty>}
      <div className="scroll-y tall">
        {d?.items.map(e => (
          <div className="attn" key={e.id}>
            <div className="attn-head">
              {e.frm ? <Pill s={e.frm} /> : <span className="muted">new</span>}
              <span className="muted">→</span>
              <Pill s={e.sts} />
              {e.tag && <span className="lang-badge">{e.tag}</span>}
              <div className="spacer" />
              <span className="sub nowrap" title={new Date(e.ts * 1000).toLocaleString()}>
                {fmtAgo(e.ts, d.now)}
              </span>
            </div>
            {e.detail && <div className="sub">{e.detail}</div>}
          </div>
        ))}
      </div>
    </Modal>
  );
}

/** Map one donor file to one episode.
 *
 *  Comparing the two lists IS the diagnosis: the pack's files carry aired numbering (S04E15) while
 *  the library records are flattened absolute (S01E51), and when Sonarr has no absolute numbers —
 *  or the pack numbers its files a third way — the automatic translation has nothing to work from.
 *  Everything shown here comes from `/context`, the same call the on-call AI is told to make. */
function AssignModal({ rec, onClose, onDone }:
  { rec: ProblemRecord; onClose: () => void; onDone: () => void }) {
  const [ctx, setCtx] = useState<any>(null);
  const [err, setErr] = useState("");
  const [msg, setMsg] = useState("");
  const [target, setTarget] = useState(rec.key);

  useEffect(() => {
    let alive = true;
    api.context(rec.kind, rec.key)
      .then(c => { if (alive) setCtx(c); })
      .catch(e => { if (alive) setErr(e.message || "could not read the record's context"); });
    return () => { alive = false; };
  }, [rec.kind, rec.key]);

  const files: any[] = ctx?.donor_files ?? [];
  const eps: any[] = ctx?.series_episodes ?? [];
  const num = ctx?.numbering;

  return (
    <Modal title={`Map a donor file — ${rec.title ?? rec.key}`} onClose={onClose} wide>
      <p className="muted" style={{ marginTop: 0 }}>
        The left column is what the download actually holds, with the season/episode read off each
        file name. The right column is what the library has records for. Where the two disagree,
        pick the target episode and assign the file that really contains it — the merge is queued
        immediately.
      </p>

      {err && <div className="err">{err}</div>}
      {!ctx && !err && <div className="muted">reading the record's context…</div>}

      {num && <div className="prob-sample">
        library S{num.library_season}E{num.library_episode} · release S{num.release_season}E
        {num.release_episode}
        {num.absolute != null && <> · absolute {num.absolute}</>}
        {num.translated
          ? " · the aired↔absolute translation DID fire, and still didn't match"
          : " · no translation applied"}
      </div>}

      {ctx && (
        <>
          <div className="row" style={{ margin: "10px 0 6px" }}>
            <b>Assign to:</b>
            <select value={target} onChange={e => setTarget(e.target.value)}
              aria-label="Episode the file will be assigned to" style={{ width: "auto" }}>
              {eps.length === 0 && <option value={rec.key}>this record</option>}
              {eps.map(e => (
                <option key={e.id} value={e.id}>
                  S{String(e.season).padStart(2, "0")}E{String(e.episode).padStart(2, "0")}
                  {" — "}{e.status}{e.id === rec.key ? "  (this record)" : ""}
                </option>
              ))}
            </select>
            {msg && <span className="sub ok">{msg}</span>}
          </div>

          <div style={{ display: "grid", gridTemplateColumns: "minmax(0,1fr) minmax(0,1fr)",
                        gap: 14, alignItems: "start" }}>
            <div>
              <div className="section-title" style={{ marginTop: 0 }}>Files in the download</div>
              {files.length === 0 && <Empty>
                The record names no download, or qB could not be asked about it. Nothing can be
                assigned until a donor exists — pick a release first.
              </Empty>}
              {files.map((f, i) => f.error
                ? <div className="err" key={i}>{f.error}</div>
                : (
                  <div className="dashrow" key={f.file}>
                    <div className="dashrow-main">
                      <div className="sub mono" title={f.file}>
                        {String(f.file).split("/").pop()}
                      </div>
                      <div className="sub">
                        {f.parsed_season == null
                          ? <span className="warn">no S/E in the name</span>
                          : <>parsed S{String(f.parsed_season).padStart(2, "0")}E
                              {String(f.parsed_episode).padStart(2, "0")}</>}
                        {f.maps_to?.length > 0 && <> · could mean {f.maps_to
                          .map((m: number[]) => `S${String(m[0]).padStart(2, "0")}E${String(m[1]).padStart(2, "0")}`)
                          .join(", ")}</>}
                      </div>
                    </div>
                    <Act cls="btn sec small" busyLabel="assigning…"
                      title="Point this file at the selected episode and queue the merge"
                      run={async () => {
                        const ok = await runAction(() => api.assign(target, f.file));
                        if (ok) { setMsg(`assigned → ${target}`); onDone(); }
                      }}>Assign</Act>
                  </div>
                ))}
            </div>

            <div>
              <div className="section-title" style={{ marginTop: 0 }}>Episodes in the library</div>
              {eps.length === 0 && <Empty>Sonarr reported no episodes for this series.</Empty>}
              <div className="scroll-y tall">
                {eps.map(e => (
                  <div className="dashrow" key={e.id}>
                    <div className="dashrow-main">
                      <div className="sub">
                        S{String(e.season).padStart(2, "0")}E{String(e.episode).padStart(2, "0")}
                        {e.id === rec.key && <b> ← this record</b>}
                      </div>
                      {e.french_path && <div className="sub mono" title={e.french_path}>
                        {String(e.french_path).split("/").pop()}
                      </div>}
                    </div>
                    <Pill s={e.status} />
                  </div>
                ))}
              </div>
            </div>
          </div>
        </>
      )}
    </Modal>
  );
}

/** The sync tuner takes a full `Movie`; this page carries the flat problem record, which has a
 *  key and a title and none of the paths the preview needs. Fetching the real record is one call
 *  and keeps the tuner honest — a hand-built object would silently lose whatever field it forgot. */
function TuneGate({ id, onClose }: { id: number; onClose: () => void }) {
  const [m, setM] = useState<Movie | null>(null);
  const [err, setErr] = useState("");
  useEffect(() => {
    let alive = true;
    api.movies().then(list => {
      if (!alive) return;
      const found = list.find(x => x.tmdb_id === id);
      if (found) setM(found); else setErr("that film is no longer in the pipeline");
    }).catch(e => { if (alive) setErr(e.message || "could not load the film"); });
    return () => { alive = false; };
  }, [id]);
  if (err) return <Modal title="Tune sync" onClose={onClose}><div className="err">{err}</div></Modal>;
  if (!m) return <Modal title="Tune sync" onClose={onClose}>
    <div className="muted">loading…</div></Modal>;
  return <SyncEditor movie={m} onClose={onClose} />;
}
