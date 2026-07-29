#!/usr/bin/env python3
"""The resident AI dispatcher — vo-merge's fixer, run WHERE THE PLATFORM CAN SUPERVISE IT.

The original dispatcher was an Unraid user-script cron on the host: when it stopped, nothing
reported anything, tickets piled up, and the whole backlog aged into "needs_human" — a dead
fixer indistinguishable from one that examined everything and gave up. Running this loop as a
container (restart: unless-stopped + the HEALTHCHECK in the Dockerfile) makes the *platform*
restart it, and the heartbeat lets vo-merge's own watchdog tell dead from slow.

Protocol (shared with backend/app/agent.py — keep the two in sync):
  - vo-merge writes one JSON ticket per record into TICKETS (its /config/ai-tickets/);
  - this loop touches TICKETS/.heartbeat every cycle, work or no work — that mtime is the
    liveness signal;
  - it CLAIMS a ticket by renaming it into claimed/ (the moment vo-merge's ai_stale_min timer
    starts counting), runs the agent CLI over it, and DELETES it on success;
  - a claimed ticket left behind is a crashed/failed run: after RETRY_MIN it goes back to the
    queue with an attempt counter, and after MAX_ATTEMPTS it lands in dead/ — visible in the
    On-call AI panel rather than silently retried forever.

The agent itself is the Claude Code CLI (or anything else set via AGENT_CMD) pointed at the
ticket's own brief: every ticket already carries the API base, the record, the action surface
and the report-back instruction, so the prompt here only frames it.

This loop is also THE WATCHER OF THE WATCHER. Every alarm vo-merge can raise lives inside
vo-merge — so when vo-merge itself is down (crashloop, dead container, refused port), nothing
anywhere could say so. The two processes now watch each other: vo-merge watches this loop via
the heartbeat, and this loop pings vo-merge's /api/health (exempt from api_key by design) and
raises the out-of-band alarm itself when it stays unreachable past VO_DOWN_ALARM_MIN. A
whole-host outage still needs a ping from OUTSIDE the box (healthchecks.io, another machine's
Uptime-Kuma) — no in-host software can report the host's own death.

Environment:
  TICKETS         ticket directory (default /tickets — mount vo-merge's ai-tickets here)
  AGENT_CMD       agent command; the prompt is piped to stdin
                  (default: claude -p --dangerously-skip-permissions)
  AGENT_TIMEOUT_S kill a run after this long (default 1800)
  POLL_S          idle poll interval (default 30)
  RETRY_MIN       minutes before a crashed (still-claimed) ticket is requeued (default 45)
  MAX_ATTEMPTS    runs before a ticket is moved to dead/ (default 3)
  VO_URL          vo-merge API base for the cross-watch, e.g. http://10.0.1.5:8090/api
                  (empty = cross-watch off)
  NOTIFY_URL      where the vo-merge-down alarm goes (same shapes vo-merge's notify.py takes:
                  ntfy topic, Discord/Slack webhook)
  VO_DOWN_ALARM_MIN  minutes of continuous unreachability before alarming (default 15)
  ANTHROPIC_API_KEY (or a mounted ~/.claude) — the CLI's own auth
"""
import json
import os
import shlex
import subprocess
import sys
import time
import urllib.request

TICKETS = os.environ.get("TICKETS", "/tickets")
CLAIMED = os.path.join(TICKETS, "claimed")
DEAD = os.path.join(TICKETS, "dead")
HEARTBEAT = os.path.join(TICKETS, ".heartbeat")
AGENT_CMD = os.environ.get("AGENT_CMD", "claude -p --dangerously-skip-permissions")
AGENT_TIMEOUT_S = int(os.environ.get("AGENT_TIMEOUT_S", "1800"))
POLL_S = int(os.environ.get("POLL_S", "30"))
RETRY_MIN = int(os.environ.get("RETRY_MIN", "45"))
MAX_ATTEMPTS = int(os.environ.get("MAX_ATTEMPTS", "3"))
VO_URL = os.environ.get("VO_URL", "").rstrip("/")
NOTIFY_URL = os.environ.get("NOTIFY_URL", "")
VO_DOWN_ALARM_MIN = int(os.environ.get("VO_DOWN_ALARM_MIN", "15"))


