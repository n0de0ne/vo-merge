"""Tests for the pieces the UI revamp added: the transition log, the coverage history, the
failure taxonomy, the settings schema and the lip-sync signal chain.

Same rule as test_regressions.py — each test is named for the behaviour it locks down. Three of
these guard against a class of rot rather than a specific bug: the settings schema silently
falling behind DEFAULTS, the error taxonomy silently falling behind the strings pipeline.py
actually writes, and a chart series silently treating a missing sample as a zero.
"""
import os
import sys
import time

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture()
def app_env(tmp_path, monkeypatch):
    """A throwaway /config with its own DB, with every module re-imported against it."""
    monkeypatch.setenv("VO_CONFIG", str(tmp_path))
    for mod in [m for m in list(sys.modules) if m.startswith("app")]:
        del sys.modules[mod]
    from app import core
    core.CONFIG_DIR = str(tmp_path)
    core.CONFIG_FILE = os.path.join(str(tmp_path), "config.json")
    core.DB_FILE = os.path.join(str(tmp_path), "vo-merge.db")
    core.LOG_FILE = os.path.join(str(tmp_path), "vo-merge.log")
    core.init_db(); core.init_tv(); core.init_indexes(); core.init_probe_cache()
    core.init_history()
    return core


def _movie(core, tmdb_id=1, **kw):
    core.upsert_movie(dict(tmdb_id=tmdb_id, imdb_id=None, radarr_id=tmdb_id, title="Film",
                           original_title="Film", year=2020, original_lang="fre",
                           french_path="/media/Films/Film/Film.mkv", quality="Bluray-1080p",
                           poster=None))
    if kw:
        core.set_status(tmdb_id, kw.pop("status", "pending"), **kw)


# ================================================================= the transition log
def test_only_real_state_changes_are_logged(app_env):
    """`_set_row` is called constantly by scans re-writing a record with the SAME status just to
    refresh its language columns. Logging those would bury the timeline in noise and make "what
    happened to this title" unreadable — the same reason `updated` is not bumped for them."""
    core = app_env
    _movie(core)
    core.set_status(1, "pending", audio_langs="fre")      # no transition: pending -> pending
    core.set_status(1, "downloading")                     # a real one
    core.set_status(1, "downloading", progress="45%")     # not a transition
    with core.db() as c:
        rows = [dict(r) for r in c.execute("SELECT frm, sts FROM events ORDER BY id")]
    assert [(r["frm"], r["sts"]) for r in rows] == [("pending", "downloading")]


def test_entering_searching_is_dropped_but_its_outcome_is_kept(app_env):
    """Every pending record is claimed into `searching` and out again on every sweep, so logging
    the entry buries the log — but the EXIT is the outcome of the search and is the whole reason
    to look at it. The first version of this guard tested that both ends were `searching`, a
    condition no real transition can satisfy, so it skipped nothing at all."""
    core = app_env
    _movie(core)
    core.set_status(1, "searching", progress="looking")   # dropped
    core.set_status(1, "no_release")                      # kept: this is the answer
    with core.db() as c:
        rows = [(r["frm"], r["sts"]) for r in c.execute("SELECT frm, sts FROM events ORDER BY id")]
    assert rows == [("searching", "no_release")]


def test_a_guarded_write_logs_the_status_it_actually_matched(app_env):
    """`expect=` and `claim_*` both make the UPDATE conditional on a status, so that status is
    true by construction. The SELECT that would otherwise supply it is a separate statement, and
    the merge worker commits between the two often enough to matter."""
    core = app_env
    _movie(core)
    core.set_status(1, "ready")
    assert core.claim_movie(1, "ready", "merging") is True
    with core.db() as c:
        last = c.execute("SELECT frm, sts FROM events ORDER BY id DESC LIMIT 1").fetchone()
    assert (last["frm"], last["sts"]) == ("ready", "merging")


def test_history_survives_the_record_being_pruned(app_env):
    """Titles are denormalised into the event row on purpose. `prune_library` deletes records for
    files that have left the library, and a chart that silently loses its early months because
    the underlying rows were tidied up is worse than no chart."""
    core = app_env
    _movie(core)
    core.set_status(1, "merged", merge_kind="grafted", added_langs="eng")
    with core.db() as c:
        c.execute("DELETE FROM movies WHERE tmdb_id=1")
    from app import history
    rows = history.events(limit=10)["items"]
    assert rows and rows[0]["title"] == "Film"
    assert rows[0]["tag"] == "grafted"


