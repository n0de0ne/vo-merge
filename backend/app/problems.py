"""Failures, grouped by CAUSE, with the remedy that actually fixes each one.

The Review tab listed failures one per row, newest first, with the raw `error` string and four
generic buttons. That shape is fine for five failures and useless for four hundred: one bad season
pack produces 40 rows saying the same thing, the operator reads the same sentence 40 times, and
the actual decision — "this whole group is a PAL transfer, apply the ratio" — is invisible because
nothing ever puts the 40 rows next to each other.

Worse, the generic buttons don't match the causes. "Retry" on a numbering mismatch re-downloads a
pack that will fail identically; "Pick another" on a donor that vanished is right but nobody knows
that from the message. So records that had a mechanical fix sat in the backlog looking impossible.

This module is the missing middle:

- `classify()` maps an (status, error) pair onto one of the causes below. The patterns are taken
  from the literal strings pipeline.py and tv.py write — when one of those changes, the test in
  test_problems.py fails, which is the point.
- Each cause names its REMEDIES in the order worth trying, and says whether a remedy is safe to
  apply to a whole group at once (`bulk`) or needs a per-record decision (an episode numbering
  mapping cannot be guessed in bulk).
- `PAL/rate` failures carry the arithmetic ratio INSIDE the error string, because
  `_sync_fail_reason` puts it there for the on-call AI. Parsing it back out (`drift_of`) turns the
  single largest "impossible" bucket into a one-click fix for a whole season.

Nothing here decides anything the pipeline could have decided itself — those decisions belong in
the escalation ladder (docs/AUTONOMY.md). This is for what is left after the ladder has run.
"""
import re
import threading
import time

from . import core

