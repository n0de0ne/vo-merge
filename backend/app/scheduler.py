"""APScheduler: runs the pipeline stages on configurable intervals, plus the merge worker."""
import threading
from apscheduler.schedulers.background import BackgroundScheduler
from . import core, pipeline, tv

_sched = BackgroundScheduler(daemon=True)
_merge_threads = {}          # pool slot index -> worker thread


def ensure_merge_workers():
    """Size the merge worker pool to `max_parallel_merges`. Raising it spawns the missing
    workers immediately; lowering it lets the surplus workers retire themselves."""
    for i, t in list(_merge_threads.items()):
        if not t.is_alive():
            _merge_threads.pop(i, None)
    for i in range(pipeline.MERGE_GATE.limit()):
        if i not in _merge_threads:
            t = threading.Thread(target=pipeline.merge_worker, args=(i,),
                                 name=f"merge-worker-{i}", daemon=True)
            t.start()
            _merge_threads[i] = t


def _promote_job():
    """Fast + cheap: move every download that hit 100% out of 'downloading' and onto the merge
    queue. Runs on its own short timer (and never behind FINISH_LOCK) so a finished torrent is
    reflected in the UI within a minute and its grab slot is freed immediately."""
    try:
        cfg = core.load_config()
        if not cfg.get("enabled"):
            return
        if cfg.get("scope_films", True):
            pipeline.promote_completed(cfg)
        if cfg.get("scope_series"):
            tv.promote_completed(cfg)
    except Exception as e:
        core.log(f"promote_job error: {e}")


def _search_job():
    try:
        cfg = core.load_config()
        # In "files" mode a scan probes the library, so it must not overlap a manual /rescan —
        # two full passes would double the mkvmerge load for no benefit. Skipping is safe: the
        # run in progress is producing fresher results than this one would.
        scanning = pipeline.SCAN_LOCK.acquire(blocking=False)
        try:
            if scanning:
                if cfg.get("scope_films", True):
                    pipeline.scan(cfg)
                if cfg.get("scope_series"):
                    tv.scan(cfg)
            else:
                core.log("search job: a rescan is already running -> searching without re-scanning")
        finally:
            if scanning:
                pipeline.SCAN_LOCK.release()
        if cfg.get("scope_films", True):
            pipeline.stage_search(cfg)
        if cfg.get("scope_series"):
            tv.stage_search(cfg)
    except Exception as e:
        core.log(f"search_job error: {e}")


def _finish_job():
    if not pipeline.FINISH_LOCK.acquire(blocking=False):
        return                       # a finish cycle is already running
    refill = False
    try:
        cfg = core.load_config()
        if cfg.get("scope_films", True):
            pipeline.stage_finish(cfg)
        if cfg.get("scope_series"):
            tv.stage_finish(cfg)
        refill = True
    except Exception as e:
        core.log(f"finish_job error: {e}")
    finally:
        pipeline.FINISH_LOCK.release()
    # merges freed slots + deleted donors -> top the download queue back up to the in-flight cap
    if refill:
        try:
            cfg = core.load_config()
            if cfg.get("scope_films", True):
                pipeline.stage_search(cfg)
            if cfg.get("scope_series"):
                tv.stage_search(cfg)
        except Exception as e:
            core.log(f"finish refill error: {e}")


def _stall_job():
    """Fast, lightweight stall sweep — deliberately NOT behind FINISH_LOCK, so seederless
    downloads get dropped + re-grabbed promptly even while a long merge run is in progress."""
    try:
        cfg = core.load_config()
        pipeline.no_seed_public(cfg)
        pipeline.sweep_orphan_donors(cfg)
        pipeline.ai_health_check(cfg)
        if cfg.get("scope_films", True):
            pipeline.sweep_stalled(cfg)
        if cfg.get("scope_series"):
            tv.sweep_stalled(cfg)
    except Exception as e:
        core.log(f"stall_job error: {e}")


def start():
    cfg = core.load_config()
    _sched.add_job(_search_job, "interval", minutes=cfg["search_interval_min"],
                   id="search", replace_existing=True)
    _sched.add_job(_finish_job, "interval", minutes=cfg["finish_interval_min"],
                   id="finish", replace_existing=True)
    _sched.add_job(_stall_job, "interval", minutes=cfg.get("stall_check_interval_min", 3),
                   id="stall", replace_existing=True)
    _sched.add_job(_promote_job, "interval", minutes=cfg.get("promote_interval_min", 1),
                   id="promote", replace_existing=True)
    _sched.start()
    ensure_merge_workers()          # background merger(s) draining the 'ready' queue
    core.log("scheduler started")


def reschedule():
    cfg = core.load_config()
    _sched.reschedule_job("search", trigger="interval", minutes=cfg["search_interval_min"])
    _sched.reschedule_job("finish", trigger="interval", minutes=cfg["finish_interval_min"])
    _sched.reschedule_job("stall", trigger="interval", minutes=cfg.get("stall_check_interval_min", 3))
    _sched.reschedule_job("promote", trigger="interval", minutes=cfg.get("promote_interval_min", 1))
    ensure_merge_workers()          # pick up a changed max_parallel_merges right away
