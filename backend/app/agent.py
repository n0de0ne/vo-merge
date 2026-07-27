"""The on-call AI, running INSIDE vo-merge.

Until now the "on-call AI" was a host-side arrangement: vo-merge wrote a ticket file into
/config/ai-tickets/ and an Unraid user script, on a cron, ran the Claude Code CLI against it.
That works, but it puts the part that actually fixes things outside the app — outside its
config, its logs, its restart, and its container image. When the cron doesn't run there is no
error anywhere: the tickets simply pile up, every record ages past `ai_stale_min`, and the whole
backlog flips to "AI did not respond within 60m" as though the agent had examined each one and
given up. That is exactly the failure this repo hit.

So `ai_mode` picks the dispatcher:

  host     — the old behaviour: write the ticket file, let the host cron run the CLI.
  builtin  — vo-merge runs the agent itself, in-process, over the Anthropic API.
  off      — no escalation at all.

`builtin` needs nothing on the host and nothing new in the image beyond `pip install anthropic`
(the Claude Code CLI would mean bundling Node into a python-slim image). It is the SAME agent
loop the CLI runs: read the record, call the app's REST actions, report the outcome — the action
surface is unchanged, because it is the app's own API either way.

Two things keep it narrow:

- **An allowlist, not the whole API.** The agent is handed one `vo_api` tool that can only reach
  read-only endpoints and the per-record actions a reviewer needs. Settings, rescans, the library
  repair (which deletes media), pause and the bulk retries are NOT reachable — an agent working a
  single stuck episode has no business re-writing the config or kicking off a library-wide pass.
- **It must report.** The loop is capped (`ai_max_steps`), one record at a time, `ai_max_records`
  per sweep. A run that ends without calling `report` is stamped `needs_human` rather than left
  looking like it succeeded.
"""
import json, re, threading, time

import requests

from . import core

# ---------------------------------------------------------------------------------------------
# What the agent is allowed to call. Anything not matched here is refused with an explanation,
# which the model can read and route around — a refusal is a tool result, not a crash.
_ALLOW = [
    ("GET",  r"/(status|movies|logs|dashboard|coverage|library|downloads|ai_log)$"),
    ("GET",  r"/tv/(status|episodes)$"),
    ("GET",  r"/(movie|episode)/[^/]+/(context|candidates)$"),
    ("GET",  r"/tv/[^/]+/[^/]+/candidates$"),
    ("POST", r"/movie/[^/]+/(search|merge|grab|sync|sync_probe|set_sync|retry|research|another"
             r"|ignore|unignore|unfixable|ai_result|apply_offset)$"),
    ("POST", r"/episode/[^/]+/(retry|grab|assign|sync_probe|set_sync|ignore|unfixable|ai_result)$"),
    ("POST", r"/search_releases$"),
    ("POST", r"/tv/[^/]+/[^/]+/grab$"),
]
_ALLOW = [(m, re.compile(p)) for m, p in _ALLOW]

# these run mkvmerge/ffmpeg and legitimately take minutes; everything else should be quick
_SLOW = re.compile(r"/(sync|sync_probe|set_sync|merge|assign|another|grab)$")
_MAX_RESULT = 12000        # chars of an API response handed back to the model


def available():
    """Is the Anthropic SDK installed? (Kept soft so an older image still boots.)"""
    try:
        import anthropic          # noqa: F401
        return True
    except Exception:
        return False


def mode(cfg):
    if not cfg.get("ai_tickets", True):
        return "off"
    m = (cfg.get("ai_mode") or "host").lower()
    return m if m in ("host", "builtin", "off") else "host"


def enabled(cfg):
    """True when this process should run the agent loop itself."""
    return mode(cfg) == "builtin" and bool(cfg.get("anthropic_key")) and available()


# ---------------------------------------------------------------------------------------------
# The ticket payload. Built here rather than in main.py so the host dispatcher and the built-in
# agent are handed byte-for-byte the same brief — two dispatchers with drifting instructions is
# how you get "it worked from the CLI but not in-app".