# --------------------------------------------------------------------------- the taxonomy
# Ordered: the FIRST rule whose pattern matches wins, so specific shapes must precede general
# ones ("post-merge QC ... misaligned" before the generic "couldn't sync").
#
# fields:
#   code      stable identifier (the UI groups and the API act on this)
#   label     what to call it on screen
#   why       what actually happened, in one sentence, in the operator's terms
#   fix       what will help, in one sentence — the thing the old raw error never said
#   remedies  action codes in the order worth trying (see REMEDIES)
#   severity  'fixable'  — a mechanical remedy exists
#             'judgement'— someone has to choose (which release, which mapping)
#             'external' — the cause is outside vo-merge (mount, disk, indexer, broken file)
#   match     regex against the error text (None = matched by status alone)
RULES = [
    dict(code="rate_mismatch", label="Rate/PAL transfer", severity="fixable",
         match=r"the arithmetic ratio is\s+([0-9.]+)",
         why="The two files play at different framerates — the classic PAL transfer, where a "
             "25fps library file runs ~4.3% short of the 23.976fps release.",
         fix="The stretch is pure arithmetic and the pipeline already computed it: apply the "
             "ratio it quoted. This is safe to apply to a whole season at once.",
         remedies=["apply_drift", "sync_probe_apply", "lipsync_apply", "another"]),
    dict(code="fps_autosync_off", label="Framerates differ, auto-sync off", severity="fixable",
         match=r"framerate differs \(([^)]*)\), auto-sync off",
         why="The donor and the library file have different framerates and automatic drift "
             "correction is switched off, so the merge refused rather than guessing. On TV this "
             "is terminal on the first attempt, so these records never spent their retry budget.",
         fix="Turn auto-sync on in Settings → Merging & sync, then retry — or apply the ratio by "
             "hand if you know it.",
         remedies=["sync_probe_apply", "apply_drift", "retry", "another"]),
    dict(code="rate_unmeasured", label="Rate differs, ratio unknown", severity="judgement",
         match=r"framerates differ but no reliable drift",
         why="The framerates differ but the probe could not report them, so not even the "
             "arithmetic ratio is available — the usual cause is a container whose rate ffprobe "
             "will not state.",
         fix="Measure it: the wide probe reports a stretch as well as an offset. Failing that, a "
             "release at the library's own framerate simply works and needs no correction.",
         remedies=["sync_probe_apply", "lipsync", "another", "search"]),
    dict(code="resync_unaligned", label="Manual re-sync found no alignment", severity="judgement",
         match=r"resync: no confident alignment",
         why="The in-place re-sync tool cross-correlated the file's own two audio tracks and "
             "found nothing it would stand behind.",
         fix="Set the offset by hand from the sync tuner, or read the lips — that measures each "
             "track against the picture instead of against the other track.",
         remedies=["lipsync", "sync_probe", "retry"]),
    dict(code="qc_rejected", label="Post-merge QC rejected the graft", severity="judgement",
         match=r"post-merge (QC|lip-sync)",
         why="The merge completed, then the grafted track was cross-correlated against the "
             "library file's own audio inside the output and came back confidently misaligned. "
             "The output was discarded before it could reach the library.",
         fix="The sync was wrong, not the release choice — measure it properly (a wide probe or "
             "a lip-sync read) or take a different donor.",
         remedies=["sync_probe_apply", "lipsync_apply", "another", "ai"]),
    dict(code="different_cut", label="Different cut — no offset aligns them", severity="judgement",
         match=r"couldn't sync|low-confidence sync|no compatible release after|different cut",
         why="The analysis windows disagree even after the wide ±300s rescue, which means "
             "material differs INSIDE the runtime (an extra scene, a different edit) — no single "
             "offset can align the pair.",
         fix="Measure it once more at full range, try lip-sync (it reads the picture, not the "
             "other file), or take a different release. A genuinely different cut is a release "
             "choice, not a sync problem.",
         remedies=["sync_probe", "lipsync", "another", "search", "unfixable"]),
    dict(code="numbering", label="Episode numbering mismatch", severity="judgement",
         match=r"download has .*this needs|numbering",
         why="The pack's files and the library's episode records use different numbering "
             "schemes, and the automatic aired↔absolute translation could not bridge them.",
         fix="Map one donor file to one episode. The record's context call lists both sides, so "
             "the mapping is a reading exercise — but it is per-episode, not bulk.",
         remedies=["assign", "ai", "search"]),
    dict(code="donor_gone", label="Donor vanished before the merge", severity="fixable",
         match=r"donor file vanished|merge interrupted and source file missing",
         why="The downloaded file was gone by the time the merge ran — deleted by a seed manager, "
             "cleaned up, or moved somewhere qB could no longer be asked about.",
         fix="Nothing is wrong with the title; fetch it again.",
         remedies=["retry", "another"]),
    dict(code="donor_unreadable", label="Donor unreadable", severity="fixable",
         match=r"donor unreadable|download complete but no video file",
         why="The download finished but nothing in it could be read as a video — a broken or "
             "mislabelled release.",
         fix="Blocklist it and take another release.",
         remedies=["another", "retry", "search"]),
    dict(code="useless_release", label="Release adds nothing", severity="judgement",
         match=r"release carries none of the missing languages|release adds nothing this file needs",
         why="The donor was probed and, whichever file won the video comparison, the output "
             "would give the library file nothing it still lacks.",
         fix="The scoring picked the wrong candidate — search by hand, or let it try another. If "
             "this repeats for one title, the language it needs may simply not be released.",
         remedies=["search", "another", "unfixable"]),
    dict(code="mux_failed", label="mkvmerge could not mux it", severity="fixable",
         match=r"mux failed|multi remux failed",
         why="mkvmerge refused the inputs — usually a codec or container it cannot parse, "
             "occasionally a full disk.",
         fix="Check free space, then take a different release; the library file is untouched.",
         remedies=["another", "retry"]),
    dict(code="not_visible", label="Download never became visible", severity="external",
         match=r"never became visible",
         why="qB reported the download complete, but the path it named never appeared to this "
             "container. That is a MOUNT mismatch, not a media problem: `downloads_mount` and "
             "qB's own save path must resolve to the same files.",
         fix="Fix the two paths in Settings → Paths so they point at the same directory, then "
             "retry. Retrying without fixing them will fail identically.",
         remedies=["retry", "ai"]),
    dict(code="stalled", label="No seeded release could be downloaded", severity="external",
         match=r"stalled after|releases stalled|stuck for \d+ min|download .* — stuck",
         why="Every release tried stopped making progress: dead magnets, no seeders, or a "
             "download client that is not actually connected.",
         fix="Check qBittorrent and the indexers, then search by hand — the automatic picker has "
             "already exhausted its candidates.",
         remedies=["search", "retry", "ai"]),
    dict(code="library_missing", label="Library file is gone", severity="external",
         match=r"library file missing on disk|missing french_path|resync: file not found",
         why="The file the merge was going to modify is not on disk. Either it was deleted or "
             "replaced outside the *arrs, or /media is not mounted the way it was at scan time.",
         fix="Re-read the title so the record matches what is actually there now.",
         remedies=["rescan", "retry", "unfixable"]),
    dict(code="probe_failed", label="Library file could not be read", severity="external",
         match=r"library file probe failed|resync: need two distinct audio tracks"
               r"|resync: no \w+ track",
         why="mkvmerge could not read the library file well enough to work with it.",
         fix="Look at the file itself — the Library tab's Unreadable view says which of the four "
             "distinct probe failures this is, and only one of them means the file is broken.",
         remedies=["rescan", "unfixable", "ai"]),
    dict(code="finish_failed", label="The library swap failed", severity="external",
         match=r"^finish: |resync ",
         why="The merge produced a good file and then something went wrong putting it in place — "
             "moving it over the library file, deleting the donor, or telling Radarr and Plex.",
         fix="The output usually still exists. Retry: the merge re-runs and the swap is attempted "
             "again. A repeat almost always means permissions or a full disk on the library "
             "share.",
         remedies=["retry", "rescan", "ai"]),
    dict(code="operator", label="Stopped by the operator", severity="fixable",
         match=r"by the operator",
         why="Someone aborted this merge or took it off the queue.",
         fix="Put it back when you want it.",
         remedies=["retry"]),
    dict(code="transient", label="Repeated infrastructure failure", severity="external",
         match=r"consecutive attempts",
         why="The same retryable failure — a network timeout, a qB blip — happened enough times "
             "in a row that it stopped counting as transient.",
         fix="The dependency is genuinely down. Settings → Test each service, then retry.",
         remedies=["retry", "ai"]),
    dict(code="no_release", label="Nothing found to download", severity="judgement",
         status=("no_release",), match=None,
         why="The indexers returned nothing carrying a language this file is missing. The query "
             "ladder has already tried the original title, the *arr title and the alternates.",
         fix="Search with a query of your own — a different romanisation or release name is the "
             "single biggest reason a title is unfindable.",
         remedies=["search", "retry", "ai", "unfixable"]),
    dict(code="needs_decision", label="Waiting on a human decision", severity="judgement",
         status=("review",), match=None,
         why="Parked for a person on purpose: the donor is still on disk so this exact pair can "
             "still be aligned.",
         fix="Tune the sync, or release it back to the pipeline.",
         remedies=["sync_probe_apply", "lipsync_apply", "retry", "another", "unfixable"]),
    dict(code="unknown", label="Uncategorised failure", severity="judgement",
         match=None,
         why="This failure does not match any known cause.",
         fix="Send it to the on-call AI, which reads the record's full context and log lines.",
         remedies=["ai", "retry", "search", "unfixable"]),
]

