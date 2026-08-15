"""The one classifier over the probe inventory: which files meet their profile, and which don't.

This lived inside main.py, which made it reachable only from a request handler. The daily history
sampler and the coverage snapshot both need exactly the same answer, and a second implementation
of "what does complete mean" is the one thing this app cannot afford — /coverage and /library were
deliberately built on a single classifier for that reason. Moving it here keeps that property
while letting the scheduler use it too.
"""
import os

from . import core, media, pipeline

# What counts as vo-merge having actually CHANGED a file. `replaced` legitimately records no added
# languages (the download became the file), so it can only be recognised by its kind; a `grafted`
# row that recorded nothing added says nothing about what the app did, and `already` (a scan
# closing out a file that was correct on its own) is not work at all. Recently-merged, the 24h/7d
# counters, the completion forecast and the throughput chart all read this, so they cannot
# disagree about what a completion is.
DID_WORK = ("(merge_kind = 'replaced' OR COALESCE(added_langs,'') != '' "
            "OR COALESCE(added_subs,'') != '')")


def build(cfg, cols="path, auds, subs, err"):
    """Every probed library file, classified against the profile its library targets.

    The `probes` table is the ONLY complete inventory: `scan()` deliberately inserts a
    movies/episodes record only when a file HAS a gap, so those tables are a list of problems,
    not a list of files. Everything that reports on "how much of the library is correct" —
    /coverage, /library and the coverage history all — reads this, so the summary, the browsable
    list and the chart can never disagree about what "complete" means.

    Yields (row, top, kind, want_a, want_s, have_a, have_s, miss_a, miss_s). `-EN` mirrors are
    skipped: they are symlinks to the same files and would double-count."""
    mount = (cfg.get("media_mount") or "/media").rstrip("/")
    anime = {x.lower() for x in (cfg.get("anime_dirs") or ["Anime"])}
    series = {x.lower() for x in (cfg.get("series_dirs") or ["Series"])}
    mirrors = {v.lower() for v in pipeline.EN_LIBS.values()}
    prof = {}
    with core.db() as c:
        rows = c.execute(f"SELECT {cols} FROM probes").fetchall()
        # The anime profile's original-audio slot resolves per title, and the probes table has no
        # idea what a title's original language is — so borrow it from whichever record covers
        # this path. A file with no record (it never had a gap) leaves the slot unresolved, which
        # drops it: better than demanding a language we can't name.
        origs = {r["p"]: r["o"] for r in c.execute(
            "SELECT french_path p, original_lang o FROM movies WHERE french_path IS NOT NULL "
            "UNION ALL SELECT french_path p, orig_lang o FROM episodes "
            "WHERE french_path IS NOT NULL")}
    for r in rows:
        path = r["path"] or ""
        top = path[len(mount) + 1:].split(os.sep, 1)[0] if path.startswith(mount + "/") else "?"
        if top.lower() in mirrors:
            continue
        kind = "anime" if top.lower() in anime else ("series" if top.lower() in series else "movie")
        key = (kind, origs.get(path))
        if key not in prof:
            prof[key] = media.profile(kind, cfg, key[1])
        want_a, want_s = prof[key]
        if r["err"]:
            yield r, top, kind, want_a, want_s, None, None, None, None
            continue
        have_a = {x for x in (r["auds"] or "").split(",") if x}
        have_s = {x for x in (r["subs"] or "").split(",") if x}
        yield (r, top, kind, want_a, want_s, have_a, have_s,
               [k for k in want_a if k not in have_a], [k for k in want_s if k not in have_s])


def totals(cfg):
    """Collapse the inventory into the counters the coverage chart and its history both use.

    Returns {total, complete, missing_audio, missing_subs, missing_both, unreadable,
             libs: {name: {total, complete}}}. The five middle counters partition `total`
    exactly — every probed file is complete, short of audio, short of subs, short of both, or
    unreadable. There is deliberately no "not targeted" bucket: a probed file is one of those
    five, which is what makes the stacked bar add up to the library."""
    out = {"total": 0, "complete": 0, "missing_audio": 0, "missing_subs": 0,
           "missing_both": 0, "unreadable": 0, "libs": {}}
    subs_gap = bool(cfg.get("subs_only_gap", True))
    for _r, top, _kind, _wa, _ws, _ha, _hs, miss_a, miss_s in build(cfg):
        lib = out["libs"].setdefault(top, {"total": 0, "complete": 0})
        out["total"] += 1
        lib["total"] += 1
        if miss_a is None:                      # err set: the probe could not read it
            out["unreadable"] += 1
            continue
        # A subtitle-only shortfall is only a gap when the pipeline would actually chase it.
        # Counting it as incomplete while `subs_only_gap` is off would report a backlog nothing
        # will ever work on, which is how a coverage number stops being trusted.
        short_s = bool(miss_s) and subs_gap
        if miss_a and short_s:
            out["missing_both"] += 1
        elif miss_a:
            out["missing_audio"] += 1
        elif short_s:
            out["missing_subs"] += 1
        else:
            out["complete"] += 1
            lib["complete"] += 1
    return out