def movie_context(mv):
    """(summary, context) describing ONE movie record and every action that can fix it."""
    record = {k: mv.get(k) for k in
              ("tmdb_id", "title", "original_title", "year", "original_lang", "status",
               "error", "sync_delta", "sync_offset_ms", "candidate_title",
               "candidate_score", "candidate_seeders", "attempts", "tried",
               "french_path", "en_file", "merged_file", "quality", "dl_hash",
               "need_audio", "need_subs", "audio_langs", "sub_langs")}
    tmdb_id = mv["tmdb_id"]
    summary = f"{mv['title']} ({mv['year']}) — {mv['status']}"
    if mv.get("error"):
        summary += f": {mv['error']}"
    ctx = {
        "record": record,
        "api": "http://10.0.1.5:8090/api (host) / http://localhost:8080/api (in-container)",
        "read_this_first": f"GET /movie/{tmdb_id}/context — the record, a probe of BOTH files "
                           "(fps/duration/audio tracks) and the matching log lines. Comparing "
                           "the two probes is the diagnosis for most sync and 'nothing to add' "
                           "failures.",
        "actions": [
            "GET  /movie/{id}/context — probes of both files + the relevant log lines",
            "GET  /movie/{id}/candidates — list releases (incl. already-tried)",
            "POST /movie/{id}/sync {\"offset_ms\":0} — re-run auto sync-detect + merge",
            "POST /movie/{id}/sync_probe {\"max_lag_s\":300} — MEASURE the offset and report "
            "it WITHOUT merging, searching much further out than the merge path does. On any "
            "\"couldn't sync\" this is the call to make FIRST: the merge path only searches "
            "+/-sync_max_lag_s, so a consistent offset beyond that reads as \"different cut\" "
            "when it is really a sponsor card or a 'previously on'. Returns every window's own "
            "answer, so a real re-edit (windows disagree) looks different from a large constant "
            "offset (windows agree). Add \"apply\":true to merge with what it finds.",
            "POST /movie/{id}/set_sync {\"offset_ms\":N,\"drift\":1.0427083} — apply a KNOWN "
            "offset and/or rate stretch with no detection (drift = donor_fps/base_fps; "
            "1.0427083 is film->PAL). Use when /context shows the two files' fps differ.",
            "POST /movie/{id}/another — blocklist current release, grab next best",
            "POST /movie/{id}/research — search again (keeps blocklist)",
            "POST /search_releases {\"query\":\"...\"} — run an ARBITRARY Prowlarr query and "
            "grab from the results. The built-in search composes its own query from the library "
            "title, so a title it never matches can never be found however often it re-searches; "
            "try the original/romaji/alternate title with no year.",
            "POST /movie/{id}/unfixable {\"reason\":\"...\"} — terminal give-up WITH a recorded "
            "reason (preferred over /ignore, which reads as an unexamined skip)",
            "POST /movie/{id}/ignore — give up on this title",
        ],
        "report_back": (
            f"When done, POST /movie/{tmdb_id}/ai_result with "
            "{\"status\":\"resolved|failed|needs_human\",\"verdict\":\"one line\","
            "\"action_taken\":\"what you did\"} so this leaves the operator's manual-review queue "
            "(needs_human = a person must decide)."),
        "docs": "/mnt/nvme/AIWorkspace/vo-merge/dev/vo-merge/CLAUDE.md",
    }
    return summary, ctx