_COMPILED = [(r, re.compile(r["match"], re.I) if r.get("match") else None) for r in RULES]
BY_CODE = {r["code"]: r for r in RULES}

# What each remedy does, whether a whole group can be given it at once, and whether it is slow
# enough to need a background run. `bulk: False` means the action needs a per-record decision and
# the UI must only offer it on a single row.
REMEDIES = {
    "retry":            dict(label="Retry", bulk=True, slow=False, danger=False,
                             note="Blocklist the release that failed, drop its download, and "
                                  "search again from scratch."),
    "another":          dict(label="Pick another release", bulk=True, slow=False, danger=False,
                             note="Keep the record, blocklist this release, search for a "
                                  "different one."),
    "apply_drift":      dict(label="Apply the computed rate", bulk=True, slow=False, danger=False,
                             note="Use the stretch ratio the pipeline already worked out from "
                                  "the two framerates, and re-merge."),
    "sync_probe":       dict(label="Measure the offset", bulk=True, slow=True, danger=False,
                             note="Re-run detection at ±300s and report what it finds. Changes "
                                  "nothing on its own."),
    "sync_probe_apply": dict(label="Measure and merge", bulk=True, slow=True, danger=False,
                             note="Re-run detection at ±300s and merge with the result."),
    "lipsync":          dict(label="Read the lips", bulk=True, slow=True, danger=False,
                             note="Correlate mouth movement in the picture against the speech in "
                                  "each audio track. The only measurement that does not depend "
                                  "on the two files agreeing with each other."),
    "lipsync_apply":    dict(label="Read the lips and merge", bulk=True, slow=True, danger=False,
                             note="As above, then merge with the offset it measures."),
    "rescan":           dict(label="Re-read the file", bulk=True, slow=True, danger=False,
                             note="Probe what is on disk right now, bypassing the cache, and "
                                  "update the record."),
    "search":           dict(label="Search releases…", bulk=False, slow=False, danger=False,
                             note="Run your own indexer query for this title."),
    "assign":           dict(label="Map a donor file…", bulk=False, slow=False, danger=False,
                             note="Point one downloaded file at one episode."),
    "ai":               dict(label="Send to the on-call AI", bulk=True, slow=False, danger=False,
                             note="File a ticket. The agent reads the record's context and log "
                                  "lines and acts through this same API."),
    "unfixable":        dict(label="Give up, with a reason", bulk=True, slow=False, danger=False,
                             note="Mark it ignored and record WHY, so it doesn't read as an "
                                  "unexamined skip. Re-examined periodically anyway."),
}


