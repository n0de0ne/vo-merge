"""Everything that answers "is this getting better?" — the charts' data source.

The app kept only snapshots: `movies`/`episodes` are current state, `probes` is what each file
holds right now. So every question with a time axis was unanswerable, and the two that were asked
anyway had to be faked from the one timestamp that happens to persist (`merged_at`) — which exists
for exactly one of the twelve states. `core.events` (written from the single choke point every
transition passes through) and `core.coverage_history` (a daily roll-up of the inventory) are the
two series this module reads.

Two rules the readers here are built on, because getting either wrong makes an honest chart lie:

- **A day with no coverage SAMPLE is a hole, not a zero.** Nobody probed the library that day; the
  library did not become 0% complete. Those days come back as `null` and the line breaks.
- **A day with no EVENTS is a real zero.** Nothing merged, nothing failed. Filling it in is the
  whole point — leaving it out silently compresses the x-axis and turns a quiet week into a
  vertical cliff.
"""
import calendar
import json
import time

from . import core, inventory

DAY = 86400

# Which transitions each throughput series counts. `merged` is the terminal state of three
# different outcomes and only two of them are work we did, so the split is by `tag` (the
# merge_kind carried into the event) rather than by status — otherwise a library re-read reads as
# thousands of merges, which is the exact mistake "Recently merged" was rebuilt to avoid.
FAIL_STATES = ("error", "sync_fail", "review")


def _day(ts):
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def _axis(days):
    """The dense day axis, oldest first, ending today (UTC). Every series is emitted against this
    same list so the frontend can zip them without matching on dates."""
    today = time.time()
    return [_day(today - i * DAY) for i in range(days - 1, -1, -1)]


def coverage_series(days=30):
    """Daily library coverage. Days with no sample are `null` — see the module docstring."""
    axis = _axis(days)
    with core.db() as c:
        rows = {r["day"]: dict(r) for r in c.execute(
            "SELECT * FROM coverage_history WHERE day >= ? ORDER BY day", (axis[0],))}
    out = []
    for d in axis:
        r = rows.get(d)
        if not r:
            out.append({"day": d, "total": None, "complete": None, "pct": None})
            continue
        total, comp = int(r["total"] or 0), int(r["complete"] or 0)
        # The percentage is of what could ever be complete: an unreadable file is not a language
        # problem, and leaving it in the denominator caps the chart below 100% forever with no
        # explanation on screen.
        denom = total - int(r["unreadable"] or 0)
        out.append({
            "day": d, "total": total, "complete": comp,
            "pct": round(100.0 * comp / denom, 2) if denom > 0 else None,
            "missing_audio": int(r["missing_audio"] or 0),
            "missing_subs": int(r["missing_subs"] or 0),
            "missing_both": int(r["missing_both"] or 0),
            "unreadable": int(r["unreadable"] or 0),
            "libs": json.loads(r["libs"] or "{}"),
        })
    return out


def throughput_series(days=30):
    """Per-day counts of what the pipeline actually did. Missing days are zeros, deliberately."""
    axis = _axis(days)
    blank = {"grafted": 0, "replaced": 0, "already": 0, "failed": 0,
             "grabbed": 0, "no_release": 0, "ignored": 0}
    out = {d: dict(blank, day=d) for d in axis}
    # timegm, not mktime: every day key in this module is UTC (`_day` uses gmtime), and mktime
    # would read the same string as local time — silently shifting the window by the host's offset
    # and dropping or double-counting the edge day for anyone not on UTC.
    since = calendar.timegm(time.strptime(axis[0], "%Y-%m-%d"))
    with core.db() as c:
        rows = c.execute("SELECT ts, sts, tag FROM events WHERE ts >= ?", (since,)).fetchall()
    for r in rows:
        d = out.get(_day(r["ts"]))
        if not d:
            continue
        sts = r["sts"]
        if sts == "merged":
            d[(r["tag"] or "grafted") if r["tag"] in ("grafted", "replaced", "already")
              else "grafted"] += 1
        elif sts in FAIL_STATES:
            d["failed"] += 1
        elif sts == "downloading":
            d["grabbed"] += 1
        elif sts == "no_release":
            d["no_release"] += 1
        elif sts == "ignored":
            d["ignored"] += 1
    series = [out[d] for d in axis]
    for d in series:
        d["merged"] = d["grafted"] + d["replaced"]      # work we did, excluding 'already'
    return series


def summary(days=30):
    """Everything the Overview's chart row needs, in one round trip."""
    cov = coverage_series(days)
    thru = throughput_series(days)
    totals = {k: sum(d[k] for d in thru)
              for k in ("grafted", "replaced", "already", "failed", "grabbed",
                        "no_release", "ignored", "merged")}
    sampled = [d for d in cov if d["pct"] is not None]
    return {
        "days": days, "now": time.time(),
        "coverage": cov, "throughput": thru, "totals": totals,
        # An empty chart with no explanation reads as "nothing is happening". It usually means
        # no scan has run yet, which is a different thing and has a different fix.
        "have_coverage": bool(sampled),
        "first_sample": sampled[0]["day"] if sampled else None,
        "gained_pct": (round(sampled[-1]["pct"] - sampled[0]["pct"], 2)
                       if len(sampled) >= 2 else None),
    }


def events(limit=100, offset=0, kind=None, status=None, q=None, since=None):
    """The Activity → History table: what happened, newest first."""
    where, args = ["1=1"], []
    if kind in ("movie", "episode"):
        where.append("kind = ?")
        args.append(kind)
    if status:
        marks = ",".join("?" * len(status.split(",")))
        where.append(f"sts IN ({marks})")
        args += [s.strip() for s in status.split(",")]
    if q:
        where.append("(title LIKE ? OR detail LIKE ?)")
        args += [f"%{q}%", f"%{q}%"]
    if since:
        where.append("ts >= ?")
        args.append(float(since))
    sql = " AND ".join(where)
    with core.db() as c:
        total = c.execute(f"SELECT COUNT(*) n FROM events WHERE {sql}", tuple(args)).fetchone()["n"]
        rows = c.execute(f"SELECT * FROM events WHERE {sql} ORDER BY ts DESC, id DESC "
                         f"LIMIT ? OFFSET ?", tuple(args) + (int(limit), int(offset))).fetchall()
    return {"items": [dict(r) for r in rows], "total": total,
            "offset": int(offset), "limit": int(limit), "now": time.time()}


def timeline(kind, key, limit=60):
    """One record's own history — every state it has been through, newest first. This is what
    makes a parked failure legible: "grabbed 3 releases, each rejected for a different reason" is
    a different problem from "grabbed once, sync failed once"."""
    with core.db() as c:
        rows = c.execute("SELECT * FROM events WHERE kind=? AND key=? ORDER BY ts DESC LIMIT ?",
                         (kind, str(key), int(limit))).fetchall()
    return [dict(r) for r in rows]


def sample(cfg):
    """Take today's coverage sample. Idempotent per day, so the end of every scan and the daily
    housekeeping job can both call it without weighting a busy day more than a quiet one."""
    row = inventory.totals(cfg)
    core.snapshot_coverage(row)
    return row


def backfill_if_empty(cfg):
    """The charts are empty on an existing install until something samples, and the first sample
    only lands after the next scan — which can be an hour away, or a day. One sample at startup
    means the Overview has a point to draw immediately rather than an unexplained blank."""
    with core.db() as c:
        n = c.execute("SELECT COUNT(*) n FROM coverage_history").fetchone()["n"]
        probed = c.execute("SELECT COUNT(*) n FROM probes").fetchone()["n"]
    if n == 0 and probed > 0:
        return sample(cfg)
    return None