def episode_context(e):
    """(summary, context) describing ONE episode record and every action that can fix it."""
    record = {k: e.get(k) for k in
              ("id", "series_title", "season", "episode", "status", "error", "sync_delta",
               "sync_offset_ms", "candidate_title", "candidate_score", "candidate_seeders",
               "attempts", "tried", "french_path", "en_file", "quality", "dl_hash",
               "need_audio", "need_subs", "audio_langs", "sub_langs")}
    ep_id = e["id"]
    summary = (f"{e['series_title']} S{e['season']:02d}E{e['episode']:02d} — {e['status']}")
    if e.get("error"):
        summary += f": {e['error']}"
    ctx = {
        "record": record,
        "api": "http://10.0.1.5:8090/api (host) / http://localhost:8080/api (in-container)",
        "read_this_first": f"GET /episode/{ep_id}/context — the record, a probe of both files, "
                           "the matching log lines, EVERY donor file with the (season, episode) "
                           "parsed from it, the series' episode list, and a `numbering` block "
                           "(library S/E vs the release's S/E and absolute number). Comparing "
                           "the donor list with the episode list IS the diagnosis for a "
                           "numbering mismatch; `translated: true` means vo-merge already mapped "
                           "aired<->absolute from Sonarr and a plain /retry is the right move.",
        "actions": [
            "GET  /episode/{id}/context — probes, log lines, donor files vs the episode list",
            "GET  /episode/{id}/candidates — list releases (incl. already-tried)",
            "POST /episode/{id}/retry — blocklist current release, drop donor, re-search",
            "POST /episode/{id}/assign {\"path\":\"/abs/file.mkv\"} — map one donor file to this "
            "episode and queue the merge (when automatic numbering translation can't apply)",
            "POST /episode/{id}/sync_probe {\"max_lag_s\":300} — MEASURE the offset without "
            "merging, searching further out than the merge path does. Make this call FIRST on "
            "any \"couldn't sync\": windows that AGREE on a large offset mean extra material at "
            "the head (fixable with --sync), windows that DISAGREE mean a genuinely different "
            "cut (not fixable). Add \"apply\":true to merge with what it finds.",
            "POST /episode/{id}/set_sync {\"offset_ms\":N,\"drift\":1.0427083} — apply a known "
            "offset / rate stretch with no detection",
            "POST /search_releases {\"query\":\"...\"} — arbitrary Prowlarr query (original/"
            "romaji/alternate title, no year) for the no_release backlog",
            "POST /episode/{id}/unfixable {\"reason\":\"...\"} — terminal give-up WITH a reason",
            "POST /episode/{id}/ignore — give up on this episode",
        ],
        "report_back": (
            f"When done, POST /episode/{ep_id}/ai_result with "
            "{\"status\":\"resolved|failed|needs_human\",\"verdict\":\"one line\","
            "\"action_taken\":\"what you did\"} so this leaves the operator's manual-review queue."),
        "docs": "/mnt/nvme/AIWorkspace/vo-merge/dev/vo-merge/CLAUDE.md",
    }
    return summary, ctx


SYSTEM = """You are vo-merge's on-call engineer.

vo-merge is a language companion to Radarr/Sonarr: it probes every library file, works out which
target languages it is missing (`need_audio` / `need_subs`), downloads a release that carries
them, and grafts the missing audio and subtitle tracks into the existing library file — keeping
the library file's (usually better) video, A/V-synced.

You are handed ONE record that failed, and the REST actions that can fix it. Work it end to end:

1. Read `/movie/{id}/context` or `/episode/{id}/context` FIRST. It carries probes of both files,
   the log lines for this record, and (for episodes) every donor file with the season/episode
   parsed from it. It usually IS the diagnosis.
2. Act with the endpoints listed in the brief. Common shapes:
   - "couldn't sync" -> `sync_probe` with a wide `max_lag_s` BEFORE anything else. Windows that
     agree on a large offset = extra material at the head, fixable; windows that disagree = a
     genuinely different cut, not fixable with one offset.
   - framerates differ -> `set_sync` with `drift` = donor_fps/base_fps (25/23.976 = 1.0427083).
   - donor holds different episodes than the record wants -> `assign` the right file per episode.
   - nothing found -> `search_releases` with the original/romaji/alternate title and no year.
   - the release is simply bad -> `another` / `retry` to blocklist it and take the next one.
3. Finish by calling the `report` tool exactly once. `resolved` = you fixed it or queued a fix
   that should work; `failed` = you examined it and it cannot be fixed by these actions;
   `needs_human` = a person must decide. Say what you actually did — a verdict with no action is
   worse than none, because it clears the record out of the operator's review queue.

Be economical: a handful of calls, not an exhaustive sweep. Never guess an offset you have not
measured, and never report `resolved` for something you did not actually change."""


# ---------------------------------------------------------------------------------------------
STATE = {"running": False, "last_run": None, "last_error": None,
         "handled": 0, "resolved": 0, "current": None}
WAKE = threading.Event()
_LOCK = threading.Lock()


def _base(cfg):
    return (cfg.get("ai_api_base") or "http://127.0.0.1:8080/api").rstrip("/")