def classify(status, error):
    """Which cause this failure is. Falls back to `unknown`, never raises."""
    text = error or ""
    for rule, rx in _COMPILED:
        if rule.get("status") and status not in rule["status"]:
            continue
        if rx is None:
            if rule.get("status"):
                return rule["code"]           # matched by status alone (no_release / review)
            continue
        if rx.search(text):
            return rule["code"]
    return "unknown"


def drift_of(error):
    """The rate ratio a `rate_mismatch` error quotes, or None.

    `pipeline._sync_fail_reason` writes the arithmetic ratio into the message precisely so the
    next actor doesn't have to re-derive it. Reading it back is what makes "apply the computed
    rate" a button instead of a research task."""
    m = re.search(r"the arithmetic ratio is\s+([0-9.]+)", error or "", re.I)
    if not m:
        return None
    try:
        k = float(m.group(1))
    except ValueError:
        return None
    return k if 0.9 <= k <= 1.11 else None      # same guard main._set_sync applies


# --------------------------------------------------------------------------- grouping
FAIL_STATES = ("error", "sync_fail", "review", "no_release")


def _rows(states=FAIL_STATES):
    """Every failing record, as flat dicts with a `kind`/`key` identity."""
    marks = ",".join("?" * len(states))
    out = []
    with core.db() as c:
        for r in c.execute(f"SELECT tmdb_id, title, year, status, error, poster, updated, "
                           f"ai_status, ai_verdict, priority, sync_drift, en_file, french_path "
                           f"FROM movies WHERE status IN ({marks})", states):
            d = dict(r)
            out.append(dict(d, kind="movie", key=str(d["tmdb_id"]), title=d["title"],
                            sub=str(d["year"]) if d["year"] else None))
        for r in c.execute(f"SELECT id, series_id, series_title, season, episode, status, error, "
                           f"poster, updated, ai_status, ai_verdict, priority, sync_drift, "
                           f"en_file, french_path FROM episodes WHERE status IN ({marks})", states):
            d = dict(r)
            out.append(dict(d, kind="episode", key=d["id"], title=d["series_title"],
                            sub=f"S{int(d['season'] or 0):02d}E{int(d['episode'] or 0):02d}"))
    return out