def test_merge_kind_reaches_the_event_so_a_re_read_is_not_a_merge(app_env):
    """`merged` is the terminal state of three outcomes and only two are work vo-merge did. A
    library re-read closing out thousands of already-correct files must not draw as thousands of
    merges — the exact mistake "Recently merged" was rebuilt to avoid."""
    core = app_env
    _movie(core, tmdb_id=1)
    _movie(core, tmdb_id=2)
    core.set_status(1, "merged", merge_kind="grafted", added_langs="eng")
    core.set_status(2, "merged", merge_kind="already", added_langs="")
    from app import history
    thru = history.throughput_series(2)
    assert sum(d["grafted"] for d in thru) == 1
    assert sum(d["already"] for d in thru) == 1
    assert sum(d["merged"] for d in thru) == 1, "'already' must not count as a merge"


def test_a_day_with_no_coverage_sample_is_a_hole_not_a_zero(app_env):
    """Nobody probed the library that day; the library did not become 0% complete. Plotting a
    missing sample at the baseline shows a library that collapsed and recovered overnight."""
    core = app_env
    core.snapshot_coverage({"total": 100, "complete": 60, "unreadable": 0, "libs": {}})
    from app import history
    series = history.coverage_series(5)
    assert len(series) == 5
    assert [p["pct"] for p in series[:-1]] == [None, None, None, None]
    assert series[-1]["pct"] == 60.0


def test_a_day_with_no_events_really_is_zero(app_env):
    """The mirror of the rule above, and the reason the two series are built separately: leaving
    quiet days out of the throughput chart would compress the axis and turn a quiet week into a
    vertical cliff."""
    from app import history
    thru = history.throughput_series(7)
    assert len(thru) == 7
    assert all(d["merged"] == 0 for d in thru)


def test_the_trend_line_and_the_coverage_panel_agree(app_env):
    """Both divide by every probed file, unreadable included. Excluding them from the trend reads
    better — the chart can reach 100% — but it made the line and the panel directly above it show
    different percentages for the same library on the same day, with nothing to say which was
    right. /api/coverage is the incumbent; this follows it."""
    core = app_env
    core.snapshot_coverage({"total": 100, "complete": 90, "unreadable": 10, "libs": {}})
    from app import history
    assert history.coverage_series(1)[0]["pct"] == 90.0


def test_the_history_sampler_classifies_exactly_like_the_coverage_endpoint(app_env):
    """The reason inventory.py exists. An earlier version of `totals` conditioned the subtitle
    shortfall on `subs_only_gap`, so with that setting off the daily sample and the coverage panel
    disagreed about how many files were complete."""
    from app import core, inventory
    core.put_probe("/media/Films/A/A.mkv", auds="fre,eng", subs="fre", ntracks=2)   # subs-only gap
    core.put_probe("/media/Films/B/B.mkv", auds="fre", subs="fre,eng", ntracks=1)   # audio gap
    for flag in (True, False):
        cfg = dict(core.DEFAULTS, media_mount="/media", subs_only_gap=flag)
        t = inventory.totals(cfg)
        # Mirror of main.coverage's elif chain, evaluated over the same generator.
        exp = {"complete": 0, "missing_audio": 0, "missing_subs": 0, "missing_both": 0}
        for _r, _top, _k, _wa, _ws, _ha, _hs, ma, ms in inventory.build(cfg):
            if ma is None:
                continue
            exp["missing_both" if (ma and ms) else "missing_audio" if ma
                else "missing_subs" if ms else "complete"] += 1
        assert {k: t[k] for k in exp} == exp, f"disagreed with subs_only_gap={flag}"


def test_snapshotting_twice_in_a_day_refreshes_rather_than_double_counts(app_env):
    """Every rescan samples, and an operator can run several a day. Keyed by day so a busy day
    cannot weigh more than a quiet one."""
    core = app_env
    core.snapshot_coverage({"total": 10, "complete": 1, "unreadable": 0, "libs": {}})
    core.snapshot_coverage({"total": 10, "complete": 4, "unreadable": 0, "libs": {}})
    with core.db() as c:
        assert c.execute("SELECT COUNT(*) n FROM coverage_history").fetchone()["n"] == 1
    from app import history
    assert history.coverage_series(1)[0]["complete"] == 4


