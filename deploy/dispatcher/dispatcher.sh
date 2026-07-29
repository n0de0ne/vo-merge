#!/bin/bash
# vo-merge resident dispatcher — HOST edition (Unraid User Scripts, bash only).
#
# Runs the Claude Code CLI that is ALREADY LOGGED IN on this host — i.e. your Claude
# subscription, not API-key billing — over vo-merge's per-record tickets. Same take/ack
# protocol as dispatcher.py and backend/app/agent.py, which is what the legacy user-script
# cron never spoke:
#   - touch .heartbeat every cycle (vo-merge's check_dispatcher tells dead from slow with it);
#   - CLAIM a ticket by mv into claimed/ (the moment the ai_stale_min timer starts);
#   - delete it on success; a claimed ticket left behind is a crashed run, requeued after
#     RETRY_MIN with an attempt counter and dead-lettered into dead/ after MAX_ATTEMPTS;
#   - cross-watch: ping vo-merge's /api/health and raise the out-of-band alarm when vo-merge
#     itself is down — the one failure vo-merge's own watchdogs can never report.
#
# Install (User Scripts plugin):
#   - new script -> paste this file -> schedule "At First Array Start Only" (it loops forever);
#   - or schedule it on a cron with ONESHOT=1 (drains the queue once per firing; keep
#     vo-merge's ai_dispatcher_alarm_min above your cron interval).
# Settings: edit the defaults below, or drop overrides in /boot/config/vo-dispatcher.conf
# (plain `VAR=value` lines; it survives reboots).
#
# The CLI must be reachable from the User Scripts environment — if `claude` isn't on cron's
# PATH, set AGENT_CMD to its absolute path (e.g. /usr/local/bin/claude).

TICKETS="${TICKETS:-/mnt/user/appdata/vo-merge/ai-tickets}"
AGENT_CMD="${AGENT_CMD:-claude -p --dangerously-skip-permissions}"
AGENT_TIMEOUT_S="${AGENT_TIMEOUT_S:-1800}"
POLL_S="${POLL_S:-30}"
RETRY_MIN="${RETRY_MIN:-45}"
MAX_ATTEMPTS="${MAX_ATTEMPTS:-3}"
VO_URL="${VO_URL-http://127.0.0.1:8090/api}"    # explicitly empty = cross-watch off
NOTIFY_URL="${NOTIFY_URL-}"                     # same value as vo-merge's notify_url
VO_DOWN_ALARM_MIN="${VO_DOWN_ALARM_MIN:-15}"
ONESHOT="${ONESHOT:-0}"                         # 1 = single pass (cron mode), 0 = loop forever
[ -f /boot/config/vo-dispatcher.conf ] && . /boot/config/vo-dispatcher.conf

CLAIMED="$TICKETS/claimed"
DEAD="$TICKETS/dead"
HB="$TICKETS/.heartbeat"
mkdir -p "$TICKETS" "$CLAIMED" "$DEAD"

log() { echo "[$(date '+%F %T')] $*"; }

# One dispatcher at a time — User Scripts happily starts a second copy, and two consumers
# racing the same mv-claim is survivable but pointless.
exec 9>"$TICKETS/.dispatcher.lock"
if ! flock -n 9; then
    log "another dispatcher holds the lock — exiting"
    exit 0
fi

notify() {  # $1 = title, $2 = body — same payload shapes as backend/app/notify.py.
    [ -n "$NOTIFY_URL" ] || return 0
    case "$NOTIFY_URL" in
        *discord.com/api/webhooks*|*discordapp.com/api/webhooks*)
            curl -fsS --max-time 10 -H 'Content-Type: application/json' \
                 -d "{\"content\":\"**vo-merge: $1**\\n$2\"}" "$NOTIFY_URL" >/dev/null ;;
        *hooks.slack.com*)
            curl -fsS --max-time 10 -H 'Content-Type: application/json' \
                 -d "{\"text\":\"*vo-merge: $1* — $2\"}" "$NOTIFY_URL" >/dev/null ;;
        *)  curl -fsS --max-time 10 -H "Title: vo-merge: $1" -H "Priority: high" \
                 -d "$2" "$NOTIFY_URL" >/dev/null ;;
    esac || log "notify failed: $1"
}

