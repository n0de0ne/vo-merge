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
    try:
        cfg = core.load_config()
        if cfg.get("scope_films", True):
            pipeline.stage_finish(cfg)
        if cfg.get("scope_series"):
            tv.stage_finish(cfg)
    except Exception as e:
        core.log(f"finish_job error: {e}")


def start():
    cfg = core.load_config()
    _sched.add_job(_search_job, "interval", minutes=cfg["search_interval_min"],
                   id="search", replace_existing=True)
    _sched.add_job(_finish_job, "interval", minutes=cfg["finish_interval_min"],
                   id="finish", replace_existing=True)
    _sched.start()
    core.log("scheduler started")


def reschedule():
    cfg = core.load_config()
    _sched.reschedule_job("search", trigger="interval", minutes=cfg["search_interval_min"])
    _sched.reschedule_job("finish", trigger="interval", minutes=cfg["finish_interval_min"])