def test_pruning_the_log_respects_the_retention_window(app_env):
    core = app_env
    core.log_event("movie", 1, "pending", "error", "Old", None, "boom")
    with core.db() as c:
        c.execute("UPDATE events SET ts = ?", (time.time() - 200 * 86400,))
    core.log_event("movie", 2, "pending", "error", "New", None, "boom")
    assert core.prune_events(90) == 1
    from app import history
    assert history.events()["total"] == 1


# ================================================================= the failure taxonomy
# The literal strings pipeline.py and tv.py write. When one of those changes this test fails,
# which is the point: a taxonomy that has silently stopped matching production is worse than no
# taxonomy, because the page keeps looking authoritative.
LIVE_ERRORS = [
    ("sync_fail", "framerates differ (25.0 donor vs 23.976 library) and the rate test could not "
                  "confirm a stretch; the arithmetic ratio is 1.0427083 "
                  '(POST /set_sync {"drift": 1.0427083} to apply it)', "rate_mismatch"),
    ("sync_fail", "framerates differ but no reliable drift could be measured", "rate_unmeasured"),
    ("sync_fail", "framerate differs (25.0 vs 23.976), auto-sync off", "fps_autosync_off"),
    ("review", "post-merge QC: grafted audio misaligned by +1800ms (conf 0.71)", "qc_rejected"),
    ("review", "post-merge lip-sync: grafted audio is -900ms out against the picture (conf 0.52)",
     "qc_rejected"),
    ("review", "couldn't sync (low-confidence sync); no compatible release after 4 tries",
     "different_cut"),
    ("sync_fail", "low-confidence sync", "different_cut"),
    ("error", "download has S01E01-E06, this needs S01E138-E145", "numbering"),
    ("sync_fail", "donor file vanished before the merge (not on disk, and qB no longer has it)",
     "donor_gone"),
    ("error", "merge interrupted and source file missing", "donor_gone"),
    ("sync_fail", "donor unreadable (probe failed)", "donor_unreadable"),
    ("sync_fail", "download complete but no video file", "donor_unreadable"),
    ("sync_fail", "release carries none of the missing languages (still needs eng)",
     "useless_release"),
    ("sync_fail", "release adds nothing this file needs (file still needs nothing)",
     "useless_release"),
    ("error", "mux failed: mkvmerge rc=2: no space left on device", "mux_failed"),
    ("error", "multi remux failed: mkvmerge rc=2", "mux_failed"),
    ("error", "download complete but /downloads/x never became visible", "not_visible"),
    ("no_release", "all candidate releases stalled after 4 tries", "stalled"),
    ("no_release", "all releases stalled", "stalled"),
    ("error", "download complete, but its torrent is not in the audio-merge-tv category in qB "
              "(hash abc123) — reconciling — stuck for 30 min", "stalled"),
    ("error", "merge: library file missing on disk", "library_missing"),
    ("error", "merge: missing french_path", "library_missing"),
    # `transient()` appends "— N consecutive attempts" to whatever gave up. The underlying reason
    # is the useful grouping, so an exhausted probe failure lands with the other probe failures
    # and only the shapes with no better home ("grab: …") fall through to `transient`.
    ("error", "merge: library file probe failed — 3 consecutive attempts", "probe_failed"),
    ("error", "grab: HTTPSConnectionPool timed out — 5 consecutive attempts", "transient"),
    ("error", "resync: no confident alignment (0.12) — set offset manually", "resync_unaligned"),
    ("error", "resync: need two distinct audio tracks", "probe_failed"),
    ("error", "finish: [Errno 13] Permission denied", "finish_failed"),
    ("review", "merge aborted by the operator (abort)", "operator"),
    ("review", "taken off the merge queue by the operator", "operator"),
    ("no_release", None, "no_release"),
    ("review", None, "needs_decision"),
    ("error", "merge: something nobody has seen before", "unknown"),
]


@pytest.mark.parametrize("status,error,code", LIVE_ERRORS)
def test_every_error_the_pipeline_writes_lands_in_the_right_group(app_env, status, error, code):
    from app import problems
    assert problems.classify(status, error) == code