def groups(limit_samples=8, states=FAIL_STATES):
    """Failures rolled up by cause — the Problems page.

    Each group carries enough to decide without opening anything: what happened, what fixes it,
    how many records, how many are episodes of the same show (a season pack failing 40 times is
    ONE problem), and the remedies that apply."""
    rows = _rows(states)
    buckets = {}
    for r in rows:
        code = classify(r["status"], r["error"])
        b = buckets.setdefault(code, {"code": code, "items": [], "drifts": set(), "shows": set()})
        b["items"].append(r)
        k = drift_of(r["error"])
        if k:
            b["drifts"].add(round(k, 7))
        if r["kind"] == "episode":
            b["shows"].add(r.get("series_id"))
    out = []
    for code, b in buckets.items():
        rule = BY_CODE[code]
        items = sorted(b["items"], key=lambda x: -(x.get("updated") or 0))
        # Only offer a remedy the group can actually take. `apply_drift` on a group where no
        # record quotes a ratio is a button that does nothing, which is worse than no button.
        remedies = [x for x in rule["remedies"]
                    if x != "apply_drift" or b["drifts"]]
        out.append({
            "code": code, "label": rule["label"], "severity": rule["severity"],
            "why": rule["why"], "fix": rule["fix"],
            "count": len(items),
            "movies": sum(1 for i in items if i["kind"] == "movie"),
            "episodes": sum(1 for i in items if i["kind"] == "episode"),
            "shows": len([s for s in b["shows"] if s is not None]),
            "with_ai": sum(1 for i in items if i.get("ai_status") in ("failed", "needs_human")),
            "newest": items[0].get("updated") if items else None,
            "drifts": sorted(b["drifts"]),
            "remedies": [dict(REMEDIES[x], code=x) for x in remedies if x in REMEDIES],
            "sample_error": next((i["error"] for i in items if i.get("error")), None),
            "samples": [{k: i.get(k) for k in
                         ("kind", "key", "title", "sub", "status", "error", "poster",
                          "updated", "ai_status", "ai_verdict", "priority")}
                        for i in items[:limit_samples]],
        })
    # Fixable first — the point of the page is to empty those, and burying them under a big
    # judgement bucket is how the backlog stops looking actionable.
    order = {"fixable": 0, "judgement": 1, "external": 2}
    out.sort(key=lambda g: (order.get(g["severity"], 3), -g["count"]))
    return {"groups": out, "total": len(rows), "now": time.time()}


def records(code, limit=200, offset=0, states=FAIL_STATES):
    """Every record in one group, for the drill-down list."""
    rows = [r for r in _rows(states) if classify(r["status"], r["error"]) == code]
    rows.sort(key=lambda x: (-(x.get("priority") or 0), -(x.get("updated") or 0)))
    page = rows[offset:offset + limit]
    for r in page:
        r["drift"] = drift_of(r["error"])
        r["has_donor"] = bool(r.get("en_file"))
    return {"code": code, "total": len(rows), "offset": offset, "limit": limit,
            "items": page, "rule": BY_CODE.get(code), "now": time.time()}


# --------------------------------------------------------------------------- bulk remedies
# A remedy that probes files is minutes of work on a big group, so it runs in a thread with the
# same progress-state shape the rescan and repair passes use — the UI already knows how to poll
# that, and a button that blocks a request for four minutes is a button that times out.
BULK_STATE = {"running": False, "action": "", "code": "", "started": 0.0, "finished": 0.0,
              "total": 0, "done": 0, "ok": 0, "failed": 0, "results": [], "error": None}
_BULK_LOCK = threading.Lock()