def log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def beat():
    """Touch the heartbeat. Every cycle, work or none — its absence/staleness is exactly what
    vo-merge's check_dispatcher alarms on."""
    with open(HEARTBEAT, "a"):
        os.utime(HEARTBEAT, None)


def queued():
    try:
        names = [n for n in os.listdir(TICKETS)
                 if n.endswith(".json") and os.path.isfile(os.path.join(TICKETS, n))]
    except OSError:
        return []
    # oldest first — FIFO keeps the ai_stale_min ordering honest
    return sorted(names, key=lambda n: os.path.getmtime(os.path.join(TICKETS, n)))


def requeue_crashed():
    """A ticket still sitting in claimed/ past RETRY_MIN is a run that died. Send it back with
    its attempt count bumped; past MAX_ATTEMPTS it goes to dead/ — a visible verdict ('the
    agent crashed on this N times'), not an infinite silent retry."""
    now = time.time()
    try:
        names = os.listdir(CLAIMED)
    except OSError:
        return
    for n in names:
        if not n.endswith(".json"):
            continue
        p = os.path.join(CLAIMED, n)
        try:
            if now - os.path.getmtime(p) < RETRY_MIN * 60:
                continue
            with open(p) as f:
                t = json.load(f)
        except Exception:
            t = {}
        attempts = int(t.get("_attempts", 0)) + 1
        t["_attempts"] = attempts
        try:
            if attempts >= MAX_ATTEMPTS:
                os.makedirs(DEAD, exist_ok=True)
                with open(os.path.join(DEAD, n), "w") as f:
                    json.dump(t, f, indent=1, default=str)
                os.remove(p)
                log(f"dead-letter {n} after {attempts} attempts")
            else:
                with open(os.path.join(TICKETS, n), "w") as f:
                    json.dump(t, f, indent=1, default=str)
                os.remove(p)
                log(f"requeued crashed ticket {n} (attempt {attempts})")
        except OSError as e:
            log(f"requeue {n} failed: {e}")


def notify(title, body):
    """Best-effort POST to NOTIFY_URL, matching vo-merge's own notify.py payload shapes."""
    if not NOTIFY_URL:
        return
    u = NOTIFY_URL.lower()
    try:
        if "discord.com/api/webhooks" in u or "discordapp.com/api/webhooks" in u:
            data = json.dumps({"content": f"**{title}**\n{body}"[:1900]}).encode()
            req = urllib.request.Request(NOTIFY_URL, data=data,
                                         headers={"Content-Type": "application/json"})
        elif "hooks.slack.com" in u:
            data = json.dumps({"text": f"*{title}*\n{body}"[:2900]}).encode()
            req = urllib.request.Request(NOTIFY_URL, data=data,
                                         headers={"Content-Type": "application/json"})
        else:
            req = urllib.request.Request(NOTIFY_URL, data=body.encode(),
                                         headers={"Title": title, "Priority": "high"})
        urllib.request.urlopen(req, timeout=10)
        log(f"notified: {title}")
    except Exception as e:
        log(f"notify failed: {e}")


_VO = {"down_since": None, "alarmed": False}


