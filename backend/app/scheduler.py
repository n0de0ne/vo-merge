"""APScheduler: runs the two pipeline stages on configurable intervals."""
from apscheduler.schedulers.background import BackgroundScheduler
from . import core, pipeline, tv

_sched = BackgroundScheduler(daemon=True)


def _search_job():
    try:
        cfg = core.load_config()
        if cfg.get("scope_films", True):
            pipeline.scan(cfg)
            pipeline.stage_search(cfg)
        if cfg.get("scope_series"):
            tv.scan(cfg)
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
    _sched.start()
    core.log("scheduler started")


def reschedule():
    cfg = core.load_config()
    _sched.reschedule_job("search", trigger="interval", minutes=cfg["search_interval_min"])
    _sched.reschedule_job("finish", trigger="interval", minutes=cfg["finish_interval_min"])
    _sched.reschedule_job("stall", trigger="interval", minutes=cfg.get("stall_check_interval_min", 3))