def _apply_one(action, rec, cfg, params):
    """Run one remedy against one record. Returns (ok, message). Imports live here so this module
    stays importable by anything that only wants `classify`."""
    from . import lipsync, pipeline, tv
    from .clients import QBittorrent
    kind, key = rec["kind"], rec["key"]
    ident = int(key) if kind == "movie" else key
    setter = core.set_status if kind == "movie" else core.set_ep_status

    if action == "retry":
        ok = (pipeline.retry_movie(ident, cfg) if kind == "movie"
              else tv.retry_episode(ident, cfg))
        return ok, "re-queued" if ok else "record not found"

    if action == "another":
        # The same steps POST /movie/{id}/another takes, and for the same reasons: dropping the
        # torrent matters (a bulk pass that only cleared the columns would leave a dozen orphaned
        # downloads holding grab slots), and AI_RESET matters (a stale "the AI couldn't fix it"
        # verdict on a record that is now searching again is the exact stale-badge bug the Review
        # filter was rewritten to end). `attempts` is deliberately NOT reset — this is "try the
        # next candidate", not "start the budget over", which is what `retry` is for.
        rec_full = core.get_movie(ident) if kind == "movie" else core.get_episode(ident)
        if not rec_full:
            return False, "record not found"
        if rec_full.get("dl_hash"):
            try:
                qb = QBittorrent(cfg["qb_url"], cfg["qb_user"], cfg["qb_pass"]); qb.login()
                qb.delete([rec_full["dl_hash"]], delete_files=True)
            except Exception as e:
                core.log(f"problems: could not drop donor for {kind} {ident}: {e}")
        setter(ident, "pending", tried=pipeline.blocklist(rec_full), error=None,
               **pipeline.DONOR_RESET, **pipeline.AI_RESET)
        return True, "blocklisted and searching for a different release"

    if action == "apply_drift":
        k = params.get("drift") or drift_of(rec.get("error"))
        if not k:
            return False, "no rate ratio in this record's message"
        setter(ident, rec["status"], sync_offset_ms=int(params.get("offset_ms") or 0),
               sync_drift=float(k), sync_manual=1, error=None)
        queued, note = pipeline.enqueue_merge(kind, ident)
        return queued, f"rate ×{k:.7f} — {note}"

    if action in ("sync_probe", "sync_probe_apply"):
        return _probe_one(kind, ident, cfg, apply=action.endswith("_apply"))

    if action in ("lipsync", "lipsync_apply"):
        if not cfg.get("lipsync_enabled", True):
            return False, "lip-sync is switched off in settings"
        return lipsync.remedy(kind, ident, cfg, apply=action.endswith("_apply"))

    if action == "rescan":
        # The same entry points the webhook and the library sweep use, so a re-read from here can
        # never judge a file differently from the way it would have been judged anyway.
        if kind == "movie":
            from .clients import Radarr  # noqa: local import, see module head
            mv = core.get_movie(ident)
            if not mv or not mv.get("radarr_id"):
                return False, "no Radarr id on this record — nothing to re-read it from"
            m = Radarr(cfg["radarr_url"], cfg["radarr_key"]).movie(mv["radarr_id"])
            if not m:
                return False, "Radarr no longer has this movie"
            return True, str(pipeline.ingest_movie(m, cfg, refresh=True))
        n = tv.scan(cfg, only_series=rec.get("series_id"), refresh=True)
        return True, f"re-read; {n} gap(s) in this series"

    if action == "ai":
        from . import agent
        if not agent.enabled(cfg):
            return False, "AI escalation is switched off in settings"
        rec_full = core.get_movie(ident) if kind == "movie" else core.get_episode(ident)
        if not rec_full:
            return False, "record not found"
        summary, ctx = (agent.movie_context(rec_full) if kind == "movie"
                        else agent.episode_context(rec_full))
        name = f"review-{'m' if kind == 'movie' else 'e'}{ident}"
        if not core.ticket(name, f"escalated from the Problems page: {summary}", ctx, force=True):
            return False, "could not write the ticket"
        setter(ident, rec_full["status"], ai_status="pending", ai_at=time.time())
        return True, "ticket filed"

    if action == "unfixable":
        reason = params.get("reason") or "marked unfixable from the Problems page"
        setter(ident, "ignored", error=reason, ai_status="needs_human", ai_verdict=reason,
               ai_at=time.time(), progress="")
        return True, "ignored, with the reason recorded"

    return False, f"unknown remedy {action!r}"


