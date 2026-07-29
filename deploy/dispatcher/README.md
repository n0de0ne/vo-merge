# Resident AI dispatcher (sidecar)

The fixer that consumes vo-merge's tickets, run **where the platform can supervise it** instead
of as a host cron whose death is silent. vo-merge pages every failure into
`/config/ai-tickets/`; this container claims each ticket, runs the Claude Code CLI over it (the
ticket carries its own brief: the record, the API actions, the report-back contract), deletes it
on success, and touches a heartbeat every cycle. vo-merge's own watchdog
(`pipeline.check_dispatcher`) alarms out-of-band when tickets queue and the heartbeat goes
stale — a dead fixer is now an *event*, not a slow discovery.

## Protocol (must match `backend/app/agent.py`)

| Path | Meaning |
|---|---|
| `<tickets>/*.json` | queued — vo-merge wrote it, nothing has looked at it |
| `<tickets>/claimed/*.json` | taken — the `ai_stale_min` timer measures from this rename |
| `<tickets>/dead/*.json` | gave up after `MAX_ATTEMPTS` crashed runs (visible verdict) |
| `<tickets>/.heartbeat` | touched every cycle — the liveness signal |

Deleting the claimed file is the ack; the agent's `POST …/ai_result` callback is the outcome.

## Run it

```yaml
# docker-compose.yml, next to vo-merge
  vo-merge-dispatcher:
    build: ./deploy/dispatcher          # or your registry copy
    restart: unless-stopped
    environment:
      - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
      - VO_URL=http://10.0.1.5:8090/api          # cross-watch: alarm when vo-merge is down
      - NOTIFY_URL=${NOTIFY_URL}                  # same value as vo-merge's notify_url
      # AGENT_CMD=claude -p --dangerously-skip-permissions   # default
      # AGENT_TIMEOUT_S=1800  POLL_S=30  RETRY_MIN=45  MAX_ATTEMPTS=3  VO_DOWN_ALARM_MIN=15
    volumes:
      - /mnt/user/appdata/vo-merge/ai-tickets:/tickets
```

## Who watches the watcher

The two processes watch **each other**: vo-merge alarms when this loop's heartbeat goes stale
with tickets queued (`pipeline.check_dispatcher`), and this loop pings vo-merge's `/api/health`
(exempt from `api_key`) and fires the same out-of-band alarm when vo-merge itself is the thing
that's down — the one failure vo-merge's own watchdogs can never report. Set `VO_URL` +
`NOTIFY_URL` to enable it. The residual blind spot is the whole HOST dying, which no software on
the host can report: if that matters, point an external uptime ping (healthchecks.io, another
machine's Uptime-Kuma) at either `/api/health` or the notify channel's silence.

Unraid: install as a container from this Dockerfile with the same single volume mapping and the
API key variable; leave the default restart policy on. The agent reaches vo-merge over the LAN
via the `api_url` each ticket carries — set **Settings → `api_url`** (e.g.
`http://10.0.1.5:8090`) and, if you set an `api_key`, note the ticket briefs tell the agent to
send it.

The CLI authenticates with `ANTHROPIC_API_KEY`, or mount an existing login
(`~/.claude:/root/.claude`).

## Keeping the old host cron instead

Everything still works with the legacy Unraid user script — per-record tickets are the same
shape it already handles, and it simply never writes a heartbeat. Raise
`ai_dispatcher_alarm_min` comfortably above its cron interval so a healthy-but-hourly cron
doesn't false-alarm; the alarm then keys off tickets aging alone. If the script also starts
touching `<tickets>/.heartbeat` each run and claiming via `claimed/`, it gets the full
dead-vs-slow discrimination and crash retry accounting.