@pytest.mark.parametrize("reason,code", [
    ("donor file vanished before the merge", "donor_gone"),
    ("donor unreadable (probe failed)", "donor_unreadable"),
    ("release carries none of the missing languages (still needs eng)", "useless_release"),
    ("mux failed: mkvmerge rc=2", "mux_failed"),
    ("couldn't sync (low-confidence sync)", "different_cut"),
])
def test_an_exhausted_failure_keeps_its_own_cause(app_env, reason, code):
    """Both retry paths compose the final message as
    f"{reason}; no compatible release after {n} tries" — that suffix rides on EVERY exhausted
    failure whatever caused it. Matching it in the different-cut rule swallowed four unrelated
    causes into one group and offered each of them a sync remedy that could not help."""
    from app import problems
    assert problems.classify("sync_fail", f"{reason}; no compatible release after 4 tries") == code


def test_the_drill_down_hands_the_ui_usable_remedies(app_env):
    """`groups()` expands each remedy code into {code,label,bulk,slow,note}; the drill-down used
    to return the raw rule, whose `remedies` are bare strings. The page filters on `r.bulk`, which
    a string does not have — so every bulk button on that view silently rendered nothing."""
    core = app_env
    _movie(core)
    core.set_status(1, "sync_fail", error="donor file vanished before the merge")
    from app import problems
    rule = problems.records("donor_gone")["rule"]
    assert rule and rule["remedies"], "no remedies returned"
    for r in rule["remedies"]:
        assert isinstance(r, dict) and {"code", "label", "bulk", "note"} <= set(r)


def test_picking_another_release_keeps_a_donor_a_pack_mate_still_needs(app_env, monkeypatch):
    """A season pack is ONE torrent behind many episodes. Deleting it with its files for one of
    them destroys the donor for every episode still waiting on it — and this remedy is offered in
    bulk, so it would do it repeatedly. Same guard tv.retry_episode uses."""
    core = app_env
    from app import problems
    for e in (1, 2):
        core.upsert_episode(dict(id=f"7:1:{e}", series_id=7, series_title="Show", tvdb_id=1,
                                 season=1, episode=e, french_path=f"/m/S01E0{e}.mkv",
                                 quality="WEB", poster=None, series_type="standard",
                                 orig_lang="eng"))
        core.set_ep_status(f"7:1:{e}", "sync_fail", dl_hash="deadbeef",
                           error="donor unreadable (probe failed)")
    deleted = []
    monkeypatch.setattr(problems, "_apply_one", problems._apply_one)     # keep the real one

    class FakeQB:
        def __init__(self, *a, **k): pass
        def login(self): pass
        def delete(self, hashes, delete_files=False): deleted.extend(hashes)

    import app.clients
    monkeypatch.setattr(app.clients, "QBittorrent", FakeQB)
    cfg = dict(core.DEFAULTS)
    rec = [r for r in problems._rows() if r["key"] == "7:1:1"][0]
    ok, msg = problems._apply_one("another", rec, cfg, {})
    assert ok and deleted == [], f"deleted a shared pack donor: {msg}"

    # ...and once no pack-mate needs it, the torrent may go. (`another` moved E01 to `pending`,
    # which is not a failing state, so put it back where the remedy can find it.)
    core.set_ep_status("7:1:2", "merged")
    core.set_ep_status("7:1:1", "sync_fail", dl_hash="deadbeef",
                       error="donor unreadable (probe failed)")
    rec = [r for r in problems._rows() if r["key"] == "7:1:1"][0]
    problems._apply_one("another", rec, cfg, {})
    assert deleted == ["deadbeef"]


def test_apply_drift_refuses_a_ratio_that_is_not_a_rate(app_env):
    """`drift` can arrive from the request body, so without this the bulk path is a way around
    the 0.9-1.11 guard the single-record endpoint applies — across a whole group at once."""
    core = app_env
    _movie(core)
    core.set_status(1, "sync_fail", error="the arithmetic ratio is 1.0427083")
    from app import problems
    rec = problems._rows()[0]
    ok, msg = problems._apply_one("apply_drift", rec, dict(core.DEFAULTS), {"drift": 4.0})
    assert not ok and "not a rate ratio" in msg


def test_the_pal_ratio_is_read_back_out_of_the_message(app_env):
    """`_sync_fail_reason` puts the arithmetic ratio in the error precisely so the next actor does
    not have to re-derive it. Reading it back is what turns the largest 'impossible' bucket into a
    one-click fix for a whole season."""
    from app import problems
    err = ("framerates differ (25.0 donor vs 23.976 library) and the rate test could not confirm "
           'a stretch; the arithmetic ratio is 1.0427083 (POST /set_sync {"drift": 1.0427083})')
    assert problems.drift_of(err) == pytest.approx(1.0427083)
    assert problems.drift_of("couldn't sync (low-confidence sync)") is None
    # Same guard main._set_sync applies: a ratio that isn't a rate ratio is not offered at all.
    assert problems.drift_of("the arithmetic ratio is 4.0") is None