check_vo() {
    [ -n "$VO_URL" ] || return 0
    local now since
    now=$(date +%s)
    if curl -fsS --max-time 10 "${VO_URL%/}/health" >/dev/null 2>&1; then
        [ -f "$TICKETS/.vo-alarmed" ] && notify "back up" "vo-merge answers again"
        [ -f "$TICKETS/.vo-down-since" ] && log "vo-merge reachable again"
        rm -f "$TICKETS/.vo-down-since" "$TICKETS/.vo-alarmed"
        return 0
    fi
    if [ ! -f "$TICKETS/.vo-down-since" ]; then
        echo "$now" > "$TICKETS/.vo-down-since"
        log "vo-merge unreachable"
    elif [ ! -f "$TICKETS/.vo-alarmed" ]; then
        since=$(cat "$TICKETS/.vo-down-since" 2>/dev/null || echo "$now")
        if [ $(( now - since )) -gt $(( VO_DOWN_ALARM_MIN * 60 )) ]; then
            touch "$TICKETS/.vo-alarmed"
            notify "vo-merge itself is DOWN" \
                   "unreachable for $(( (now - since) / 60 ))min. The pipeline and all of its own alarms are offline — check the vo-merge container."
        fi
    fi
}

requeue_crashed() {
    # A ticket still in claimed/ past RETRY_MIN is a run that died. The attempt counter lives
    # in a hidden sidecar file (not .json, so vo-merge's claimed-count never miscounts it) and
    # survives the round-trip back to the queue.
    local f name mt attempts cf now
    now=$(date +%s)
    for f in "$CLAIMED"/*.json; do
        [ -e "$f" ] || continue
        mt=$(stat -c %Y "$f" 2>/dev/null) || continue
        [ $(( now - mt )) -ge $(( RETRY_MIN * 60 )) ] || continue
        name=$(basename "$f")
        cf="$CLAIMED/.$name.attempts"
        attempts=$(( $(cat "$cf" 2>/dev/null || echo 0) + 1 ))
        if [ "$attempts" -ge "$MAX_ATTEMPTS" ]; then
            mv "$f" "$DEAD/$name" && rm -f "$cf"
            log "dead-letter $name after $attempts attempts"
        else
            echo "$attempts" > "$cf"
            mv "$f" "$TICKETS/$name"
            log "requeued crashed ticket $name (attempt $attempts)"
        fi
    done
}

run_one() {  # $1 = ticket filename
    local name="$1" dst rc t0 out
    dst="$CLAIMED/$name"
    mv "$TICKETS/$name" "$dst" 2>/dev/null || return 0   # raced / withdrawn by vo-merge
    log "run $name"
    t0=$(date +%s)
    out=$(mktemp /tmp/vo-dispatch.XXXXXX)
    {
        cat <<'FRAMING'
You are the on-call maintenance agent for vo-merge, a media-library language merger. The JSON
ticket below describes ONE failed record and every REST action available to fix it. Diagnose
first (start with the read-this-first /context call if present), act via the API, and ALWAYS
finish by POSTing the ai_result callback described in report_back — the callback is the only
signal the system has that you ran. If the record cannot be fixed, use the unfixable action
with a one-line reason rather than leaving it. Do not modify library files directly; act only
through the API.

TICKET:
FRAMING
        cat "$dst"
    } | timeout "$AGENT_TIMEOUT_S" $AGENT_CMD >"$out" 2>&1
    rc=$?
    if [ $rc -eq 0 ]; then
        rm -f "$dst" "$CLAIMED/.$name.attempts"
        log "done $name in $(( $(date +%s) - t0 ))s"
    else
        # leave it claimed — requeue_crashed retries it with the attempt counter
        log "FAILED $name (rc=$rc) in $(( $(date +%s) - t0 ))s: $(tail -c 300 "$out" | tr '\n' ' ')"
    fi
    rm -f "$out"
}

pass() {
    touch "$HB"
    check_vo
    requeue_crashed
    # oldest first (FIFO keeps the ai_stale_min ordering honest). Ticket names carry no
    # whitespace (review-m123.json / review-e9:1:5.json), so ls-into-for is safe here.
    local name
    for name in $(ls -tr "$TICKETS" 2>/dev/null | grep '\.json$'); do
        run_one "$name"
        touch "$HB"          # a long agent run must not read as a dead dispatcher
    done
}

log "dispatcher up: dir=$TICKETS cmd='$AGENT_CMD' mode=$([ "$ONESHOT" = 1 ] && echo oneshot || echo loop)"
if [ "$ONESHOT" = "1" ]; then
    pass
    exit 0
fi
while true; do
    pass
    sleep "$POLL_S"
done
