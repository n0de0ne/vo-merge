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

The **successor of the original `ai-dispatch` user script** — replace that script's content
with this one. Everything it proved out is carried over: multi-app ticket dirs
(sonarr-completor too), **session resume** of interrupted runs (`--resume` + the
half-applied-action warning), the **idle-vs-hard timeout** watchdog (silence kills a wedged
run fast; a legitimately slow sync_probe survives), the **daily run budget** with a once-a-day
warning, **Unraid-native notifications**, the **dispatcher-side `ai_result` callback** (a run
that forgot to report still reports), session-loss detection after a plugin reinstall,
transcript archiving to `done/`, the CLI/HOME liveness alarms, and `--allowedTools` instead of
`--dangerously-skip-permissions` (which the CLI refuses for root). New on top: the heartbeat,
claim-while-running, `dead/`, and the vo-merge-down cross-watch.

1. **User Scripts** → open the old ai-dispatch script → replace its content with
   `dispatcher.sh` (one consumer per queue — don't run both).
2. Schedule **"At First Array Start Only"** for loop mode (30 s poll, flock-guarded), or keep
   the old cron cadence with `ONESHOT=1` (then keep vo-merge's `ai_dispatcher_alarm_min` above
   the cron interval). An hourly cron in loop mode also works and doubles as a supervisor:
   each firing either becomes the daemon or exits on the lock.
3. Settings: edit the variables at the top, or persist them in
   `/boot/config/vo-dispatcher.conf` (sourced bash) — a config matching the original setup:

   ```bash
   TICKET_DIRS="/mnt/user/appdata/vo-merge/ai-tickets /mnt/user/appdata/sonarr-completor/ai-tickets"
   CLAUDE=/usr/local/emhttp/plugins/unraid-aicliagents/bin/claude
   HOME_DIR=/tmp/unraid-aicliagents/work/root/home
   WORKDIR=/mnt/nvme/AIWorkspace
   PROMPT_FILE=/mnt/user/appdata/ai-dispatch/prompt.md      # your existing guardrails file
   VO_URL=http://10.0.1.3:8090/api
   NOTIFY_URL=<same value as vo-merge's notify_url>
   ```

   `DISPATCH` (state/log/transcripts) defaults to `/mnt/user/appdata/ai-dispatch`, so the run
   budget, attempt state and `done/` archive live where the old script kept them.
4. The outcome contract is unchanged: the agent ends with `STATUS:` / `DIAGNOSIS:` / `ACTION:`
   lines (your existing `prompt.md` already instructs this; the built-in framing used when the
   file is absent does too).

The agent reaches vo-merge over the LAN via the `api_url` each ticket carries — set
**Settings → `api_url`** (e.g. `http://10.0.1.3:8090`).

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