def test_a_group_only_offers_a_remedy_it_can_actually_take(app_env):
    """A button that does nothing is worse than no button. `apply_drift` is only offered when at
    least one record in the group actually quotes a ratio."""
    core = app_env
    _movie(core)
    core.set_status(1, "sync_fail", error="low-confidence sync")
    from app import problems
    g = {x["code"]: x for x in problems.groups()["groups"]}
    assert "different_cut" in g
    assert "apply_drift" not in [r["code"] for r in g["different_cut"]["remedies"]]

    core.set_status(1, "sync_fail",
                    error="framerates differ (25.0 donor vs 23.976 library) and the rate test "
                          "could not confirm a stretch; the arithmetic ratio is 1.0427083")
    g = {x["code"]: x for x in problems.groups()["groups"]}
    assert [r["code"] for r in g["rate_mismatch"]["remedies"]][0] == "apply_drift"
    assert g["rate_mismatch"]["drifts"] == [1.0427083]


def test_a_partial_selection_is_never_widened_to_the_whole_group(app_env):
    """The difference between retrying six records and retrying four hundred. Explicit keys must
    win over the group code."""
    core = app_env
    for i in (1, 2, 3):
        _movie(core, tmdb_id=i)
        core.set_status(i, "sync_fail", error="low-confidence sync")
    from app import problems
    assert len(problems.targets_for(code="different_cut")) == 3
    picked = problems.targets_for(code="different_cut", keys=["movie:2"])
    assert [t["key"] for t in picked] == ["2"]


def test_fixable_groups_sort_ahead_of_the_judgement_backlog(app_env):
    """The page exists to empty the mechanical bucket. Burying it under a large judgement group is
    how a backlog stops looking actionable."""
    core = app_env
    for i in range(1, 6):
        _movie(core, tmdb_id=i)
        core.set_status(i, "sync_fail", error="low-confidence sync")
    _movie(core, tmdb_id=9)
    core.set_status(9, "sync_fail", error="donor file vanished before the merge")
    from app import problems
    codes = [g["code"] for g in problems.groups()["groups"]]
    assert codes[0] == "donor_gone", "one fixable record outranks five judgement calls"


# ================================================================= the settings schema
def test_every_config_key_is_described(app_env):
    """A key in DEFAULTS with no entry in settings_meta is invisible in the UI and reachable only
    by hand-editing config.json on the host — which is the exact failure the schema exists to end.
    A stale entry is the mirror: a field rendering a key that no longer exists."""
    from app import settings_meta
    undescribed, stale = settings_meta.missing()
    assert undescribed == [], f"config keys with no description: {undescribed}"
    assert stale == [], f"described keys that no longer exist: {stale}"


def test_the_schema_never_leaks_a_secret(app_env):
    """The schema carries CURRENT values so the form can render them. It must take them from the
    already-masked config, not re-read the raw one — `plex2_token` was returned in cleartext
    beside five masked siblings once already."""
    from app import core, settings_meta
    cfg = dict(core.DEFAULTS, prowlarr_key="REALKEY", plex2_token="REALTOKEN", qb_pass="hunter2")
    masked = {k: (bool(v) if k in core._SECRET_KEYS and k != "qb_pass" else
                  ("********" if k == "qb_pass" else v)) for k, v in cfg.items()}
    out = settings_meta.schema(cfg, masked=masked)
    values = {f["key"]: f["value"] for s in out["sections"] for f in s["fields"]}
    assert values["prowlarr_key"] is True
    assert values["plex2_token"] is True
    assert values["qb_pass"] == "********"
    assert "REALKEY" not in str(out) and "REALTOKEN" not in str(out)


def test_secret_fields_are_flagged_so_the_form_cannot_save_a_mask_back(app_env):
    """A masked secret comes back as a boolean. Sending it back would overwrite the real value
    with `True`. The form keys off this flag, so it has to be set on every secret."""
    from app import core, settings_meta
    fields = settings_meta.fields()
    for k in core._SECRET_KEYS:
        if k in ("webhook_token",):        # deliberately visible: it is part of the webhook URL
            continue
        assert fields[k].get("secret"), f"{k} is a secret but the schema does not say so"


