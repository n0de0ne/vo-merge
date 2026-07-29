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
    """Kept as one constant because the sync fields were exactly the ones each retry path forgot."""
    from app import pipeline
    assert set(pipeline.DONOR_RESET) == {"dl_hash", "dl_id", "en_file",
                                         "sync_offset_ms", "sync_drift", "sync_manual"}


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
    assert len(app_env.tail_log(9999)) - before == 1


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
    assert json.loads(mv["tried"]) == [f"rel-{i}" for i in range(1, 5)]
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