def _call_api(cfg, method, path, body):
    """The agent's one door into the app. Allowlisted, so a review agent can act on its record
    but cannot re-write settings, start a library pass, or reach the media-deleting repair."""
    method = (method or "GET").upper()
    path = "/" + (path or "").lstrip("/")
    if path.startswith("/api/"):
        path = path[4:]                       # tolerate the model pasting the full prefix
    bare = path.split("?", 1)[0].rstrip("/") or "/"
    if not any(m == method and rx.match(bare) for m, rx in _ALLOW):
        return (f"refused: {method} {bare} is not on this agent's allowlist. You may read "
                "status/movies/logs/dashboard/coverage/library/downloads/ai_log, tv/status, "
                "tv/episodes, {movie,episode}/{id}/{context,candidates}, and act with the "
                "per-record endpoints in your brief. Settings, rescans, library repair, pause "
                "and the bulk retries are deliberately out of reach.")
    url = _base(cfg) + path
    timeout = 900 if _SLOW.search(bare) else 120
    try:
        payload = json.loads(body) if body and body.strip() else None
    except Exception as ex:
        return f"bad request: `body` must be a JSON object, got {ex}"
    try:
        if method == "GET":
            r = requests.get(url, timeout=timeout)
        else:
            r = requests.post(url, json=(payload if payload is not None else {}), timeout=timeout)
    except Exception as ex:
        return f"request failed: {ex}"
    text = core.redact(r.text or "")
    if len(text) > _MAX_RESULT:
        text = text[:_MAX_RESULT] + f"\n…truncated ({len(r.text)} chars total)"
    return f"HTTP {r.status_code}\n{text}"


def _stamp(kind, key, status, verdict):
    if kind == "movie":
        mv = core.get_movie(int(key))
        if mv:
            core.set_status(int(key), mv["status"], ai_status=status,
                            ai_verdict=verdict, ai_at=time.time())
    else:
        e = core.get_episode(key)
        if e:
            core.set_ep_status(key, e["status"], ai_status=status,
                               ai_verdict=verdict, ai_at=time.time())


_STATUSES = ("resolved", "failed", "needs_human")


def _handle(client, cfg, kind, key):
    """Run the agent loop over ONE record. Returns the reported status (or None)."""
    from anthropic import beta_tool

    rec = core.get_movie(int(key)) if kind == "movie" else core.get_episode(key)
    if not rec:
        return None
    summary, ctx = (movie_context(rec) if kind == "movie" else episode_context(rec))
    outcome = {"status": None}

    @beta_tool
    def vo_api(method: str, path: str, body: str = "") -> str:
        """Call one of vo-merge's own REST endpoints (the actions listed in your brief).

        Args:
            method: GET or POST.
            path: the API path, e.g. /movie/1234/context or /episode/1-2-3/sync_probe.
            body: JSON object as a string, for POST bodies. Omit for GET.
        """
        return _call_api(cfg, method, path, body)

    @beta_tool
    def report(status: str, verdict: str, action_taken: str = "") -> str:
        """Record the outcome for this record and finish. Call this exactly once, at the end.

        Args:
            status: resolved (you fixed it), failed (examined, not fixable with these actions),
                or needs_human (a person must decide).
            verdict: one line explaining the outcome.
            action_taken: what you actually did.
        """
        st = (status or "").strip().lower()
        if st not in _STATUSES:
            return f"status must be one of {_STATUSES}"
        line = (verdict or "").strip()
        if action_taken:
            line = f"{line} — {action_taken}".strip(" —")
        outcome["status"] = st
        _stamp(kind, key, st, line[:400] or st)
        core.log(f"ai(builtin) {kind} {key}: {st} — {line[:100]}")
        return "recorded"

    params = dict(
        model=cfg.get("ai_model") or "claude-opus-5",
        max_tokens=8000,
        max_iterations=max(4, int(cfg.get("ai_max_steps", 30))),
        system=SYSTEM,
        tools=[vo_api, report],
        messages=[{"role": "user",
                   "content": f"Record to handle ({kind}): {summary}\n\n"
                              + json.dumps(ctx, ensure_ascii=False, indent=1, default=str)}],
        thinking={"type": "adaptive"},
        output_config={"effort": cfg.get("ai_effort", "medium")},
    )
    try:
        for _ in client.beta.messages.tool_runner(**params):
            pass
    except Exception as ex:
        # An older model rejects adaptive thinking / output_config; retry once plainly rather
        # than making the whole feature depend on the operator picking a current model.
        if re.search(r"thinking|output_config|effort", str(ex), re.I):
            params.pop("thinking", None)
            params.pop("output_config", None)
            for _ in client.beta.messages.tool_runner(**params):
                pass
        else:
            raise
    if not outcome["status"]:
        # It ran out of steps, or stopped without a verdict. Saying nothing would leave the
        # record 'pending' until the staleness sweep guessed for it an hour later.
        _stamp(kind, key, "needs_human",
               "the on-call agent finished without reporting an outcome (out of steps?)")
        core.log(f"ai(builtin) {kind} {key}: no report -> needs_human")
    return outcome["status"]