# ================================================================= lip-sync
def _signals(displacement_frames, fps=12, wdur=20, lag_s=4.0, seed=7):
    """A picture window and the padded audio envelope around it, with the audio displaced by a
    known number of frames. `displacement_frames > 0` = the audio sits LATE."""
    rng = np.random.default_rng(seed)
    lagf = int(lag_s * fps)
    n = wdur * fps
    # One long "story" of speech activity; the picture reads one slice of it, the audio another.
    story = np.clip(rng.normal(0, 1, n + 6 * lagf), 0, None)
    base = 2 * lagf                                   # where the picture window sits in the story
    vis = story[base: base + n].copy()
    # env covers [picture_start - lag, picture_start + n + lag] in PICTURE time; audio content is
    # displaced, so it is read from the story shifted by -displacement.
    start = base - lagf - displacement_frames
    env = story[start: start + n + 2 * lagf + 1].copy()
    return vis, env, fps, lag_s


def test_lipsync_reports_the_correction_not_the_displacement(app_env):
    """The sign convention every consumer depends on: the value is what `--sync` should ADD to
    the audio's timestamps. Audio that plays late must come back NEGATIVE, or applying it would
    double the error instead of cancelling it."""
    from app import lipsync
    for frames in (9, -9, 0, 24):
        vis, env, fps, lag = _signals(frames)
        off, conf = lipsync._xcorr(vis, env, fps, lag)
        assert conf > 0.9, f"synthetic signals should correlate strongly (got {conf:.2f})"
        assert off == pytest.approx(-frames * 1000.0 / fps, abs=1.0), \
            f"displacement {frames} frames should correct by {-frames * 1000.0 / fps:.0f}ms"


def test_lipsync_searches_the_full_padded_range(app_env):
    """The search range comes from how much extra AUDIO was decoded, not from the video window.
    A displacement near the edge of the pad must still be found — that is what makes a ±60s
    rescue affordable."""
    from app import lipsync
    vis, env, fps, lag = _signals(int(3.5 * 12))          # 3.5s late, pad is 4s
    off, conf = lipsync._xcorr(vis, env, fps, lag)
    assert conf > 0.9
    assert off == pytest.approx(-3500, abs=100)


def test_lipsync_does_not_invent_an_offset_from_unrelated_signals(app_env):
    """The failure mode that matters: a confident-looking number where there is no relationship.
    The correlation is normalised against the LOCAL audio norm, so unrelated signals must not
    clear the floor at any lag — including a lag that happens to sit under a loud passage."""
    from app import lipsync
    rng = np.random.default_rng(11)
    fps, lag = 12, 4.0
    lagf = int(fps * lag)
    for i in range(25):
        vis = np.clip(rng.normal(0, 1, 240), 0, None)
        env = np.clip(rng.normal(0, 1, 240 + 2 * lagf + 1), 0, None)
        if i % 3 == 0:
            env[100:140] *= 12          # a loud passage: must not become the answer
        _off, conf = lipsync._xcorr(vis, env, fps, lag)
        assert conf < 0.30, f"unrelated signals cleared the confidence floor ({conf:.2f})"


def test_lipsync_locates_the_mouth_by_its_syllable_rate(app_env):
    """The dependency-free region search. A patch modulating at 4 Hz (speech) must beat a patch
    with more total motion at 0.4 Hz (a pan, a flicker) — otherwise the strongest 'speaker' in
    frame is whatever moves most, which is never the mouth."""
    from app import lipsync
    fps, n, h, w = 12, 120, 24, 32
    t = np.arange(n) / fps
    frames = np.zeros((n, h, w))
    frames[:, 4:8, 4:8] += (2.0 * np.sin(2 * np.pi * 0.4 * t))[:, None, None]   # slow, strong
    frames[:, 16:20, 20:24] += (0.5 * np.sin(2 * np.pi * 4.0 * t))[:, None, None]  # syllable rate
    sig, face = lipsync._visual_signal(frames, fps)
    assert not face                                  # no cascade in play here
    spec = np.abs(np.fft.rfft(sig - sig.mean()))
    freqs = np.fft.rfftfreq(len(sig), 1.0 / fps)
    peak = freqs[int(np.argmax(spec[1:])) + 1]
    assert 2.0 <= peak <= 8.0, f"picked up a {peak:.1f} Hz region instead of the talking one"


