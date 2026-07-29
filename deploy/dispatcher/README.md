# Resident AI dispatcher

The fixer that consumes vo-merge's tickets, run **where it can be supervised** instead of as a
cron whose death is silent. vo-merge pages every failure into `/config/ai-tickets/`
(`appdata/vo-merge/ai-tickets/` on the host); the dispatcher claims each ticket, runs the
Claude Code CLI over it (the ticket carries its own brief: the record, the API actions, the
report-back contract), deletes it on success, and touches a heartbeat every cycle. vo-merge's
watchdog (`pipeline.check_dispatcher`) alarms out-of-band when tickets queue and the heartbeat
goes stale — a dead fixer is an *event*, not a slow discovery.

Two editions, same protocol. **The host edition is the recommended one**: it runs the `claude`
CLI already logged in on the host, i.e. **your Claude subscription** — no `ANTHROPIC_API_KEY`,
no per-token billing.

## Option A (recommended): on the Unraid host — `dispatcher.sh`

Uses the host's existing CLI login. This is the modern replacement for the legacy
"run the CLI from a user-script cron" setup — same tickets directory, but it now speaks the
full protocol (heartbeat, claim/ack, crash retry, dead-letter), which is what lets vo-merge
tell a dead dispatcher from a slow one.

1. **User Scripts** plugin → add a new script → paste `dispatcher.sh`.
2. Schedule **"At First Array Start Only"** — it loops forever (30 s poll) and takes a lock, so
   double-starts are harmless. Prefer a cron? Schedule it hourly with `ONESHOT=1` and keep
   vo-merge's `ai_dispatcher_alarm_min` above the cron interval.
3. Settings: edit the variables at the top, or persist overrides in
   `/boot/config/vo-dispatcher.conf` (plain `VAR=value` lines):
   - `TICKETS` — default `/mnt/user/appdata/vo-merge/ai-tickets`
   - `AGENT_CMD` — default `claude -p --dangerously-skip-permissions`; use the absolute path if
     `claude` isn't on cron's PATH
   - `VO_URL` — default `http://127.0.0.1:8090/api` (the cross-watch; empty = off)
   - `NOTIFY_URL` — same value as vo-merge's `notify_url`, for the vo-merge-down alarm
4. Retire the legacy ticket-handling user script — two consumers race the same queue.

The agent reaches vo-merge over the LAN via the `api_url` each ticket carries — set
**Settings → `api_url`** (e.g. `http://10.0.1.5:8090`).

## Option B: as a container — `dispatcher.py` + `Dockerfile`

For non-Unraid hosts, or when you want the platform (`restart: unless-stopped` + healthcheck)
to supervise the process itself. Auth is **either** your subscription — mount the host's CLI
login — **or** an API key:

```yaml
  vo-merge-dispatcher:
    build: ./deploy/dispatcher
    restart: unless-stopped
    environment:
      - VO_URL=http://10.0.1.5:8090/api          # cross-watch: alarm when vo-merge is down
      - NOTIFY_URL=${NOTIFY_URL}                  # same value as vo-merge's notify_url
      # - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}  # only if NOT mounting the login below
      # AGENT_CMD / AGENT_TIMEOUT_S / POLL_S / RETRY_MIN / MAX_ATTEMPTS / VO_DOWN_ALARM_MIN
    volumes:
      - /mnt/user/appdata/vo-merge/ai-tickets:/tickets
      - /root/.claude:/root/.claude               # subscription auth (claude login on the host)
```

## Protocol (must match `backend/app/agent.py` — both editions speak it)

| Path | Meaning |
|---|---|
| `<tickets>/*.json` | queued — vo-merge wrote it, nothing has looked at it |
| `<tickets>/claimed/*.json` | taken — the `ai_stale_min` timer measures from this rename |
| `<tickets>/dead/*.json` | gave up after `MAX_ATTEMPTS` crashed runs (visible verdict) |
| `<tickets>/.heartbeat` | touched every cycle — the liveness signal |

Deleting the claimed file is the ack; the agent's `POST …/ai_result` callback is the outcome.

## Who watches the watcher

The two processes watch **each other**: vo-merge alarms when the dispatcher's heartbeat goes
stale with tickets queued (`pipeline.check_dispatcher`), and the dispatcher pings vo-merge's
`/api/health` (exempt from `api_key`) and fires the same out-of-band alarm when vo-merge itself
is the thing that's down — the one failure vo-merge's own watchdogs can never report. The
residual blind spot is the whole HOST dying, which no software on the host can report: if that
matters, point an external uptime ping (healthchecks.io, another machine's Uptime-Kuma) at
`/api/health`.