def _queue(cfg):
    """Records waiting on the AI, oldest first. `ai_health_check` stamps them 'pending'; this is
    the same set the host dispatcher would have found in its ticket."""
    n = max(1, int(cfg.get("ai_max_records", 3)))
    out = []
    with core.db() as c:
        for r in c.execute("SELECT tmdb_id FROM movies WHERE ai_status='pending' "
                           "AND status IN ('error','review','sync_fail') "
                           "ORDER BY COALESCE(ai_at,0) LIMIT ?", (n,)):
            out.append(("movie", str(r["tmdb_id"])))
        for r in c.execute("SELECT id FROM episodes WHERE ai_status='pending' "
                           "AND status IN ('error','sync_fail') "
                           "ORDER BY COALESCE(ai_at,0) LIMIT ?", (n,)):
            out.append(("episode", r["id"]))
    return out[:n]


def run_pending(cfg=None):
    """Work the pending queue. One record at a time, capped per sweep — this spends real money,
    so it is deliberately a trickle rather than a stampede."""
    cfg = cfg or core.load_config()
    if not enabled(cfg):
        return 0
    if cfg.get("paused"):
        return 0            # pausing means "start nothing new", and the agent starts merges
    if not _LOCK.acquire(blocking=False):
        return 0
    done = 0
    try:
        import anthropic
        q = _queue(cfg)
        if not q:
            return 0
        STATE["running"] = True
        client = anthropic.Anthropic(api_key=cfg["anthropic_key"])
        for kind, key in q:
            STATE["current"] = f"{kind} {key}"
            try:
                st = _handle(client, cfg, kind, key)
                done += 1
                STATE["handled"] += 1
                if st == "resolved":
                    STATE["resolved"] += 1
                STATE["last_error"] = None
            except Exception as ex:
                STATE["last_error"] = core.redact(str(ex))[:300]
                core.log(f"ai(builtin) {kind} {key} failed: {core.redact(str(ex))[:200]}")
                _stamp(kind, key, "needs_human",
                       f"the on-call agent errored: {core.redact(str(ex))[:200]}")
    finally:
        STATE["running"] = False
        STATE["current"] = None
        STATE["last_run"] = time.time()
        _LOCK.release()
    return done


def wake():
    """Something was just escalated — don't wait for the next tick."""
    WAKE.set()


def _worker():
    while True:
        WAKE.wait(timeout=300)
        WAKE.clear()
        try:
            run_pending()
        except Exception as ex:
            STATE["last_error"] = core.redact(str(ex))[:300]
            core.log(f"ai agent worker error: {core.redact(str(ex))[:200]}")


_started = False


def start():
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=_worker, name="ai-agent", daemon=True).start()


def status(cfg=None):
    """What the UI needs to say whether the built-in agent is alive and configured."""
    cfg = cfg or core.load_config()
    m = mode(cfg)
    return {"mode": m, "model": cfg.get("ai_model") or "claude-opus-5",
            "sdk": available(), "key": bool(cfg.get("anthropic_key")),
            "ready": enabled(cfg), "running": STATE["running"], "current": STATE["current"],
            "last_run": STATE["last_run"], "last_error": STATE["last_error"],
            "handled": STATE["handled"], "resolved": STATE["resolved"]}