def test_lipsync_decodes_on_the_igpu_and_downscales_there(app_env):
    """`fps=` and `scale=` are FILTERS — they run after the decoder, so asking for 128x72 at 12fps
    does NOT make the decode cheap: every frame of the window is still decoded at full resolution.
    Measured on this content, the identical 128x72 output costs 7x more CPU from a 1080p source
    and 30x more from a 4K one. That is exactly the work `-hwaccel vaapi` + `scale_vaapi` moves
    off the CPU, and the first version of this module skipped it on the reasoning that tiny output
    frames must mean a cheap decode."""
    from app import core, lipsync
    seen = {}

    def fake(cmd, **kw):
        seen["cmd"] = cmd
        raise AssertionError("not reached")

    monkey = getattr(core, "run_proc")
    core.run_proc = fake
    try:
        try:
            lipsync._decode("/x.mkv", 0, 8, 12, 128, 72, 4, "vaapi", "/dev/dri/renderD128", 60)
        except AssertionError:
            pass
        cmd = " ".join(seen["cmd"])
        assert "-hwaccel vaapi" in cmd
        assert "-hwaccel_output_format vaapi" in cmd
        # The downscale must happen ON the GPU: without scale_vaapi before hwdownload, full-size
        # surfaces cross PCIe to be scaled on the CPU, which is most of the win thrown away.
        assert "scale_vaapi=128:72,hwdownload" in cmd
        assert cmd.index("scale_vaapi") < cmd.index("hwdownload")
    finally:
        core.run_proc = monkey


def test_lipsync_never_retries_a_timed_out_decode_in_software(app_env):
    """A hung decode is the one failure the software fallback must NOT answer: it would hang for
    exactly as long a second time, and the merge worker is already holding its slot. Same contract
    as offdet_video, which learned it first."""
    import subprocess as sp
    from app import core, lipsync, offdet_video
    calls = []

    def fake(cmd, **kw):
        calls.append(cmd)
        raise sp.TimeoutExpired(cmd, kw.get("timeout"))

    monkey = core.run_proc
    core.run_proc = fake
    try:
        frames, rc = lipsync._decode("/x.mkv", 0, 8, 12, 128, 72, 4, "vaapi", "/dev/dri/r", 1)
        assert rc == offdet_video.TIMEOUT_RC
        lipsync._frames("/x.mkv", 0, 8, 12, cfg={"sync_hwaccel": "vaapi"}, timeout=1)
    finally:
        core.run_proc = monkey
    # one attempt inside _decode, one inside _frames — and crucially no software retry after either
    assert len(calls) == 2, f"a timed-out decode was retried: {len(calls)} attempts"


def test_lipsync_falls_back_to_software_when_the_igpu_fails(app_env):
    """A box with no /dev/dri, or a wedged driver, must still get an answer — just slower."""
    from app import core, lipsync
    import numpy as np
    calls = []

    def fake_decode(path, start, dur, fps, w, h, threads, hwaccel, device, timeout):
        calls.append(hwaccel)
        if hwaccel:                       # pretend VAAPI is unavailable
            return np.zeros((0, h, w)), 1
        return np.ones((40, h, w)), 0

    monkey = lipsync._decode
    lipsync._decode = fake_decode
    try:
        out = lipsync._frames("/x.mkv", 0, 8, 12, cfg={"sync_hwaccel": "vaapi"}, timeout=60)
    finally:
        lipsync._decode = monkey
    assert calls == ["vaapi", None]
    assert out.shape[0] == 40


def test_lipsync_rejects_windows_that_disagree(app_env):
    """The consensus tolerance is ABSOLUTE. It was briefly scaled by the spread of the very
    disagreement it exists to detect — so the wilder the windows disagreed the more generous it
    became, and it could never reject anything."""
    from app import core, lipsync
    import numpy as np
    cfg = dict(core.DEFAULTS, lipsync_min_windows=3, lipsync_min_conf=0.1)
    # Windows that resolved confidently but landed thousands of ms apart: not one displacement.
    wins = [{"start": i * 10.0, "offset_ms": v, "conf": 0.8, "used": True, "why": None}
            for i, v in enumerate([-2000, -100, 900, 2400, 5000])]
    used = [w for w in wins if w["used"]]
    offs = np.array([w["offset_ms"] for w in used], float)
    med = float(np.median(offs))
    agree = [w for w in used if abs(w["offset_ms"] - med) <= lipsync.AGREE_TOL_MS]
    assert len(agree) < cfg["lipsync_min_windows"], "scattered windows were accepted as agreeing"
    # ...while windows within a couple of frames of each other still are.
    tight = [-40, 10, 0, 55, -20]
    med2 = float(np.median(tight))
    assert len([v for v in tight if abs(v - med2) <= lipsync.AGREE_TOL_MS]) == 5


