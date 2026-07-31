"""Regression tests for defects found in the July 2026 review.

Each test is named for the behaviour it locks down and says which failure it prevents. Several
of these bugs were silent in production — a desynced file marked `merged`, a secrets file served
over HTTP — so a test that fails loudly is the whole point.
"""
import json
import os
import subprocess
import sys
import threading

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
    return core


# ------------------------------------------------------------------ finding 01
def test_static_path_containment_rejects_absolute_paths(tmp_path):
    """`os.path.join(STATIC, "/config/config.json")` returns /config/config.json — the join
    discards its left operand — so the SPA route served the API keys, the qB password and both
    Plex tokens to anyone who asked. Reproduced against a live server before the fix."""
    from app.main import _under
    static = tmp_path / "static"; static.mkdir()
    (static / "index.html").write_text("spa")
    secret = tmp_path / "config.json"; secret.write_text('{"prowlarr_key": "leak"}')

    assert _under(str(static / "index.html"), str(static)) is True
    assert _under(str(secret), str(static)) is False, "absolute path escaped the static root"
    assert _under("/etc/passwd", str(static)) is False
    assert _under(str(static / ".." / "config.json"), str(static)) is False


def test_donor_must_live_under_a_download_root(app_env):
    """/episode/{id}/assign took any absolute path, so a LIBRARY file could be named as the donor
    for an unrelated episode — whose own library file is then replaced by the merge of the two."""
    from app import main
    roots = main._donor_roots(dict(app_env.DEFAULTS))
    donor = "/media/.Téléchargements/completed/audio-merge-tv/9_S01/ep.mkv"
    library = "/media/Anime/Blue Lock/Season 01/ep.mkv"
    assert any(main._under(donor, r) for r in roots)
    assert not any(main._under(library, r) for r in roots)


# ------------------------------------------------------------------ finding 02
def test_a_measured_offset_is_not_replayed_onto_the_next_donor(app_env):
    """The worst failure mode in the app: a merge stored sync_offset_ms, nothing cleared it, and
    a stored offset skipped detection entirely — so a re-opened record muxed a completely
    different donor with the previous donor's shift and reported success."""
    from app import pipeline
    app_env.upsert_movie({"tmdb_id": 1, "imdb_id": "tt1", "radarr_id": 1, "title": "T",
                          "original_title": "T", "year": 2020, "original_lang": "french",
                          "french_path": "/x.mkv", "quality": "1080p"})
    # a successful merge records what it measured
    app_env.set_status(1, "merged", sync_offset_ms=38000, sync_drift=None)
    assert app_env.get_movie(1)["sync_offset_ms"] == 38000
    assert not app_env.get_movie(1)["sync_manual"], "a measurement is not an instruction"

    # the record is sent back for a different release
    app_env.set_status(1, "pending", **pipeline.DONOR_RESET)
    mv = app_env.get_movie(1)
    assert mv["sync_offset_ms"] == 0 and not mv["sync_manual"] and mv["dl_hash"] is None


def test_a_deliberate_offset_survives_and_is_marked_manual(app_env):
    """/set_sync exists so the AI can apply a known offset; that one MUST skip detection."""
    app_env.upsert_movie({"tmdb_id": 2, "imdb_id": "tt2", "radarr_id": 2, "title": "T",
                          "original_title": "T", "year": 2020, "original_lang": "french",
                          "french_path": "/x.mkv", "quality": "1080p"})
    app_env.set_status(2, "pending", sync_offset_ms=250, sync_drift=1.0427083, sync_manual=1)
    mv = app_env.get_movie(2)
    assert mv["sync_manual"] == 1 and mv["sync_offset_ms"] == 250


def test_donor_reset_covers_every_donor_field(app_env):
    """Kept as one constant because the sync fields were exactly the ones each retry path forgot.
    `transient_fails` rides along: a fresh run gets a fresh infrastructure-failure budget."""
    from app import pipeline
    assert set(pipeline.DONOR_RESET) == {"dl_hash", "dl_id", "en_file",
                                         "sync_offset_ms", "sync_drift", "sync_manual",
                                         "transient_fails"}


# ------------------------------------------------------------------ finding 03 / 13
def test_a_scan_cannot_resurrect_a_record_the_worker_claimed(app_env):
    """The scan reads a status, decides, then writes back — and the worker can claim
    ready -> merging in between. Writing unconditionally put `ready` back underneath a running
    merge, and the record was merged a second time into the same output path."""
    app_env.upsert_movie({"tmdb_id": 3, "imdb_id": "tt3", "radarr_id": 3, "title": "T",
                          "original_title": "T", "year": 2020, "original_lang": "french",
                          "french_path": "/x.mkv", "quality": "1080p"})
    app_env.set_status(3, "ready")
    assert app_env.claim_movie(3, "ready", "merging") is True        # the worker wins the race
    applied = app_env.set_status(3, "ready", expect="ready", audio_langs="fre")
    assert applied is False
    assert app_env.get_movie(3)["status"] == "merging"


