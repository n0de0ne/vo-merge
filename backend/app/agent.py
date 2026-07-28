"""The on-call AI's brief, and whether the host dispatcher is actually consuming it.

The dispatcher is an Unraid user script on a cron that runs the Claude Code CLI over the ticket
files vo-merge writes into /config/ai-tickets/. It lives outside this app, so the only evidence
we have that it exists at all is second-hand — and the failure mode is silent: when the script
stops running, tickets pile up, every record ages past `ai_stale_min`, and the whole backlog
flips to "AI did not respond within 60m", which reads exactly like an agent that examined each
one and gave up. That is the state this install was found in.

Two things live here, both aimed at that:

- **The brief itself** (`movie_context` / `episode_context`). It used to be written inline in
  main.py's escalation endpoints and again, differently, in `pipeline.ai_health_check`. One copy
  means an operator-escalated record and an auto-escalated one can't be given different
  instructions.
- **`status()`** — what the ticket directory actually looks like: how many tickets are sitting
  there and how long the oldest has waited. `core.ticket` refuses to overwrite a ticket of the
  same kind ("already awaiting dispatch"), so a file that has been there for days is the closest
  thing to direct evidence that nothing on the host is reading them.
"""
import json
import os
import time

from . import core


def enabled(cfg):
    return bool(cfg.get("ai_tickets", True))


# ---------------------------------------------------------------------------------------------
# The ticket payload. Built here rather than in main.py so an operator escalation and the
# automatic sweep hand the dispatcher byte-for-byte the same brief.

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
            "(needs_human = a person must decide). This callback is the ONLY signal vo-merge has "
            "that the dispatcher ran at all — a record with no callback is flagged for a human "
            "after ai_stale_min minutes, indistinguishable from one you examined and gave up on."),
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
            "\"action_taken\":\"what you did\"} so this leaves the operator's manual-review queue. "
            "This callback is the ONLY signal vo-merge has that the dispatcher ran at all."),
        "docs": "/mnt/nvme/AIWorkspace/vo-merge/dev/vo-merge/CLAUDE.md",
    }
    return summary, ctx


TICKET_DIR = os.path.join(core.CONFIG_DIR, "ai-tickets")


def _ticket_files():
    try:
        return [os.path.join(TICKET_DIR, n) for n in os.listdir(TICKET_DIR)
                if n.endswith(".json")]
    except OSError:
        return []                # no ticket has ever been filed; the dir is created lazily


def undispatched():
    """Records whose ticket is STILL SITTING in the directory, i.e. the dispatcher has not
    reached them yet. Keys are "movie:<tmdb_id>" / "episode:<ep_id>".

    This is what stops the `ai_stale_min` sweep from lying. The dispatcher handles a couple of
    tickets per cron firing, so a backlog bigger than its throughput — one bad season pack is
    400 episodes — guarantees that most records sit untouched for well over an hour. Ageing
    those out to `needs_human` reports "the AI examined this and gave up" about a record no
    agent has yet opened. The timeout should measure how long the DISPATCHER has had it, not
    how long the ticket has existed."""
    keys = set()
    for path in _ticket_files():
        name = os.path.basename(path)[:-5]
        if name.startswith("review-m"):
            keys.add(f"movie:{name[8:]}")
            continue
        if name.startswith("review-e"):
            keys.add(f"episode:{name[8:]}")
            continue
        try:                     # errors-review.json covers a whole batch; read who is in it
            t = json.load(open(path))
        except Exception:
            continue
        for rec in (t.get("context") or {}).get("records") or []:
            if rec.get("type") and rec.get("id") is not None:
                keys.add(f"{rec['type']}:{rec['id']}")
    return keys


def status(cfg=None):
    """Is the host dispatcher consuming what we write?

    `core.ticket` will not overwrite a ticket of the same kind — "already awaiting dispatch" —
    so the dispatcher is expected to remove each file once it has picked it up. A ticket sitting
    there for hours is therefore the most direct evidence available from inside the container
    that nothing on the host is reading them. It is evidence, not proof (a dispatcher could be
    running and leaving the files), so the UI pairs it with `last_callback`, which is."""
    cfg = cfg or core.load_config()
    waiting, oldest = 0, None
    try:
        now = time.time()
        for name in os.listdir(TICKET_DIR):
            if not name.endswith(".json"):
                continue
            waiting += 1
            age = now - os.path.getmtime(os.path.join(TICKET_DIR, name))
            oldest = age if oldest is None else max(oldest, age)
    except OSError:
        pass                     # no tickets have ever been filed — the directory is created lazily
    return {"enabled": enabled(cfg), "dir": TICKET_DIR,
            "waiting": waiting, "oldest_age": oldest}