def test_applying_a_lipsync_offset_clears_any_stale_rate_stretch(app_env):
    """A lip-sync reading is a CONSTANT offset. `sync_manual=1` makes the merge apply what it is
    given verbatim, so a drift left over from a previous attempt would be applied on top of it —
    the same trap DONOR_RESET exists for."""
    core = app_env
    _movie(core)
    core.set_status(1, "review", sync_drift=1.0427083, sync_offset_ms=250, sync_manual=1,
                    french_path="/nope.mkv")
    from app import lipsync
    src = __import__("inspect").getsource(lipsync.remedy)
    assert "sync_drift=None" in src, "remedy() must clear sync_drift when it sets an offset"


def test_lipsync_refuses_a_file_too_short_to_sample(app_env):
    """An honest "no reading" beats a number from two windows that overlap."""
    from app import core, lipsync
    out = lipsync.measure("/nonexistent.mkv", 0, dur=10, cfg=core.DEFAULTS)
    assert out["offset_ms"] is None and "too short" in out["note"]


def test_lipsync_qc_accepts_when_it_cannot_tell(app_env):
    """Inconclusive ACCEPTS, exactly like the existing post-merge QC: absence of evidence is not
    evidence of misalignment, and a quiet film must not burn its retry budget on a measurement
    that was never going to resolve."""
    from app import core, lipsync
    cfg = dict(core.DEFAULTS, lipsync_qc=True)
    ok, off, conf = lipsync.verify_graft("/nonexistent.mkv", 1, cfg)
    assert ok is True and off == 0


def test_lipsync_qc_is_off_unless_asked_for(app_env):
    """It costs a second decode pass per merge. The default must not silently slow every merge."""
    from app import core, lipsync
    assert core.DEFAULTS["lipsync_qc"] is False
    ok, _off, _conf = lipsync.verify_graft("/whatever.mkv", 1, core.DEFAULTS)
    assert ok is True


def test_lipsync_rescue_is_skipped_when_a_stretch_is_needed(app_env):
    """Lip-sync returns ONE number, and a constant cannot correct a rate difference. Applying it
    to a PAL pair would be the exact mistake the `fps_diff and not drift` guard exists to
    prevent."""
    from app import core, pipeline
    assert pipeline.lipsync_rescue("/a.mkv", "/b.mkv", 100, core.DEFAULTS, fps_diff=True) is None


# ================================================================= the shared classifier
def test_coverage_totals_partition_the_library(app_env):
    """Every probed file is complete, short of audio, short of subs, short of both, or
    unreadable. There is deliberately no sixth 'not targeted' bucket — that concept is what made
    Films report 2,041 files against 2,429 on disk."""
    from app import core, inventory
    cfg = dict(core.DEFAULTS, media_mount="/media", anime_dirs=["Anime"], series_dirs=["Series"])
    core.put_probe("/media/Films/A/A.mkv", auds="fre,eng", subs="fre,eng", ntracks=2)
    core.put_probe("/media/Films/B/B.mkv", auds="fre", subs="fre,eng", ntracks=1)
    core.put_probe("/media/Films/C/C.mkv", auds="fre,eng", subs="fre", ntracks=2)
    core.put_probe("/media/Films/D/D.mkv", auds="fre", subs="", ntracks=1)
    core.put_probe("/media/Films/E/E.mkv", err="no audio track")
    t = inventory.totals(cfg)
    assert t["total"] == 5
    assert (t["complete"], t["missing_audio"], t["missing_subs"], t["missing_both"],
            t["unreadable"]) == (1, 1, 1, 1, 1)
    assert (t["complete"] + t["missing_audio"] + t["missing_subs"] + t["missing_both"]
            + t["unreadable"]) == t["total"]
    assert t["libs"]["Films"] == {"total": 5, "complete": 1}