def test_claim_is_exclusive_under_concurrency(app_env):
    """Only one caller may take a queued item, or the same file is merged twice."""
    app_env.upsert_movie({"tmdb_id": 4, "imdb_id": "tt4", "radarr_id": 4, "title": "T",
                          "original_title": "T", "year": 2020, "original_lang": "french",
                          "french_path": "/x.mkv", "quality": "1080p"})
    app_env.set_status(4, "ready")
    wins = []
    def race():
        if app_env.claim_movie(4, "ready", "merging"):
            wins.append(1)
    ts = [threading.Thread(target=race) for _ in range(12)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert sum(wins) == 1


# ------------------------------------------------------------------ finding 05 / 06
def test_run_mux_removes_a_partial_output_on_failure(app_env, tmp_path):
    """Disk full gives rc>=2, and the half-written file used to be left inside the library folder
    where Plex indexes it — compounding the very disk-full that produced it, once per retry."""
    from app import pipeline
    out = str(tmp_path / "partial.mkv")
    ok, err = pipeline.run_mux(
        ["sh", "-c", f"echo partial > {out}; echo 'Error: disk full'; exit 2"], out, {})
    assert ok is False and "disk full" in err
    assert not os.path.exists(out)


def test_run_mux_keeps_output_on_warnings(app_env, tmp_path):
    """mkvmerge rc=1 means 'completed with warnings' and is a success."""
    from app import pipeline
    out = str(tmp_path / "warned.mkv")
    ok, err = pipeline.run_mux(["sh", "-c", f"echo muxed > {out}; exit 1"], out, {})
    assert ok is True and err is None and os.path.exists(out)


def test_run_mux_kills_and_cleans_up_on_timeout(app_env, tmp_path, monkeypatch):
    """An unbounded mux held the merge worker forever, and at max_parallel_merges=1 that stops
    ALL merging with nothing reporting it."""
    from app import pipeline
    out = str(tmp_path / "hung.mkv")
    with open(out, "w") as f:
        f.write("partial")

    def fake_run(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
    monkeypatch.setattr(pipeline.subprocess, "run", fake_run)
    ok, err = pipeline.run_mux(["mkvmerge"], out, {"mux_timeout_min": 240})
    assert ok is False and "exceeded 240min" in err
    assert not os.path.exists(out)


def test_every_subprocess_call_has_a_timeout():
    """The wedge this prevents is invisible: no error, no log line, merging simply stops."""
    import ast
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent / "app"
    missing = []
    for f in sorted(root.glob("*.py")):
        for node in ast.walk(ast.parse(f.read_text())):
            if isinstance(node, ast.Call) and \
               ast.unparse(node.func) in ("subprocess.run", "subprocess.check_output",
                                          "subprocess.call"):
                if not any(k.arg == "timeout" for k in node.keywords):
                    missing.append(f"{f.name}:{node.lineno}")
    assert not missing, f"subprocess calls with no timeout: {missing}"


# ------------------------------------------------------------------ finding 07 / 12
def test_every_secret_is_masked_or_explicitly_exempt(app_env):
    """The mask list was hand-written, so plex2_token — a real Plex account token — was returned
    in cleartext beside five siblings that were masked."""
    from app import main
    app_env.save_config({k: f"SECRET-VALUE-{k}" for k in app_env._SECRET_KEYS})
    masked = main.get_settings()
    for k in app_env._SECRET_KEYS:
        if k in main._VISIBLE_SECRETS:
            continue
        assert masked[k] != f"SECRET-VALUE-{k}", f"{k} leaked in cleartext"


def test_redact_scrubs_a_token_out_of_an_exception(app_env):
    """/api/test/plex returned str(e), and both ConnectionError and HTTPError quote the full URL
    — query string, X-Plex-Token and all."""
    app_env.save_config({"plex_token": "PLEX-TOK-ABCDEFGH"})
    app_env.load_config()                      # refresh the redaction set
    msg = "Max retries exceeded with url: /identity?X-Plex-Token=PLEX-TOK-ABCDEFGH"
    out = app_env.redact(msg)
    assert "PLEX-TOK-ABCDEFGH" not in out and "***" in out


# ------------------------------------------------------------------ finding 09
@pytest.mark.parametrize("best,want,why", [
    ((20, 0, "t", "l", "r"), False, "0 seeders — the old test compared SCORE against min_seeders"),
    ((100, 0, "t", "l", "r"), False, "0 seeders, high score"),
    ((40, 30, "t", "l", "r"), True, "low score but well seeded — see the note below"),
    ((65, 30, "t", "l", "r"), True, "clears the floor"),
    ((60, 5, "t", "l", "r"), True, "exactly at the floor"),
    ((60, 4, "t", "l", "r"), False, "one seeder short"),
    (None, False, "no result"),
])
def test_tv_usable_applies_the_seeder_floor(app_env, best, want, why):
    """Only the SEEDER floor, deliberately — `score_threshold` is calibrated against
    pipeline.score_release, which has bonuses this scorer lacks. See
    test_tv_usable_does_not_reject_an_ordinary_english_release."""
    from app import tv
    assert tv._usable(best, {"min_seeders": 5, "score_threshold": 60}) is want, why


def test_tv_multi_bonus_matches_the_rest_of_the_scorers(app_env):
    """+20 here against +200 everywhere else meant the one path that decides unattended barely
    expressed the preference the whole design is built on."""
    with open(os.path.join(os.path.dirname(__file__), "..", "app", "tv.py")) as f:
        src = f.read()
    assert "sc += 40 if subs_only else 200" in src


# ------------------------------------------------------------------ finding 11
def test_config_write_is_atomic_and_leaves_no_temp_file(app_env):
    app_env.save_config({"min_seeders": 9})
    assert app_env.load_config()["min_seeders"] == 9
    assert not os.path.exists(app_env.CONFIG_FILE + ".tmp")


def test_a_broken_config_is_never_overwritten(app_env):
    """load_config fell back to DEFAULTS silently, and the next save — which starts from that
    fallback — wrote the defaults back, erasing every URL and key the operator had entered."""
    app_env.save_config({"min_seeders": 9, "prowlarr_url": "http://real:9696"})
    with open(app_env.CONFIG_FILE, "w") as f:
        f.write('{"min_seeders": 9, TRUNCA')
    app_env.load_config()
    with pytest.raises(app_env.ConfigUnreadable):
        app_env.save_config({"min_seeders": 3})
    with open(app_env.CONFIG_FILE) as f:
        assert "TRUNCA" in f.read(), "the operator's file was destroyed"


# ------------------------------------------------------------------ finding 21
@pytest.mark.parametrize("data,want", [
    ({"search_interval_min": 0}, 1),          # 0 -> APScheduler fires every second
    ({"search_interval_min": "60"}, 60),      # numeric string coerced
    ({"max_parallel_merges": 0}, 1),          # 0 merges = nothing ever merges
])
def test_settings_are_coerced_and_clamped(app_env, data, want):
    from app import main
    assert list(main._validate_settings(data).values())[0] == want


def test_a_non_numeric_interval_is_rejected_before_it_is_persisted(app_env):
    """timedelta(minutes="abc") raised AFTER save_config had written it, so the next boot died
    inside scheduler.start() and the app never came up until config.json was hand-edited."""
    from fastapi import HTTPException
    from app import main
    with pytest.raises(HTTPException):
        main._validate_settings({"search_interval_min": "abc"})


def test_negative_and_zero_stay_allowed_where_they_mean_something(app_env):
    """no_release_retry_h: 0 = retry every scan, negative = never. Clamping those breaks them."""
    from app import main
    assert main._validate_settings({"no_release_retry_h": 0})["no_release_retry_h"] == 0
    assert main._validate_settings({"no_release_retry_h": -1})["no_release_retry_h"] == -1


# ------------------------------------------------------------------ finding 22 / 25 / 32
def test_tail_log_returns_the_last_lines_without_reading_the_whole_file(app_env):
    for i in range(5000):
        app_env.log(f"line {i}")
    tail = app_env.tail_log(5)
    assert len(tail) == 5 and "line 4999" in tail[-1]


def test_log_rotates_instead_of_growing_forever(app_env, monkeypatch):
    monkeypatch.setattr(app_env, "LOG_MAX_BYTES", 20000)
    for i in range(3000):
        app_env.log(f"line {i} " + "x" * 60)
    assert os.path.exists(app_env.LOG_FILE + ".1")
    assert os.path.getsize(app_env.LOG_FILE) < 60000


def test_hot_queries_use_an_index(app_env):
    """There were no indexes at all beyond the primary keys, so every `WHERE status=? ORDER BY
    updated DESC` was a full scan — run several times a minute by the background jobs."""
    with app_env.db() as c:
        plan = list(c.execute("EXPLAIN QUERY PLAN "
                              "SELECT * FROM episodes WHERE status=? ORDER BY updated DESC",
                              ("ready",)))
    assert "INDEX" in plan[0][-1].upper()


def test_concurrent_upserts_do_not_raise(app_env):
    """SELECT-then-INSERT raced: a webhook ingest and the scheduled scan reaching a new title at
    the same moment made the loser raise IntegrityError, aborting that whole scan pass."""
    errs = []
    def w():
        for _ in range(40):
            try:
                app_env.upsert_movie({"tmdb_id": 99, "imdb_id": "tt9", "radarr_id": 9,
                                      "title": "Race", "original_title": "Race", "year": 2020,
                                      "original_lang": "french", "french_path": "/r.mkv",
                                      "quality": "1080p"})
            except Exception as e:                      # noqa: BLE001 - recording it IS the test
                errs.append(e)
    ts = [threading.Thread(target=w) for _ in range(6)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert not errs


def test_upsert_never_disturbs_the_pipeline_status(app_env):
    app_env.upsert_movie({"tmdb_id": 5, "imdb_id": "tt5", "radarr_id": 5, "title": "A",
                          "original_title": "A", "year": 2020, "original_lang": "french",
                          "french_path": "/x.mkv", "quality": "1080p"})
    app_env.set_status(5, "downloading")
    app_env.upsert_movie({"tmdb_id": 5, "imdb_id": "tt5", "radarr_id": 5, "title": "B",
                          "original_title": "A", "year": 2020, "original_lang": "french",
                          "french_path": "/x.mkv", "quality": "1080p"})
    mv = app_env.get_movie(5)
    assert mv["status"] == "downloading" and mv["title"] == "B"


# ------------------------------------------------------------------ updated / merged_at
def test_updated_only_moves_on_a_real_state_change(app_env):
    """It is the sort key for Needs attention and the FIFO order of the merge queue, so a write
    that changes nothing must not reshuffle the dashboard."""
    app_env.upsert_movie({"tmdb_id": 6, "imdb_id": "tt6", "radarr_id": 6, "title": "T",
                          "original_title": "T", "year": 2020, "original_lang": "french",
                          "french_path": "/x.mkv", "quality": "1080p"})
    app_env.set_status(6, "error", error="boom")
    first = app_env.get_movie(6)["updated"]
    app_env.set_status(6, "error", ai_status="pending")      # the 3-minute sweep
    assert app_env.get_movie(6)["updated"] == first
    app_env.set_status(6, "pending")
    assert app_env.get_movie(6)["updated"] > first


def test_merged_at_is_stamped_once_on_the_transition(app_env):
    """Every later write with status='merged' — including a scan re-reading the file — used to
    reset it, floating old merges back into 'Recently merged'."""
    app_env.upsert_movie({"tmdb_id": 7, "imdb_id": "tt7", "radarr_id": 7, "title": "T",
                          "original_title": "T", "year": 2020, "original_lang": "french",
                          "french_path": "/x.mkv", "quality": "1080p"})
    app_env.set_status(7, "merged", merge_kind="grafted")
    first = app_env.get_movie(7)["merged_at"]
    assert first
    app_env.set_status(7, "merged", audio_langs="fre,eng")
    assert app_env.get_movie(7)["merged_at"] == first


# ------------------------------------------------------------------ second-pass review
# The fixes above were themselves reviewed, and these are the defects that review found in them.
# They are the subtler half: each one leaves the original bug technically fixed while restoring
# its effect through a path the first fix didn't consider.

def test_a_carried_out_manual_offset_does_not_persist_to_the_next_donor(app_env):
    """`sync_manual` gates detection, and nothing cleared it after the merge that consumed it —
    so one `/set_sync` kept skipping detection for every donor the record was ever given
    afterwards. The stale-offset bug, surviving behind a single manual fix."""
    app_env.upsert_movie({"tmdb_id": 20, "imdb_id": "tt20", "radarr_id": 20, "title": "T",
                          "original_title": "T", "year": 2020, "original_lang": "french",
                          "french_path": "/x.mkv", "quality": "1080p"})
    app_env.set_status(20, "pending", sync_offset_ms=38000, sync_manual=1)   # the AI's /set_sync
    # ...the merge that applies it must consume the instruction
    app_env.set_status(20, "merged", sync_offset_ms=38000, sync_manual=0, merge_kind="grafted")
    assert app_env.get_movie(20)["sync_manual"] == 0


def test_reopening_a_record_drops_the_donor_and_its_offset(app_env):
    """A scan re-opens `merged`/`no_release` by design. Without DONOR_RESET the record kept the
    previous donor's en_file AND its offset, which the next merge would apply blind."""
    from app import pipeline
    app_env.upsert_movie({"tmdb_id": 21, "imdb_id": "tt21", "radarr_id": 21, "title": "T",
                          "original_title": "T", "year": 2020, "original_lang": "french",
                          "french_path": "/x.mkv", "quality": "1080p"})
    app_env.set_status(21, "merged", en_file="/donor1.mkv", dl_hash="abc",
                       sync_offset_ms=38000, sync_manual=1)
    app_env.set_status(21, "pending", expect="merged", attempts=0, **pipeline.DONOR_RESET)
    mv = app_env.get_movie(21)
    assert mv["en_file"] is None and mv["sync_offset_ms"] == 0 and not mv["sync_manual"]


def test_tv_usable_does_not_reject_an_ordinary_english_release(app_env):
    """Applying the movie-calibrated `score_threshold` to the TV scorer was a regression: that
    scorer has no +60 resolution / +30 source / +80 id bonuses, so 60 demanded ~40 seeders of any
    release that is neither MULTI nor advertises a missing language — which is exactly what a
    plain English release looks like, since English is unmarked."""
    from app import tv
    cfg = {"min_seeders": 5, "score_threshold": 60}
    # Show.S01E05.1080p.WEB-DL with 20 seeders: score = min(20,100) + 20 (RES) = 40
    assert tv._usable((40, 20, "Show.S01E05.1080p.WEB-DL", "l", "rid"), cfg) is True
    assert tv._usable((20, 0, "dead", "l", "rid"), cfg) is False       # the real bug: 0 seeders
    assert tv._usable((120, 3, "starved", "l", "rid"), cfg) is False


def test_tv_search_honours_the_blocklist(app_env, monkeypatch):
    """The TV blocklist was decorative: `dl_id` was never recorded on the automatic path and
    `_search` never read `tried`, so a dropped release was re-picked by the very next search —
    the grab -> stall -> blocklist -> re-grab-the-same-thing loop."""
    from app import tv

    class FakePro:
        def __init__(self, *a, **k): pass
        def search(self, q, ids):
            return [{"title": "Show.S01E05.1080p.WEB-DL-A", "seeders": 30,
                     "magnetUrl": "magnet:?xt=urn:btih:" + "a" * 40, "size": 2e9},
                    {"title": "Show.S01E05.720p.WEB-DL-B", "seeders": 20,
                     "magnetUrl": "magnet:?xt=urn:btih:" + "b" * 40, "size": 1e9}]
    monkeypatch.setattr(tv, "Prowlarr", FakePro)
    cfg = dict(app_env.DEFAULTS, min_seeders=5)

    first = tv._search("Show S01E05", cfg, season=1, ep=5)
    assert first is not None and len(first) == 5, "must return the release id to blocklist"
    second = tv._search("Show S01E05", cfg, season=1, ep=5, tried={first[4]})
    assert second is not None and second[4] != first[4]
    assert tv._search("Show S01E05", cfg, season=1, ep=5,
                      tried={first[4], second[4]}) is None


def test_a_broken_config_logs_once_not_once_per_request(app_env):
    """load_config runs on every API request and every scheduler tick. Logging the parse failure
    each time churned through all 3 rotated files in minutes, burying the real history."""
    app_env.save_config({"min_seeders": 9})
    with open(app_env.CONFIG_FILE, "w") as f:
        f.write('{"min_seeders": 9, TRUNCA')
    before = len(app_env.tail_log(9999))
    for _ in range(20):
        app_env.load_config()
    # Once per DISTINCT broken state, never per request: the complaint plus its dispatcher page
    # (a broken config stops the whole pipeline, so it files a config-broken ticket too).
    assert len(app_env.tail_log(9999)) - before == 2


def test_removing_a_broken_config_lets_saves_work_again(app_env):
    """The log line says "fix or remove the file" — but the flag only cleared on a successful
    parse, so removing it left the process refusing to save for its whole lifetime."""
    app_env.save_config({"min_seeders": 9})
    with open(app_env.CONFIG_FILE, "w") as f:
        f.write('{"min_seeders": 9, TRUNCA')
    app_env.load_config()
    with pytest.raises(app_env.ConfigUnreadable):
        app_env.save_config({"min_seeders": 3})
    os.remove(app_env.CONFIG_FILE)                       # the operator does what it says
    app_env.load_config()
    app_env.save_config({"min_seeders": 3})
    assert app_env.load_config()["min_seeders"] == 3


def test_search_movie_reraises_so_the_sweep_can_stop(app_env, monkeypatch):
    """stage_search wraps search_movie in `except SearchUnavailable` to abandon the sweep, but
    search_movie caught it itself — making that handler dead code, so the film sweep ran all 25
    queries against a down indexer instead of stopping at the first."""
    from app import pipeline
    app_env.upsert_movie({"tmdb_id": 22, "imdb_id": "tt22", "radarr_id": 22, "title": "T",
                          "original_title": "T", "year": 2020, "original_lang": "french",
                          "french_path": "/x.mkv", "quality": "1080p"})
    app_env.set_status(22, "pending")

    def boom(*a, **k):
        raise pipeline.SearchUnavailable("connection refused")
    monkeypatch.setattr(pipeline, "candidates", boom)
    with pytest.raises(pipeline.SearchUnavailable):
        pipeline.search_movie(22, dict(app_env.DEFAULTS))
    # ...and the record is left retryable, not settled into a 24h cooldown
    assert app_env.get_movie(22)["status"] == "pending"


def test_enqueue_merge_reports_when_nothing_will_drain_the_queue(app_env):
    """The API used to merge inline, so it ran regardless of `enabled`/`paused`. Queueing is
    right, but `enabled` is False on a fresh install — reporting success while nothing happens
    is not."""
    from app import pipeline
    app_env.upsert_movie({"tmdb_id": 23, "imdb_id": "tt23", "radarr_id": 23, "title": "T",
                          "original_title": "T", "year": 2020, "original_lang": "french",
                          "french_path": "/x.mkv", "quality": "1080p"})
    app_env.save_config({"enabled": False})
    queued, note = pipeline.enqueue_merge("movie", 23)
    assert queued is True and "disabled" in note

    app_env.save_config({"enabled": True, "paused": True})
    queued, note = pipeline.enqueue_merge("movie", 23)
    assert queued is True and "paused" in note

    app_env.save_config({"enabled": True, "paused": False})
    queued, note = pipeline.enqueue_merge("movie", 23)
    assert queued is True and note == ""


def test_a_record_that_fails_again_actually_gets_a_new_ticket(app_env):
    """Pruning ai_seen_records.json let a recovered-then-re-failed record become "new" again —
    but core.ticket keys the batch on a hash of the record ids, so the identical set produced the
    identical key and the (kind,key) guard silently refused to write the ticket. The record was
    already stamped ai_status='pending', and with nothing on disk `undispatched()` couldn't see
    it either, so the staleness sweep reported "the AI did not respond within 60m" about a page
    that was never sent — the exact failure this whole mechanism exists to make visible."""
    key = "abc123"
    assert app_env.ticket("errors-review", "first", {"records": []}, key=key) is True
    os.remove(os.path.join(app_env.CONFIG_DIR, "ai-tickets", "errors-review.json"))

    # same record-set failing again: the once-only guard would refuse this
    assert app_env.ticket("errors-review", "again", {"records": []}, key=key) is False
    assert app_env.ticket("errors-review", "again", {"records": []}, key=key, once=False) is True


def test_once_false_still_refuses_to_clobber_an_undispatched_ticket(app_env):
    """The other guard must survive: a ticket the dispatcher has not picked up yet is work in
    flight, and overwriting it would lose whatever it listed."""
    assert app_env.ticket("errors-review", "first", {"records": []}, key="k1", once=False) is True
    # still on disk -> a second page must not replace it
    assert app_env.ticket("errors-review", "second", {"records": []}, key="k2", once=False) is False


def test_retrying_clears_the_previous_attempts_ai_verdict(app_env, monkeypatch):
    """The Review tab filters on ai_status, and neither retry path cleared it — so a record that
    had already been re-queued sat in the list forever wearing the verdict of the attempt that
    failed. The symptom is a row badged `pending`, searching again, captioned "AI did not respond
    within 60m", while "Retry N failed" reports a tiny N against a list of a thousand."""
    from app import pipeline
    app_env.upsert_movie({"tmdb_id": 30, "imdb_id": "tt30", "radarr_id": 30, "title": "T",
                          "original_title": "T", "year": 2026, "original_lang": "english",
                          "french_path": "/x.mkv", "quality": "1080p"})
    app_env.set_status(30, "error", error="grab: torrent never appeared", dl_id="rel-A",
                       attempts=4, ai_status="needs_human", ai_verdict="no donor exists",
                       ai_at=1.0)
    assert pipeline.retry_movie(30, dict(app_env.DEFAULTS, qb_url="http://127.0.0.1:1")) is True
    mv = app_env.get_movie(30)
    assert mv["status"] == "pending"
    assert mv["ai_status"] is None and mv["ai_verdict"] is None and mv["ai_at"] is None
    assert mv["attempts"] == 0                      # an operator retry means "try again"
    assert "rel-A" in mv["tried"], "the release that failed must stay blocklisted"


def test_a_deliberate_give_up_keeps_its_recorded_reason(app_env):
    """`ignored` is the AI's /unfixable verdict — clearing that would turn a considered give-up
    back into an unexplained skip, which is the whole thing /unfixable exists to avoid."""
    from app import pipeline
    assert "ignored" in pipeline.AI_KEEP_STATES
    assert set(pipeline.AI_RESET) == {"ai_status", "ai_verdict", "ai_at"}


def test_a_sync_failure_tries_other_releases_before_asking_a_human(app_env):
    """`sync_review: True` (the default) sent EVERY sync failure straight to `review` on the
    first attempt, so max_sync_retries — the budget that exists to try four DIFFERENT releases —
    was never spent. The operator was told to "pick another release" by a pipeline that would
    happily have done it. Review is what happens when that budget is gone, not instead of it."""
    from app import pipeline
    cfg = dict(app_env.DEFAULTS, qb_url="http://127.0.0.1:1", max_sync_retries=4, sync_review=True)
    app_env.upsert_movie({"tmdb_id": 40, "imdb_id": "tt40", "radarr_id": 40, "title": "T",
                          "original_title": "T", "year": 2018, "original_lang": "danish",
                          "french_path": "/x.mkv", "quality": "1080p"})
    seen = []
    for i in range(1, 5):
        app_env.set_status(40, "merging", dl_id=f"rel-{i}", dl_hash=f"h{i}")
        pipeline.reject_and_retry(40, "couldn't sync", cfg, 14.9, final="review")
        seen.append(app_env.get_movie(40)["status"])
    assert seen[:3] == ["pending"] * 3, "the first three failures must fetch another release"
    assert seen[3] == "review", "only the exhausted budget reaches a human"
    mv = app_env.get_movie(40)
    assert mv["attempts"] == 4
    # entries are [rid, ts] since the blocklist learned to age — the identities are what matter
    assert [e[0] for e in json.loads(mv["tried"])] == [f"rel-{i}" for i in range(1, 5)]
    assert mv["dl_hash"] == "h4", "review keeps the donor — /set_sync needs the file"


def test_the_framerate_message_carries_the_ratio_to_apply(app_env):
    """The old text named a manual action and withheld the one fact that makes the automatic one
    possible. The stretch is just donor_fps/base_fps, so the message now states it — snapped to
    the textbook transfer ratio, since ffprobe reports 23.976 rather than 24000/1001."""
    from app import pipeline
    msg = pipeline._sync_fail_reason(1200, True, None, base_fps=23.976, donor_fps=25.0)
    assert "1.0427083" in msg, msg          # film -> PAL, not the 1.0427094 raw division
    assert "/set_sync" in msg
    # a ratio that is NOT a standard transfer must not be snapped to one
    odd = pipeline._sync_fail_reason(1200, True, None, base_fps=23.976, donor_fps=30.0)
    assert "1.2512513" in odd, odd


def test_tv_sync_failure_is_no_longer_terminal_on_the_first_try(app_env):
    """TV went straight to sync_fail with `attempts` never incremented, so the retry budget was
    dead code there and one framerate mismatch ended the episode permanently."""
    from app import tv
    cfg = dict(app_env.DEFAULTS, max_sync_retries=4, sync_review=True)
    app_env.upsert_episode({"id": "7:1:1", "series_id": 7, "series_title": "S", "tvdb_id": 1,
                            "season": 1, "episode": 1, "french_path": "/y.mkv",
                            "quality": "1080p"})
    app_env.set_ep_status("7:1:1", "merging", dl_id="rel-1", dl_hash="pack")
    tv._reject_and_retry_ep(app_env.get_episode("7:1:1"), "couldn't sync", cfg, final="review")
    e = app_env.get_episode("7:1:1")
    assert e["status"] == "pending" and e["attempts"] == 1
    assert "rel-1" in e["tried"]


# ------------------------------------------------------------------ donor stale-path race
def _stub_qb(monkeypatch, module, *, content_path=None, save_path=None, files=None, gone=False):
    class QB:
        def __init__(self, *a, **k): pass
        def login(self): pass
        def torrent(self, h):
            return None if gone else {"hash": h, "content_path": content_path,
                                      "save_path": save_path}
        def files(self, h): return files or []
    monkeypatch.setattr(module, "QBittorrent", QB)


def test_donor_path_is_re_resolved_after_qb_moves_the_completed_download(app_env, tmp_path,
                                                                        monkeypatch):
    """`en_file` is captured at promote time from the torrent's content_path — then qB MOVES the
    finished torrent out of its incomplete directory, and the merge (which runs later, off a
    queue) still holds the pre-move path. The record was marked "merge: missing en_file" while
    the donor sat on disk perfectly intact under its new name."""
    from app import pipeline
    media = tmp_path / "media"
    inc = media / ".Téléchargements" / "incompleted" / "audio-merge" / "550"
    comp = media / ".Téléchargements" / "completed" / "audio-merge" / "550"
    inc.mkdir(parents=True)
    (inc / "Movie.2019.MULTI.1080p.mkv").write_bytes(b"x" * 5000)
    cached = str(inc / "Movie.2019.MULTI.1080p.mkv")
    cfg = dict(app_env.DEFAULTS, media_mount=str(media))

    _stub_qb(monkeypatch, pipeline,
             content_path="/data/.Téléchargements/completed/audio-merge/550",
             save_path="/data/.Téléchargements/completed/audio-merge/550")
    # nothing has moved yet -> the cached path is returned untouched, no qB call needed
    assert pipeline.resolve_donor_path(cfg, "abc", cached) == cached

    comp.parent.mkdir(parents=True, exist_ok=True)
    inc.rename(comp)                                   # qB's completion move
    assert not os.path.exists(cached)                  # the old guard failed exactly here
    got = pipeline.resolve_donor_path(cfg, "abc", cached)
    assert got == str(comp / "Movie.2019.MULTI.1080p.mkv") and os.path.exists(got)


def test_a_genuinely_missing_donor_still_fails_cleanly(app_env, tmp_path, monkeypatch):
    """The re-resolve must not paper over a real loss: if qB doesn't have the torrent any more,
    the caller's existing error path is still the right answer."""
    from app import pipeline
    cfg = dict(app_env.DEFAULTS, media_mount=str(tmp_path))
    _stub_qb(monkeypatch, pipeline, gone=True)
    assert pipeline.resolve_donor_path(cfg, "abc", "/gone/donor.mkv") is None
    # ...and with no hash recorded there is nothing to ask
    assert pipeline.resolve_donor_path(cfg, None, "/gone/donor.mkv") is None


def test_re_resolve_falls_back_to_the_file_list_and_picks_the_largest_video(app_env, tmp_path,
                                                                           monkeypatch):
    """content_path can point at a renamed root. The file list is relative to save_path and
    survives that, so it is the second source — largest VIDEXT entry, non-video ignored."""
    from app import pipeline
    media = tmp_path / "media"
    save = media / ".Téléchargements" / "completed" / "audio-merge-tv" / "9_S01"
    (save / "Season 01").mkdir(parents=True)
    (save / "Season 01" / "ep01.mkv").write_bytes(b"x" * 100)
    (save / "Season 01" / "ep02.mkv").write_bytes(b"x" * 9000)
    (save / "readme.nfo").write_text("junk")
    cfg = dict(app_env.DEFAULTS, media_mount=str(media))
    _stub_qb(monkeypatch, pipeline,
             content_path="/data/.Téléchargements/incompleted/renamed-away",
             save_path="/data/.Téléchargements/completed/audio-merge-tv/9_S01",
             files=[{"name": "Season 01/ep01.mkv", "size": 100},
                    {"name": "readme.nfo", "size": 10},
                    {"name": "Season 01/ep02.mkv", "size": 9000}])
    got = pipeline.resolve_donor_path(cfg, "h1", "/stale.mkv")
    assert got == str(save / "Season 01" / "ep02.mkv")


def test_the_orphan_sweep_cannot_delete_a_merge_imminent_donor(app_env):
    """A donor whose owner is queued or actively merging must never be swept. (`grabbed` needs no
    entry: dl_hash is only written at `downloading`, so the sweep cannot match such a record at
    all — the 30-minute added_on grace is what covers that window.)"""
    from app import pipeline
    assert {"ready", "merging"} <= set(pipeline.KEEP_DONOR_STATES)


# ------------------------------------------------------------------ request priority
def test_priority_moves_a_title_to_the_front_of_both_queues(app_env):
    """A title someone has just asked for must not sit behind a backlog ordered by recency —
    it would never be reached. Priority has to apply to the SEARCH sweep and the MERGE queue
    both: getting it downloaded first achieves nothing if it then queues behind thirty season
    pack episodes, each of which is a sync detect plus a remux."""
    from app import pipeline
    for i in range(1, 6):
        app_env.upsert_movie({"tmdb_id": i, "imdb_id": f"tt{i}", "radarr_id": i,
                              "title": f"Backlog {i}", "original_title": f"B{i}", "year": 2010,
                              "original_lang": "french", "french_path": f"/b{i}.mkv",
                              "quality": "1080p"})
        app_env.set_status(i, "pending", updated=1000 + i)
    app_env.upsert_movie({"tmdb_id": 99, "imdb_id": "tt99", "radarr_id": 99,
                          "title": "REQUESTED", "original_title": "R", "year": 2026,
                          "original_lang": "english", "french_path": "/r.mkv",
                          "quality": "1080p"})
    app_env.set_status(99, "pending", updated=500)      # worst possible place in the sweep
    assert [m["title"] for m in app_env.get_movies("pending")][-1] == "REQUESTED"
    app_env.set_priority("movie", 99, 1)
    assert [m["title"] for m in app_env.get_movies("pending")][0] == "REQUESTED"

    # ...and the merge queue, behind a full season pack
    for i in range(1, 31):
        eid = f"7:1:{i}"
        app_env.upsert_episode({"id": eid, "series_id": 7, "series_title": "Pack", "tvdb_id": 1,
                                "season": 1, "episode": i, "french_path": f"/e{i}.mkv",
                                "quality": "1080p"})
        app_env.set_ep_status(eid, "ready", updated=2000 + i)
    app_env.set_priority("movie", 99, 0)
    app_env.set_status(99, "ready", updated=9999)       # arrived last
    assert pipeline.merge_queue()[-1][1] == 99
    app_env.set_priority("movie", 99, 1)
    assert pipeline.merge_queue()[0][1] == 99, "the requested film must be merged first"


def test_priority_is_reversible_and_ordered_by_level(app_env):
    from app import pipeline
    for i in (1, 2):
        app_env.upsert_movie({"tmdb_id": i, "imdb_id": f"tt{i}", "radarr_id": i, "title": f"M{i}",
                              "original_title": f"M{i}", "year": 2020, "original_lang": "french",
                              "french_path": f"/m{i}.mkv", "quality": "1080p"})
        app_env.set_status(i, "ready", updated=100 + i)
    app_env.set_priority("movie", 1, 1)
    app_env.set_priority("movie", 2, 5)
    assert [i for _, i, _ in pipeline.merge_queue()] == [2, 1], "higher level goes first"
    app_env.set_priority("movie", 2, 0)
    assert [i for _, i, _ in pipeline.merge_queue()] == [1, 2]


def test_prioritising_a_series_skips_finished_episodes(app_env):
    """TV is requested per SHOW. Bumping one that is already merged would only pollute the
    ordering — there is nothing left to do for it."""
    for i, st in enumerate(("pending", "merged", "ignored", "error"), start=1):
        eid = f"8:1:{i}"
        app_env.upsert_episode({"id": eid, "series_id": 8, "series_title": "S", "tvdb_id": 1,
                                "season": 1, "episode": i, "french_path": f"/e{i}.mkv",
                                "quality": "1080p"})
        app_env.set_ep_status(eid, st)
    assert app_env.prioritise_series(8, 1) == 2         # pending + error only
    got = {e["id"]: e["priority"] for e in app_env.get_episodes()}
    assert got["8:1:1"] == 1 and got["8:1:4"] == 1
    assert not got["8:1:2"] and not got["8:1:3"]


def test_setting_priority_does_not_disturb_status_or_updated(app_env):
    """Priority is orthogonal to the state machine — and `updated` means "when the state last
    changed", so moving it here would reshuffle the attention panel for a non-event."""
    app_env.upsert_movie({"tmdb_id": 11, "imdb_id": "tt11", "radarr_id": 11, "title": "T",
                          "original_title": "T", "year": 2020, "original_lang": "french",
                          "french_path": "/x.mkv", "quality": "1080p"})
    app_env.set_status(11, "downloading", updated=4242)
    app_env.set_priority("movie", 11, 1)
    mv = app_env.get_movie(11)
    assert mv["status"] == "downloading" and mv["updated"] == 4242 and mv["priority"] == 1


# ------------------------------------------------------------------ completion forecast
def _seed_library(core_, complete, incomplete, merged_per_day=10, days=60):
    import time as _t
    now = _t.time()
    for i in range(complete + incomplete):
        core_.put_probe(f"/media/Films/F{i}/f{i}.mkv",
                        auds="fre,eng" if i < complete else "fre",
                        subs="fre,eng" if i < complete else "fre")
    for i in range(merged_per_day * days):
        core_.upsert_movie({"tmdb_id": 10000 + i, "imdb_id": f"t{i}", "radarr_id": i,
                            "title": f"M{i}", "original_title": f"M{i}", "year": 2020,
                            "original_lang": "french", "french_path": f"/media/Films/F{i}/f{i}.mkv",
                            "quality": "1080p"})
        core_.set_status(10000 + i, "merged", added_langs="eng", merge_kind="grafted",
                         merged_at=now - (i // merged_per_day) * 86400)
    return now


def test_forecast_projects_from_the_measured_completion_rate(app_env):
    """700/1000 complete, 10 real merges a day -> 200 more files for 90%, so 20 days. The rate
    must come from the SAME predicate as Recently merged, or the headline number and the list
    under it would tell different stories."""
    from app import main
    app_env.save_config({"media_mount": "/media"})
    _seed_library(app_env, complete=700, incomplete=300)
    f = main.forecast(90.0)
    assert (f["total"], f["complete"], f["needed"]) == (1000, 700, 200)
    assert f["rate_used"] == 10.0
    assert f["eta_days"] == 20.0


def test_forecast_gives_no_date_when_the_remainder_is_blocked(app_env):
    """The remaining files are not uniformly reachable: `no_release` has nothing to find and
    `ignored` is a deliberate give-up. Extrapolating the current rate across those would produce
    a confident date the pipeline cannot deliver."""
    from app import main
    app_env.save_config({"media_mount": "/media"})
    _seed_library(app_env, complete=700, incomplete=300)
    for n, i in enumerate(range(20000, 20280)):
        app_env.upsert_movie({"tmdb_id": i, "imdb_id": f"b{i}", "radarr_id": i, "title": f"B{i}",
                              "original_title": f"B{i}", "year": 2020, "original_lang": "french",
                              "french_path": f"/media/Films/F{700 + n}/f{700 + n}.mkv",
                              "quality": "1080p"})
        app_env.set_status(i, "no_release" if n < 200 else "ignored")
    f = main.forecast(90.0)
    assert f["eta_days"] is None
    assert f["blocked"]["no_release"] == 200 and f["blocked"]["ignored"] == 80
    assert "unblocked" in f["reason"]


def test_forecast_gives_no_date_with_no_completions_to_project_from(app_env):
    from app import main
    app_env.save_config({"media_mount": "/media"})
    _seed_library(app_env, complete=10, incomplete=90, merged_per_day=0, days=0)
    f = main.forecast(90.0)
    assert f["eta_days"] is None and "no rate" in f["reason"]


def test_forecast_does_not_understate_the_rate_on_a_young_install(app_env):
    """A 30-day window on an install that is three days old must divide by the span actually
    observed, not by 30 — otherwise a busy new setup reports a tenth of its real throughput."""
    from app import main
    app_env.save_config({"media_mount": "/media"})
    _seed_library(app_env, complete=700, incomplete=300, merged_per_day=10, days=3)
    assert main.forecast(90.0)["rate"]["30d"] == 10.0


def test_forecast_reports_target_already_met(app_env):
    from app import main
    app_env.save_config({"media_mount": "/media"})
    _seed_library(app_env, complete=950, incomplete=50)
    f = main.forecast(90.0)
    assert f["needed"] == 0 and f["eta_days"] == 0 and "already at" in f["reason"]


def test_forecast_needs_a_probed_library(app_env):
    from app import main
    f = main.forecast(90.0)
    assert f["eta_days"] is None and "probed" in f["reason"]


# ------------------------------------------------------------------ autonomy phase 1
# The fixer must not be able to die silently, and its queue must have real per-record units.

def _seed_error_movie(core, tmdb, title="Broken"):
    core.upsert_movie({"tmdb_id": tmdb, "imdb_id": f"tt{tmdb}", "radarr_id": tmdb,
                       "title": title, "original_title": title, "year": 2020,
                       "original_lang": "french", "french_path": f"/media/Films/{title}/f.mkv",
                       "quality": "1080p"})
    core.set_status(tmdb, "error", error="merge: something broke")


def test_ai_health_check_files_one_ticket_per_record(app_env, monkeypatch):
    """The errors-review batch had queue semantics that fought the dispatcher: while one batch
    sat unconsumed, every later failure was refused a ticket. Per-record files are the take/ack
    units the dispatcher actually works in."""
    from app import pipeline, agent
    monkeypatch.setattr(pipeline, "inflight_downloads", lambda cfg: 0)
    cfg = dict(app_env.DEFAULTS)
    _seed_error_movie(app_env, 1)
    _seed_error_movie(app_env, 2, "Broken2")
    pipeline.ai_health_check(cfg)
    assert os.path.exists(os.path.join(agent.TICKET_DIR, "review-m1.json"))
    assert os.path.exists(os.path.join(agent.TICKET_DIR, "review-m2.json"))
    assert app_env.get_movie(1)["ai_status"] == "pending"
    # a second sweep re-pages nothing: the seen-set marks them, the tickets still sit queued
    before = app_env.get_movie(1)["ai_at"]
    pipeline.ai_health_check(cfg)
    assert app_env.get_movie(1)["ai_at"] == before


def test_ticket_queue_cap_leaves_overflow_unstamped(app_env, monkeypatch):
    """One bad season pack is 400 failures at once. Beyond ai_max_tickets, records must stay
    UNSTAMPED — stamping ai_status='pending' with no ticket on disk is exactly the bug that made
    the staleness sweep report 'the AI did not respond' about pages that were never sent."""
    from app import pipeline
    monkeypatch.setattr(pipeline, "inflight_downloads", lambda cfg: 0)
    cfg = dict(app_env.DEFAULTS, ai_max_tickets=1)
    _seed_error_movie(app_env, 1)
    _seed_error_movie(app_env, 2, "Broken2")
    pipeline.ai_health_check(cfg)
    stamped = [app_env.get_movie(i)["ai_status"] for i in (1, 2)]
    assert stamped.count("pending") == 1 and stamped.count(None) == 1
    # the queue drains (dispatcher consumed the ticket) -> the overflow record is paged next
    from app import agent
    for n in os.listdir(agent.TICKET_DIR):
        if n.endswith(".json"):
            os.remove(os.path.join(agent.TICKET_DIR, n))
    pipeline.ai_health_check(cfg)
    assert [app_env.get_movie(i)["ai_status"] for i in (1, 2)].count("pending") == 2


def test_obsolete_ticket_is_withdrawn_when_the_record_recovers(app_env, monkeypatch):
    """A record that leaves its problem state on its own strands its queued ticket: the
    dispatcher wastes a run on a solved problem, and — since core.ticket refuses a same-kind
    overwrite — the stale file blocks that record's NEXT page indefinitely."""
    from app import pipeline, agent
    monkeypatch.setattr(pipeline, "inflight_downloads", lambda cfg: 0)
    cfg = dict(app_env.DEFAULTS)
    _seed_error_movie(app_env, 1)
    pipeline.ai_health_check(cfg)
    tpath = os.path.join(agent.TICKET_DIR, "review-m1.json")
    assert os.path.exists(tpath)
    app_env.set_status(1, "merged", ai_status=None)      # something fixed it
    pipeline.ai_health_check(cfg)
    assert not os.path.exists(tpath), "queued ticket for a recovered record must be withdrawn"
    # ...and a LATER failure of the same record pages again
    app_env.set_status(1, "error", error="fails differently")
    pipeline.ai_health_check(cfg)
    assert os.path.exists(tpath)


def test_notify_rate_limits_per_kind_and_rearms_on_clear(app_env, monkeypatch):
    """A standing condition re-fires every 3-minute sweep; a channel that repeats itself all day
    gets muted by its human, which is worse than no channel. One alarm per kind per window —
    re-armed the moment the condition is observed healthy."""
    from app import notify
    sent = []
    class _R:
        def raise_for_status(self):
            pass
    monkeypatch.setattr(notify.requests, "post", lambda *a, **k: sent.append(a) or _R())
    cfg = dict(app_env.DEFAULTS, notify_url="https://ntfy.example/vo")
    assert notify.send("disk", "low", "10GB", cfg=cfg) is True
    assert notify.send("disk", "low", "9GB", cfg=cfg) is False, "same kind inside the window"
    assert notify.send("dispatcher", "dead", "x", cfg=cfg) is True, "kinds are independent"
    notify.clear("disk")
    assert notify.send("disk", "low again", "8GB", cfg=cfg) is True
    assert len(sent) == 3
    assert notify.send("anything", "x", "y", cfg=dict(app_env.DEFAULTS)) is False, \
        "empty notify_url means the channel is off"


def test_dispatcher_alarm_requires_stale_queue_AND_no_live_heartbeat(app_env, monkeypatch):
    """A live sidecar with a deep queue is slow, not dead; a legacy hourly cron keeps no
    heartbeat but drains the queue. The alarm must need both signals bad."""
    import time as _t
    from app import pipeline, agent, notify
    calls = []
    monkeypatch.setattr(notify, "send", lambda k, t, b, cfg=None, force=False: calls.append(k))
    monkeypatch.setattr(notify, "clear", lambda k: None)
    cfg = dict(app_env.DEFAULTS, ai_dispatcher_alarm_min=1)
    os.makedirs(agent.TICKET_DIR, exist_ok=True)
    tpath = os.path.join(agent.TICKET_DIR, "review-m9.json")
    with open(tpath, "w") as f:
        f.write("{}")
    old = _t.time() - 3600
    os.utime(tpath, (old, old))
    pipeline.check_dispatcher(cfg)                      # stale queue, no heartbeat -> alarm
    assert calls == ["dispatcher"]
    with open(agent.HEARTBEAT, "w"):
        pass                                            # fresh heartbeat -> alive, just slow
    pipeline.check_dispatcher(cfg)
    assert calls == ["dispatcher"], "a live heartbeat must suppress the alarm"
    st = agent.status(cfg)
    assert st["heartbeat_age"] is not None and st["waiting"] == 1


def test_broken_config_pages_the_dispatcher(app_env):
    """An unparseable config.json doesn't just degrade — with `enabled` defaulting False it
    STOPS the pipeline, and the only trace used to be one log line."""
    with open(app_env.CONFIG_FILE, "w") as f:
        f.write('{"enabled": true, TRUNCATED')
    cfg = app_env.load_config()
    assert cfg["enabled"] is False
    from app import agent
    assert os.path.exists(os.path.join(agent.TICKET_DIR, "config-broken.json"))


# ------------------------------------------------------------------ autonomy phase 2
# Failures a retry can fix must not page the AI; failures a crash caused must not strand a
# record; the pipeline runs the AI runbook's deterministic first line itself.

def test_transient_failures_self_retry_then_become_a_real_error(app_env):
    """A momentary qB outage minted an `error` record, which paged the AI for something the next
    sweep fixes for free. transient() self-retries, bounded — an unbroken run of failures IS an
    error (the mount is gone, qB is misconfigured) and escalates with the count attached."""
    from app import pipeline
    _seed_error_movie(app_env, 1)
    app_env.set_status(1, "grabbed")
    cfg = dict(app_env.DEFAULTS, transient_max=3)
    assert pipeline.transient("movie", 1, "grab: connection refused", cfg) is True
    mv = app_env.get_movie(1)
    assert mv["status"] == "pending" and mv["transient_fails"] == 1
    assert pipeline.transient("movie", 1, "grab: connection refused", cfg) is True
    assert pipeline.transient("movie", 1, "grab: connection refused", cfg) is False
    mv = app_env.get_movie(1)
    assert mv["status"] == "error" and "3 consecutive" in mv["error"]


def test_donor_reset_refreshes_the_transient_budget(app_env):
    """A record sent back for a fresh run gets a fresh infrastructure-failure budget, same as
    attempts=0 — otherwise three grab hiccups in March count against a different donor in June."""
    from app import pipeline
    _seed_error_movie(app_env, 1)
    app_env.set_status(1, "pending", transient_fails=4)
    app_env.set_status(1, "pending", **pipeline.DONOR_RESET)
    assert app_env.get_movie(1)["transient_fails"] == 0


def _age_record(core, tmdb, seconds):
    import time as _t
    with core.db() as c:
        c.execute("UPDATE movies SET updated=? WHERE tmdb_id=?", (_t.time() - seconds, tmdb))


def test_stuck_searching_and_grabbed_are_recovered(app_env):
    """Nothing ever read `searching` or `grabbed` back out: stage_search walks only `pending`,
    stage_finish reconciles only `downloading`/`merging`, and a `grabbed` record has no dl_hash
    yet so even the orphan sweep can't see it. A crash mid-transition stranded them forever."""
    from app import pipeline
    cfg = dict(app_env.DEFAULTS, grab_mode="auto")
    _seed_error_movie(app_env, 1)
    app_env.set_status(1, "searching")
    _age_record(app_env, 1, 3600)
    _seed_error_movie(app_env, 2, "B2")
    app_env.set_status(2, "grabbed")
    _age_record(app_env, 2, 3600)
    _seed_error_movie(app_env, 3, "B3")
    app_env.set_status(3, "searching")                  # fresh — a live search, leave it alone
    pipeline.sweep_stuck(cfg)
    assert app_env.get_movie(1)["status"] == "pending"
    assert app_env.get_movie(2)["status"] == "pending"
    assert app_env.get_movie(3)["status"] == "searching"
    # the release was never blocklisted — it may never have been grabbed at all
    assert not json.loads(app_env.get_movie(2).get("tried") or "[]")


def test_approval_mode_grabbed_is_a_waiting_room_not_a_stuck_state(app_env):
    from app import pipeline
    _seed_error_movie(app_env, 1)
    app_env.set_status(1, "grabbed")
    _age_record(app_env, 1, 86400)
    pipeline.sweep_stuck(dict(app_env.DEFAULTS, grab_mode="approval"))
    assert app_env.get_movie(1)["status"] == "grabbed", \
        "in approval mode a human is deciding — that is not a crash"


def test_episode_in_review_flips_needs_human_when_the_ai_is_silent(app_env, monkeypatch):
    """The staleness sweep covered movies in ('error','review','sync_fail') but episodes only in
    ('error','sync_fail') — an episode in `review` whose ticket was consumed and never answered
    showed 'AI working' forever."""
    import time as _t
    from app import pipeline, agent
    monkeypatch.setattr(pipeline, "inflight_downloads", lambda cfg: 0)
    monkeypatch.setattr(agent, "undispatched", lambda: set())
    cfg = dict(app_env.DEFAULTS, ai_stale_min=1)
    app_env.upsert_episode({"id": "9:1:5", "series_id": 9, "series_title": "Show",
                            "tvdb_id": 9, "season": 1, "episode": 5,
                            "french_path": "/media/Series/Show/S01E05.mkv", "quality": "1080p"})
    app_env.set_ep_status("9:1:5", "review", ai_status="pending", ai_at=_t.time() - 3600)
    with open(os.path.join(str(app_env.CONFIG_DIR), "ai_seen_records.json"), "w") as f:
        json.dump(["episode:9:1:5:review"], f)          # paged by an earlier sweep
    pipeline.ai_health_check(cfg)
    assert app_env.get_episode("9:1:5")["ai_status"] == "needs_human"


def test_wide_probe_rescue_gates(app_env, monkeypatch):
    """The rescue merges only on a result the merge path itself would accept: a real offset, and
    never a constant offset across differing framerates. Off means off."""
    from app import pipeline, sync
    cfg = dict(app_env.DEFAULTS)
    monkeypatch.setattr(sync, "detect", lambda *a, **k: (-40000, 0.89, "video x4", None))
    assert pipeline.wide_probe_rescue("/b", "/d", 0, 0, 5000, False, cfg) == \
        (-40000, 0.89, "video x4", None)
    assert pipeline.wide_probe_rescue("/b", "/d", 0, 0, 5000, True, cfg) is None, \
        "a constant offset cannot correct frame drift, however far out it was found"
    monkeypatch.setattr(sync, "detect", lambda *a, **k: (None, 0.1, None, None))
    assert pipeline.wide_probe_rescue("/b", "/d", 0, 0, 5000, False, cfg) is None
    called = []
    monkeypatch.setattr(sync, "detect", lambda *a, **k: called.append(1))
    assert pipeline.wide_probe_rescue("/b", "/d", 0, 0, 5000, False,
                                      dict(cfg, sync_wide_probe=False)) is None
    assert pipeline.wide_probe_rescue("/b", "/d", 0, 0, 5000, False,
                                      dict(cfg, sync_probe_lag_s=60)) is None, \
        "no point re-searching NARROWER than the pass that already failed"
    assert not called


# ------------------------------------------------------------------ autonomy phase 3
# At full autonomy nobody watches the output, so the pipeline verifies its own work — and what
# a replacement discards stays reversible by machine.

def test_qc_rejects_only_on_confident_misalignment(app_env, monkeypatch):
    """A confident-but-wrong sync was the one failure nothing downstream could detect: the
    language reads as present, the record closes, the donor is deleted. QC must catch exactly
    that — and must NOT reject good merges of quiet films on absent evidence."""
    from app import pipeline, sync
    cfg = dict(app_env.DEFAULTS)
    # constant misalignment: confident windows agreeing on a value beyond the limit
    monkeypatch.setattr(sync, "audio_windows",
                        lambda *a, **k: [(500, 3800, 0.8), (2500, 3750, 0.7), (4500, 3820, 0.8)])
    ok, res, conf = pipeline.qc_grafted_audio("/out.mkv", 1, 5000, cfg)
    assert ok is False and abs(res - 3800) < 100, "confident agreed residual must reject"
    # aligned: small residuals agree
    monkeypatch.setattr(sync, "audio_windows",
                        lambda *a, **k: [(500, 100, 0.9), (2500, 140, 0.8), (4500, 90, 0.9)])
    assert pipeline.qc_grafted_audio("/out.mkv", 1, 5000, cfg)[0] is True
    # low confidence everywhere = inconclusive = accept (absence of evidence)
    monkeypatch.setattr(sync, "audio_windows",
                        lambda *a, **k: [(500, 3800, 0.1), (2500, -2000, 0.2)])
    assert pipeline.qc_grafted_audio("/out.mkv", 1, 5000, cfg)[0] is True
    monkeypatch.setattr(sync, "audio_windows", lambda *a, **k: [])
    assert pipeline.qc_grafted_audio("/out.mkv", 1, 5000, cfg)[0] is True
    # scattered confident noise: no agreement, no line -> inconclusive, accept
    monkeypatch.setattr(sync, "audio_windows",
                        lambda *a, **k: [(500, 2400, 0.5), (2500, -2100, 0.5), (4500, 600, 0.5)])
    assert pipeline.qc_grafted_audio("/out.mkv", 1, 5000, cfg)[0] is True
    called = []
    monkeypatch.setattr(sync, "audio_windows", lambda *a, **k: called.append(1) or [])
    assert pipeline.qc_grafted_audio("/out.mkv", 1, 5000,
                                     dict(cfg, postmerge_qc=False))[0] is True
    assert not called, "postmerge_qc off must not decode anything"


def test_qc_catches_a_wrong_drift_by_its_growing_residual(app_env, monkeypatch):
    """A wrong STRETCH used to hide in the inconclusive bucket: the consensus collapse only
    reported that windows disagreed, which is also what noise looks like. The signature that
    tells them apart is the trend — a residual that grows linearly across the runtime IS a
    drifting graft, and the one failure class the phase-3 QC still let through."""
    from app import pipeline, sync
    cfg = dict(app_env.DEFAULTS)
    # 1 ms/s residual (the realistic wrong-ratio case, e.g. 25/24 applied for 25/23.976):
    # +500ms at 500s, +2500ms at 2500s, +4500ms at 4500s over a 5000s film -> span ~5000ms
    monkeypatch.setattr(sync, "audio_windows",
                        lambda *a, **k: [(500, 500, 0.7), (2500, 2500, 0.6), (4500, 4500, 0.7)])
    ok, res, conf = pipeline.qc_grafted_audio("/out.mkv", 1, 5000, cfg)
    assert ok is False and res > 1500, "a linear residual across the runtime must reject"
    # two points only: any two points fit a line perfectly, so the bar doubles
    monkeypatch.setattr(sync, "audio_windows",
                        lambda *a, **k: [(500, 200, 0.7), (4500, 1800, 0.7)])
    assert pipeline.qc_grafted_audio("/out.mkv", 1, 5000, cfg)[0] is True, \
        "two points spanning less than 2x the limit are not confident drift evidence"
    monkeypatch.setattr(sync, "audio_windows",
                        lambda *a, **k: [(500, 200, 0.7), (4500, 4200, 0.7)])
    assert pipeline.qc_grafted_audio("/out.mkv", 1, 5000, cfg)[0] is False


def test_recycle_keeps_replaced_originals_and_purges_on_ttl(app_env, tmp_path):
    """_place_multi and the TV direct remux DISCARD the library file outright — the only merge
    outcomes that destroy content. The recycle bin makes a bad replacement reversible by machine
    for recycle_keep_days; the mtime is re-stamped so a years-old rip doesn't expire at once."""
    import time as _t
    from app import pipeline
    media_root = tmp_path / "media"
    lib = media_root / "Films" / "Movie (2003)"
    lib.mkdir(parents=True)
    f = lib / "Movie (2003).mkv"
    f.write_text("original video")
    old = _t.time() - 10 * 86400
    os.utime(f, (old, old))                             # ripped long ago
    cfg = dict(app_env.DEFAULTS, media_mount=str(media_root), recycle_keep_days=7)
    dest = pipeline.recycle(str(f), cfg)
    assert dest and os.path.exists(dest) and not f.exists()
    assert pipeline.RECYCLE_DIRNAME in dest and dest.endswith("Movie (2003).mkv")
    assert pipeline.purge_recycle(cfg) == 0, "freshly recycled must survive the purge (mtime restamped)"
    os.utime(dest, (old, old))                          # now it HAS sat there past the TTL
    assert pipeline.purge_recycle(cfg) == 1
    assert not os.path.exists(dest)
    assert not os.path.exists(os.path.dirname(dest)), "emptied recycle folders are pruned"


def test_recycle_disabled_falls_back_to_the_old_destructive_path(app_env, tmp_path):
    from app import pipeline
    f = tmp_path / "media" / "Films" / "x.mkv"
    f.parent.mkdir(parents=True)
    f.write_text("v")
    cfg = dict(app_env.DEFAULTS, media_mount=str(tmp_path / "media"), recycle_keep_days=0)
    assert pipeline.recycle(str(f), cfg) is None
    assert f.exists(), "recycle off must not touch the file — the caller overwrites/deletes it"


# ------------------------------------------------------------------ autonomy phase 4
# Nothing loops forever, nothing fills the disk, state survives corruption.

def test_blocklist_entries_age_out_but_legacy_strings_hold_until_rewritten(app_env):
    """The blocklist only ever grew: a release that stalled ONCE (0 seeds on a bad day) was
    burned forever — for some titles that is the only release that exists. Timestamped entries
    age out after tried_ttl_days; legacy plain strings stay blocked (safe) until a rewrite
    stamps them, so an upgrade doesn't un-blocklist years of known-bad releases at once."""
    import time as _t
    from app import pipeline
    _seed_error_movie(app_env, 1)
    old = _t.time() - 40 * 86400
    app_env.set_status(1, "pending", dl_id="rid-new",
                       tried=json.dumps([["rid-aged", old], ["rid-fresh", _t.time()], "rid-legacy"]))
    mv = app_env.get_movie(1)
    cfg = dict(app_env.DEFAULTS, tried_ttl_days=30)
    active = pipeline.tried_active(mv, cfg)
    assert active == {"rid-fresh", "rid-legacy"}, "aged entry eligible again; legacy still held"
    assert pipeline.tried_active(mv, dict(cfg, tried_ttl_days=0)) == \
        {"rid-aged", "rid-fresh", "rid-legacy"}, "TTL<=0 preserves never-expire"
    # a rewrite stamps the legacy entry (ages from the upgrade) and appends the current release
    stamped = json.loads(pipeline.blocklist(mv))
    assert all(isinstance(e, list) and len(e) == 2 for e in stamped)
    assert {e[0] for e in stamped} == {"rid-aged", "rid-fresh", "rid-legacy", "rid-new"}
    legacy_ts = next(ts for rid, ts in stamped if rid == "rid-legacy")
    assert _t.time() - legacy_ts < 60


def test_exhausted_no_release_is_paged_once(app_env, monkeypatch):
    """no_release was NEVER escalated — records whose query can't match their title re-searched
    the same wrong query every cooldown forever, while /search_releases (built for exactly this)
    waited for someone to think of it. One page per exhaustion, not one per cooldown cycle."""
    from app import pipeline, agent
    monkeypatch.setattr(pipeline, "inflight_downloads", lambda cfg: 0)
    cfg = dict(app_env.DEFAULTS, no_release_escalate_rounds=3)
    _seed_error_movie(app_env, 1)
    app_env.set_status(1, "no_release", search_rounds=2, error=None)
    pipeline.ai_health_check(cfg)
    tpath = os.path.join(agent.TICKET_DIR, "review-m1.json")
    assert not os.path.exists(tpath), "below the rounds gate -> not paged"
    app_env.set_status(1, "no_release", search_rounds=3)
    pipeline.ai_health_check(cfg)
    assert os.path.exists(tpath) and app_env.get_movie(1)["ai_status"] == "pending"
    with open(tpath) as f:
        assert "search_releases" in f.read()
    # the dispatcher consumed it and resolved -> later cooldown cycles must NOT re-page
    os.remove(tpath)
    app_env.set_status(1, "no_release", ai_status="resolved")
    pipeline.ai_health_check(cfg)
    assert not os.path.exists(tpath), "one-shot: a delivered verdict is durable"


def test_withdrawn_page_clears_the_pending_stamp(app_env, monkeypatch):
    """A record that recovers while its ticket still QUEUES gets the ticket withdrawn — and the
    stamp must go with it, or the next problem state flips to needs_human claiming 'the AI did
    not respond' about a page nobody was ever given."""
    from app import pipeline
    monkeypatch.setattr(pipeline, "inflight_downloads", lambda cfg: 0)
    cfg = dict(app_env.DEFAULTS)
    _seed_error_movie(app_env, 1)
    pipeline.ai_health_check(cfg)                       # pages, stamps pending
    assert app_env.get_movie(1)["ai_status"] == "pending"
    app_env.set_status(1, "pending")                    # recovered before dispatch
    pipeline.ai_health_check(cfg)                       # withdraws the queued ticket
    assert app_env.get_movie(1)["ai_status"] is None


def test_disk_gate_holds_the_worker_and_requeues_the_pair(app_env, monkeypatch, tmp_path):
    """rc>=2 half-writes compound the very disk-full that causes them, once per retry. A pair
    that can't fit re-queues un-penalised and the worker cools off instead of re-probing the
    same pair every ten seconds."""
    import time as _t
    from app import pipeline, notify
    monkeypatch.setattr(notify, "send", lambda *a, **k: True)
    a = tmp_path / "a.bin"; a.write_bytes(b"x" * 1000)
    ok, free, need = pipeline.disk_headroom_ok([str(a)], str(tmp_path),
                                               dict(app_env.DEFAULTS, disk_floor_gb=0))
    assert ok is True and free > 0
    ok, free, need = pipeline.disk_headroom_ok([str(a)], str(tmp_path),
                                               dict(app_env.DEFAULTS, disk_floor_gb=10 ** 6))
    assert ok is False, "an absurd floor cannot be satisfied"
    _seed_error_movie(app_env, 1)
    app_env.set_status(1, "merging")
    pipeline.DISK_STATE["hold_until"] = 0
    pipeline.hold_for_disk("movie", 1, free, need, dict(app_env.DEFAULTS))
    assert app_env.get_movie(1)["status"] == "ready"
    assert pipeline.DISK_STATE["hold_until"] > _t.time()
    assert pipeline.merge_next(dict(app_env.DEFAULTS)) is False, "worker holds while cooling off"
    pipeline.DISK_STATE["hold_until"] = 0


def test_corrupt_db_restores_from_the_nightly_snapshot(app_env):
    """The nightly VACUUM INTO backups existed but nothing ever read one — recovery was a human
    hand-copying a file. Corruption now quarantines the bad DB and restores the snapshot."""
    from app import core as c2
    _seed_error_movie(app_env, 1)
    assert c2.backup_db(keep=3)
    with open(app_env.DB_FILE, "r+b") as f:             # clobber the header -> unreadable DB
        f.write(b"CORRUPT!" * 16)
    for ext in ("-wal", "-shm"):
        p = app_env.DB_FILE + ext
        if os.path.exists(p):
            os.remove(p)
    assert c2.verify_or_restore_db() == "restored"
    assert app_env.get_movie(1)["title"] == "Broken", "state came back from the snapshot"
    assert any(f.startswith("vo-merge.db.corrupt-") for f in os.listdir(str(app_env.CONFIG_DIR))), \
        "the corrupt file is quarantined, never deleted"
    assert c2.verify_or_restore_db() == "ok"


def test_config_backup_skips_while_the_live_file_is_broken(app_env):
    """Copying an unparseable config.json would overwrite the day's good snapshot with the very
    bytes that broke it."""
    app_env.save_config({"min_seeders": 7})
    assert app_env.backup_config(keep=3)
    with open(app_env.CONFIG_FILE, "w") as f:
        f.write('{"broken": TRUNCA')
    app_env.load_config()
    assert app_env.backup_config(keep=3) is None


# ------------------------------------------------------------------ autonomy phase 5
# Terminal verdicts stay honest: a give-up is re-examined as indexers change.

def test_long_ignored_records_get_one_cheap_revisit(app_env, monkeypatch):
    """`ignored` rightly survives every rescan — but its usual reason, 'no release exists',
    decays as truth. A slow revisit re-opens the record the day a usable release appears and
    re-stamps the sleepers, instead of making the give-up permanent by accident."""
    import time as _t
    from app import pipeline
    cfg = dict(app_env.DEFAULTS, enabled=True, ignored_revisit_days=90, min_seeders=5)
    _seed_error_movie(app_env, 1, "NowFindable")
    app_env.set_status(1, "ignored", ai_verdict="nothing exists (2025)")
    _seed_error_movie(app_env, 2, "StillNothing")
    app_env.set_status(2, "ignored")
    with app_env.db() as c:                          # both ignored long ago
        c.execute("UPDATE movies SET updated=?", (_t.time() - 120 * 86400,))
    good = [{"score": 200, "seeders": 30, "title": "NowFindable 2020 MULTI", "tried": False}]
    monkeypatch.setattr(pipeline, "candidates",
                        lambda tid, cfg=None, include_tried=False: good if tid == 1 else [])
    assert pipeline.revisit_ignored(cfg) == 1
    mv = app_env.get_movie(1)
    assert mv["status"] == "pending" and mv["attempts"] == 0
    assert mv["ai_verdict"] is None, "the give-up is over — its verdict goes with it"
    mv2 = app_env.get_movie(2)
    assert mv2["status"] == "ignored" and mv2["revisit_at"], \
        "still nothing -> stays ignored, re-stamped to sleep another cycle"
    assert pipeline.revisit_ignored(cfg) == 0, "freshly re-stamped records are not due again"


def test_revisit_is_capped_and_gated(app_env, monkeypatch):
    import time as _t
    from app import pipeline
    for i in range(1, 6):
        _seed_error_movie(app_env, i, f"T{i}")
        app_env.set_status(i, "ignored")
    with app_env.db() as c:
        c.execute("UPDATE movies SET updated=?", (_t.time() - 120 * 86400,))
    calls = []
    monkeypatch.setattr(pipeline, "candidates",
                        lambda tid, cfg=None, include_tried=False: calls.append(tid) or [])
    pipeline.revisit_ignored(dict(app_env.DEFAULTS, enabled=True, ignored_revisit_per_day=2))
    assert len(calls) == 2, "per-day cap keeps a big ignored backlog off the indexers"
    calls.clear()
    pipeline.revisit_ignored(dict(app_env.DEFAULTS, enabled=True, ignored_revisit_days=0))
    assert not calls, "0 = the old never-revisit behaviour"
    pipeline.revisit_ignored(dict(app_env.DEFAULTS, enabled=False))
    assert not calls, "a disabled pipeline must not search"


def test_repair_pass_is_shared_and_lock_guarded(app_env):
    """The endpoint and auto_repair run the SAME pass (pipeline.run_repair) so they can never
    apply different guards; start_repair refuses while a scan holds the lock."""
    from app import pipeline
    cfg = dict(app_env.DEFAULTS)
    assert pipeline.SCAN_LOCK.acquire(blocking=False)
    try:
        assert pipeline.start_repair([], cfg) is False
    finally:
        pipeline.SCAN_LOCK.release()
    pipeline.run_repair([], cfg)                     # empty pass: verifies nothing, deletes nothing
    assert pipeline.REPAIR_STATE["phase"] == "done"
    assert pipeline.REPAIR_STATE["deleted"] == 0


# ------------------------------------------------------------------ the Colony VOSTFR bug
# A Korean film missing only the French DUB grabbed a VOSTFR release (original audio + French
# subs — definitionally unable to fill the gap), then ran an hour of sync detection because the
# release won the video comparison and "wanted" the library's own English track. Two holes, two
# gates: scoring must reject a checkable VOST claim that fills no gap, and a merge must give the
# LIBRARY file something it lacks before the expensive part starts.

def test_vost_release_is_rejected_when_it_cannot_fill_the_gap():
    """VOSTFR is a concrete claim — original audio, French subs — not a free pass."""
    from app import media
    t = "Colony.2026.VOSTFR.1080p.WEBRip.10bits.AAC.2.0.x265-FaS"
    # the Colony case: Korean original, gap = French DUB -> the claim fills nothing
    assert media.useless_release(t, {"fre"}, "Colony", orig="Korean") is True
    # the same release IS the answer when the gap is the original-language VO...
    assert media.useless_release(t, {"kor"}, "Colony", orig="Korean") is False
    # ...or when the French SUBS it promises are what's missing
    assert media.useless_release(t, {"fre"}, "Colony", orig="Korean",
                                 need_subs={"fre"}) is False
    # a VOSTFR of an ENGLISH-original film still carries eng audio
    assert media.useless_release("Movie.2019.VOSTFR.1080p", {"eng"}, "Movie",
                                 orig="English") is False
    # unknown original language -> the claim is uncheckable -> old conservative pass
    assert media.useless_release(t, {"fre"}, "Colony") is False
    assert media.useless_release(t, {"fre"}, "Colony", orig="?") is False
    # a VOSTFR+VF combo advertises the French dub too -> kept
    assert media.useless_release("Movie.2019.VF.VOSTFR.1080p", {"fre"}, "Movie",
                                 orig="Korean") is False
    # MULTI always passes; plain dub-reject behaviour unchanged
    assert media.useless_release("Movie.2019.MULTI.VOSTFR.1080p", {"fre"}, "Movie",
                                 orig="Korean") is False
    assert media.useless_release("Movie.2019.FRENCH.1080p", {"eng"}, "Movie",
                                 orig="Korean") is True


def test_merge_must_give_the_library_file_something_it_lacks(app_env):
    """graft_gains judges the OUTPUT against the LIBRARY file's own gap, whichever file won the
    video comparison — the base-relative question wanted_audio answers is not the record's."""
    from app import pipeline
    cfg = dict(app_env.DEFAULTS)
    lib = {"auds": [{"lang": "eng"}, {"lang": "kor"}],
           "subs": [{"lang": "eng"}, {"lang": "fre"}]}          # Colony: needs only fre AUDIO
    vostfr = {"auds": [{"lang": "kor"}], "subs": [{"lang": "fre"}]}
    # the bug: release won the video comparison (base=vostfr), library donates its eng track
    gains, needs = pipeline.graft_gains(vostfr, lib, {"eng"}, {"eng"},
                                        "movie", cfg, "Korean", {"kor"})
    assert not gains and needs == ["fre"], "output still lacks the French dub -> pointless merge"
    # a legit swap: the release actually carries the needed dub
    multi = {"auds": [{"lang": "kor"}, {"lang": "fre"}], "subs": []}
    gains, _ = pipeline.graft_gains(multi, lib, {"eng"}, set(), "movie", cfg, "Korean", {"kor"})
    assert "fre" in gains
    # normal direction: grafting the missing dub onto the library base
    gains, _ = pipeline.graft_gains(lib, lib, {"fre"}, set(), "movie", cfg, "Korean", {"kor"})
    assert gains == {"fre"}
    # the deliberate VO fallback stays a gain: Kraken's Norwegian when no English exists
    kraken = {"auds": [{"lang": "fre"}], "subs": []}
    gains, _ = pipeline.graft_gains(kraken, kraken, {"nor"}, set(),
                                    "movie", cfg, "Norwegian", {"nor"})
    assert "nor" in gains


def test_deleted_titles_are_pruned_on_a_schedule_with_the_mount_guard(app_env, tmp_path):
    """The prune only ran inside an operator's /rescan, so on an unattended install a deleted
    title dragged coverage down forever. pipeline.prune_library is the one shared
    implementation (rescan + daily housekeeping): mount-dead refuses to touch anything, and
    mid-flight records survive even with their file briefly absent."""
    from app import pipeline
    media_root = tmp_path / "media"
    kept = media_root / "Films" / "Kept (2020)" / "kept.mkv"
    gone = media_root / "Films" / "Gone (2019)" / "gone.mkv"
    app_env.put_probe(str(kept))
    app_env.put_probe(str(gone))
    _seed_error_movie(app_env, 1)                        # error + missing file -> prunable
    _seed_error_movie(app_env, 2, "MidFlight")
    app_env.set_status(2, "downloading")                 # merge-window absence -> protected
    cfg = dict(app_env.DEFAULTS, media_mount=str(media_root))
    assert pipeline.prune_library(cfg) is None, "an unmounted share must never authorise a prune"
    assert app_env.get_movie(1) is not None
    kept.parent.mkdir(parents=True)
    kept.write_text("v")                                 # the mount is alive now
    probes, mv, ep = pipeline.prune_library(cfg)
    assert (probes, mv, ep) == (1, 1, 0)
    assert app_env.get_movie(1) is None, "settled record with a vanished file is dropped"
    assert app_env.get_movie(2) is not None, "mid-flight records are never pruned"
    with app_env.db() as c:
        left = [r["path"] for r in c.execute("SELECT path FROM probes")]
    assert left == [str(kept)]


def test_ai_paging_holds_while_a_scan_churns_statuses(app_env, monkeypatch):
    """Observed live: a running recheck re-opened records en masse, and every 3-minute sweep
    paged ~26 tickets that the NEXT sweep withdrew as 'recovered' and then re-filed — an
    endless create/withdraw cycle burning dispatcher runs on tickets about to be void. Paging
    holds while a scan runs, like searches always have."""
    from app import pipeline, agent
    monkeypatch.setattr(pipeline, "inflight_downloads", lambda cfg: 0)
    cfg = dict(app_env.DEFAULTS)
    _seed_error_movie(app_env, 1)
    assert pipeline.SCAN_LOCK.acquire(blocking=False)
    try:
        pipeline.ai_health_check(cfg)
        assert not os.path.exists(os.path.join(agent.TICKET_DIR, "review-m1.json"))
        assert app_env.get_movie(1)["ai_status"] is None, "no stamp for a page never sent"
    finally:
        pipeline.SCAN_LOCK.release()
    pipeline.ai_health_check(cfg)                       # scan over -> paged promptly
    assert os.path.exists(os.path.join(agent.TICKET_DIR, "review-m1.json"))