def check_vo():
    """The cross-watch: vo-merge's watchdogs cannot report vo-merge's own death. One /health
    probe per cycle; a sustained failure raises the alarm ONCE per outage, and recovery both
    announces itself and re-arms."""
    if not VO_URL:
        return
    try:
        urllib.request.urlopen(f"{VO_URL}/health", timeout=10)
    except Exception as e:
        now = time.time()
        if _VO["down_since"] is None:
            _VO["down_since"] = now
            log(f"vo-merge unreachable ({e})")
        elif not _VO["alarmed"] and now - _VO["down_since"] > VO_DOWN_ALARM_MIN * 60:
            _VO["alarmed"] = True
            notify("vo-merge: vo-merge itself is DOWN",
                   f"{VO_URL}/health has been unreachable for "
                   f"{int((now - _VO['down_since']) / 60)}min ({e}). The pipeline and all of "
                   f"its own alarms are offline; check the vo-merge container.")
        return
    if _VO["alarmed"]:
        notify("vo-merge: back up",
               f"{VO_URL}/health answers again after "
               f"{int((time.time() - (_VO['down_since'] or time.time())) / 60)}min down.")
    if _VO["down_since"]:
        log("vo-merge reachable again")
    _VO.update(down_since=None, alarmed=False)


def prompt_for(ticket):
    """Frame the ticket. The brief inside it is authoritative — vo-merge writes the API base,
    the record, the actions and the report-back contract into every ticket, so the framing only
    has to insist on the two behaviours that keep the loop closed."""
    return (
        "You are the on-call maintenance agent for vo-merge, a media-library language merger. "
        "The JSON ticket below describes ONE failed record and every REST action available to "
        "fix it. Diagnose (start with the read_this_first call if present), act via the API, "
        "and ALWAYS finish by POSTing the ai_result callback described in report_back — the "
        "callback is the only signal the system has that you ran. If the record cannot be "
        "fixed, use the unfixable action with a one-line reason rather than leaving it. Do not "
        "modify library files directly; act only through the API.\n\n"
        f"TICKET:\n{json.dumps(ticket, indent=1, default=str)}\n"
    )


def run_one(name):
    src = os.path.join(TICKETS, name)
    dst = os.path.join(CLAIMED, name)
    os.makedirs(CLAIMED, exist_ok=True)
    try:
        os.rename(src, dst)          # the claim: atomic, and the moment the stale-timer starts
    except OSError:
        return                       # raced another consumer / vo-merge withdrew it
    try:
        with open(dst) as f:
            ticket = json.load(f)
    except Exception as e:
        log(f"{name}: unreadable ({e}) -> dead-letter")
        os.makedirs(DEAD, exist_ok=True)
        os.replace(dst, os.path.join(DEAD, name))
        return
    log(f"run {name}: {str(ticket.get('summary', ''))[:80]}")
    t0 = time.time()
    try:
        r = subprocess.run(shlex.split(AGENT_CMD), input=prompt_for(ticket),
                           capture_output=True, text=True, timeout=AGENT_TIMEOUT_S)
        ok = r.returncode == 0
        tail = (r.stdout or r.stderr or "").strip()[-400:]
    except subprocess.TimeoutExpired:
        ok, tail = False, f"agent exceeded {AGENT_TIMEOUT_S}s and was killed"
    except Exception as e:
        ok, tail = False, f"agent failed to start: {e}"
    if ok:
        try:
            os.remove(dst)           # the ack
        except OSError:
            pass
        log(f"done {name} in {int(time.time() - t0)}s")
    else:
        # leave it claimed: requeue_crashed() retries it with the attempt counter
        log(f"FAILED {name} in {int(time.time() - t0)}s: {tail}")


def main():
    os.makedirs(TICKETS, exist_ok=True)
    log(f"dispatcher up: dir={TICKETS} cmd={AGENT_CMD!r} "
        f"retry={RETRY_MIN}min x{MAX_ATTEMPTS}")
    while True:
        try:
            beat()
            check_vo()
            requeue_crashed()
            names = queued()
            if names:
                run_one(names[0])
                beat()               # a long run must not read as a dead dispatcher
                continue             # drain before idling
        except Exception as e:
            log(f"loop error: {e}")
        time.sleep(POLL_S)


if __name__ == "__main__":
    sys.exit(main())