def _probe_one(kind, ident, cfg, apply):
    """Wide-range sync probe for one record, without going through the HTTP layer."""
    from . import media, pipeline, sync as _sync
    rec = core.get_movie(ident) if kind == "movie" else core.get_episode(ident)
    if not rec:
        return False, "record not found"
    base, donor = rec.get("french_path"), rec.get("en_file")
    import os
    if not (base and donor and os.path.exists(base) and os.path.exists(donor)):
        return False, "the donor is no longer on disk — retry instead"
    wide = dict(cfg, sync_max_lag_s=max(5, int(cfg.get("sync_probe_lag_s", 300))))
    bi, di = media.probe(base), media.probe(donor)
    if not bi or not di:
        return False, "could not probe both files"
    off, conf, method, drift = _sync.detect(base, donor, 0, 0, bi["dur"] or di["dur"] or 0,
                                            wide, tag=" bulk-probe")
    if off is None:
        return False, f"no consistent offset within ±{wide['sync_max_lag_s']}s ({method})"
    msg = f"{int(off):+d}ms conf {conf:.2f} ({method})"
    if not apply:
        return True, msg
    setter = core.set_status if kind == "movie" else core.set_ep_status
    setter(ident, rec["status"], sync_offset_ms=int(off), sync_drift=drift, sync_manual=1,
           error=None)
    queued, note = pipeline.enqueue_merge(kind, ident)
    return queued, f"{msg} — {note}"


def start_bulk(action, targets, cfg, params=None):
    """Apply one remedy to many records, in a background thread.

    Returns False when a pass is already running: two bulk passes over the same records would
    race each other's state writes, and the second would act on records the first has already
    moved."""
    params = params or {}
    with _BULK_LOCK:
        if BULK_STATE["running"]:
            return False
        BULK_STATE.update(running=True, action=action, code=params.get("code", ""),
                          started=time.time(), finished=0.0, total=len(targets), done=0,
                          ok=0, failed=0, results=[], error=None)

    def _run():
        try:
            for rec in targets:
                try:
                    ok, msg = _apply_one(action, rec, cfg, params)
                except Exception as e:            # one bad record must not stop the pass
                    ok, msg = False, f"{type(e).__name__}: {e}"
                BULK_STATE["done"] += 1
                BULK_STATE["ok" if ok else "failed"] += 1
                BULK_STATE["results"].append({
                    "kind": rec["kind"], "key": rec["key"], "title": rec.get("title"),
                    "sub": rec.get("sub"), "ok": ok, "message": msg})
            core.log(f"problems: {action} on {BULK_STATE['total']} record(s) — "
                     f"{BULK_STATE['ok']} ok, {BULK_STATE['failed']} failed")
        except Exception as e:
            BULK_STATE["error"] = str(e)
            core.log(f"problems: bulk {action} error: {e}")
        finally:
            BULK_STATE.update(running=False, finished=time.time())

    threading.Thread(target=_run, daemon=True).start()
    return True


def targets_for(code=None, keys=None, states=FAIL_STATES):
    """Resolve a bulk request onto concrete records. `keys` is a list of "movie:123"/"episode:1:2:3"
    strings; `code` selects a whole group. Explicit keys win, so a partial selection in the UI is
    never silently widened to the group."""
    rows = _rows(states)
    if keys:
        want = set(keys)
        return [r for r in rows if f"{r['kind']}:{r['key']}" in want]
    if code:
        return [r for r in rows if classify(r["status"], r["error"]) == code]
    return []
