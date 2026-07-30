#!/bin/bash
# vo-merge resident dispatcher — HOST edition (Unraid User Scripts, bash only).
#
# The successor of the original ai-dispatch user script: same Claude Code CLI already logged in
# on this host (your subscription, not API-key billing), same machinery that script proved out —
# multi-app ticket dirs, SESSION RESUME of interrupted runs, an idle-vs-hard timeout watchdog,
# a daily run budget, Unraid-native notifications, and the dispatcher-side ai_result callback so
# a run that forgot to report still reports — plus the protocol vo-merge's own watchdog now
# expects (backend/app/agent.py):
#   - touch <tickets>/.heartbeat every pass (check_dispatcher tells dead from slow with it);
#   - CLAIM a ticket by mv into claimed/ while a run is active; an unfinished ticket goes BACK
#     to the queue between attempts, so vo-merge keeps the record 'pending' instead of flagging
#     it while work is still in progress. Anything left in claimed/ at pass start is a crashed
#     dispatcher — requeued (its attempt was already counted before the run, so a reboot can't
#     hand a wedged ticket free retries);
#   - a ticket out of attempts goes to dead/ (visible verdict) after a gave-up callback;
#   - cross-watch: ping vo-merge's /api/health and alarm when vo-merge ITSELF is down — the one
#     failure vo-merge's own watchdogs can never report.
#
# A ticket is archived ONLY when its run finished properly (rc=0 AND a STATUS: line). Anything
# else — killed, crashed, rebooted, no verdict — resumes the SAME Claude session next time, so
# work is continued rather than restarted, and unfinished tickets are picked before new ones.
#
# Install (User Scripts): paste, then either schedule "At First Array Start Only" (loop mode:
# 30 s poll, flock-guarded) or a cron with ONESHOT=1 (their old model: PER_INVOCATION tickets
# per firing). Hourly cron in LOOP mode also works and doubles as a supervisor: each firing
# either becomes the daemon or exits on the lock.
#
# Settings: edit below, or persist overrides in /boot/config/vo-dispatcher.conf (sourced bash).

DISPATCH="${DISPATCH:-/mnt/user/appdata/ai-dispatch}"
# Every app that files tickets. Add sonarr-completor's dir here if it pages the same agent.
TICKET_DIRS=(${TICKET_DIRS[@]:-/mnt/user/appdata/vo-merge/ai-tickets})
CLAUDE="${CLAUDE:-claude}"                 # absolute path if not on cron's PATH, e.g.
                                           # /usr/local/emhttp/plugins/unraid-aicliagents/bin/claude
HOME_DIR="${HOME_DIR-}"                    # export HOME before running the CLI (the AI CLI Agents
                                           # plugin keeps its login under /tmp/...; set this to it)
# --dangerously-skip-permissions is REFUSED for root, so grant tools explicitly. Bash is what
# makes the ai_result callback and API actions possible — WebFetch cannot POST.
ALLOWED_TOOLS="${ALLOWED_TOOLS:-Bash,Read,Write,Edit,Glob,Grep,WebFetch,WebSearch}"
WORKDIR="${WORKDIR:-$DISPATCH/work}"       # cwd for the CLI (its CLAUDE.md / memory lives here)
PROMPT_FILE="${PROMPT_FILE:-$DISPATCH/prompt.md}"   # guardrails prepended to fresh runs; a
                                           # built-in framing is used when the file is absent
MAX_RUNS="${MAX_RUNS:-100}"                # claude runs per day
PER_INVOCATION="${PER_INVOCATION:-2}"      # tickets handled per pass/firing
MAX_ATTEMPTS="${MAX_ATTEMPTS:-3}"          # resumes of the SAME ticket before giving up on it
IDLE_TIMEOUT="${IDLE_TIMEOUT:-300}"        # kill only after this long with NO output at all
HARD_TIMEOUT="${HARD_TIMEOUT:-3600}"       # absolute backstop, however chatty the run is
WATCH_POLL="${WATCH_POLL:-15}"             # watchdog check interval during a run
POLL_S="${POLL_S:-30}"                     # queue poll interval in loop mode
ONESHOT="${ONESHOT:-0}"                    # 1 = one pass then exit (cron mode), 0 = loop forever
VO_URL="${VO_URL-http://127.0.0.1:8090/api}"   # vo-merge API: callbacks + cross-watch; empty=off
VO_DOWN_ALARM_MIN="${VO_DOWN_ALARM_MIN:-15}"
NOTIFY_URL="${NOTIFY_URL-}"                # ntfy/Discord/Slack — same value as vo-merge's notify_url
UNRAID_NOTIFY="${UNRAID_NOTIFY:-/usr/local/emhttp/webGui/scripts/notify}"
[ -f /boot/config/vo-dispatcher.conf ] && . /boot/config/vo-dispatcher.conf

LOG="$DISPATCH/dispatch.log"
STATE="$DISPATCH/state"
DONE="$DISPATCH/done"
mkdir -p "$DISPATCH" "$STATE" "$DONE" "$WORKDIR"
[ -n "$HOME_DIR" ] && export HOME="$HOME_DIR"

log() { echo "[$(date '+%F %T')] $*" | tee -a "$LOG"; }

exec 9>"$DISPATCH/.lock"
flock -n 9 || exit 0                       # one dispatcher at a time

unraid_notify() {  # $1 subject, $2 body, $3 icon(normal|warning|alert)
    [ -x "$UNRAID_NOTIFY" ] && "$UNRAID_NOTIFY" -e "AI On-Call" -s "$1" -d "$2" -i "${3:-normal}"
}

push_notify() {  # $1 title, $2 body — same payload shapes as backend/app/notify.py
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
    esac || log "push notify failed: $1"
}

alarm() { unraid_notify "$1" "$2" "alert"; push_notify "$1" "$2"; }

# These two used to log one line and exit silently. $HOME can live under /tmp (the AI CLI
# Agents plugin), so it is GONE after every reboot until the plugin recreates it — a total,
# permanent, invisible stop unless it alarms.
if ! command -v "$CLAUDE" >/dev/null 2>&1 && [ ! -x "$CLAUDE" ]; then
    log "claude CLI missing at $CLAUDE"
    alarm "Dispatcher cannot run" "claude CLI missing at $CLAUDE — tickets are piling up."
    exit 1
fi
if [ -n "$HOME_DIR" ] && [ ! -d "$HOME/.claude" ]; then
    log "claude HOME missing ($HOME/.claude)"
    alarm "Dispatcher cannot run" \
          "$HOME/.claude missing — the AI CLI Agents plugin has not started. It lives under /tmp and is lost on every reboot."
    exit 1
fi

check_vo() {
    [ -n "$VO_URL" ] || return 0
    local now since
    now=$(date +%s)
    if curl -fsS --max-time 10 "${VO_URL%/}/health" >/dev/null 2>&1; then
        [ -f "$DISPATCH/.vo-alarmed" ] && push_notify "back up" "vo-merge answers again" \
            && unraid_notify "vo-merge is back up" "health answers again" "normal"
        rm -f "$DISPATCH/.vo-down-since" "$DISPATCH/.vo-alarmed"
        return 0
    fi
    if [ ! -f "$DISPATCH/.vo-down-since" ]; then
        echo "$now" > "$DISPATCH/.vo-down-since"
        log "vo-merge unreachable"
    elif [ ! -f "$DISPATCH/.vo-alarmed" ]; then
        since=$(cat "$DISPATCH/.vo-down-since" 2>/dev/null || echo "$now")
        if [ $(( now - since )) -gt $(( VO_DOWN_ALARM_MIN * 60 )) ]; then
            touch "$DISPATCH/.vo-alarmed"
            alarm "vo-merge itself is DOWN" \
                  "unreachable for $(( (now - since) / 60 ))min. The pipeline and all of its own alarms are offline — check the vo-merge container."
        fi
    fi
}

# Tell vo-merge the outcome. Notifications do NOT reach it: the only thing that clears a record
# out of the operator's review queue is a POST to its own API. The agent is instructed to call
# back itself; doing it here too means a run that forgot still reports. Called ONLY on a
# terminal outcome — a ticket awaiting resume must stay silent so vo-merge keeps the record
# with-the-AI rather than flagging it while work is still in progress.
vo_callback() {  # $1 ticket-name, $2 status-word, $3 diagnosis, $4 action
    [ -n "$VO_URL" ] || return 0
    local vo ep body
    case "$2" in
        fixed*|resolved*|done*|ok*)        vo=resolved    ;;
        needs-operator*|unknown*|gave-up*) vo=needs_human ;;
        *)                                 vo=failed      ;;
    esac
    case "$1" in
        review-m*) ep="movie/${1#review-m}"   ;;
        review-e*) ep="episode/${1#review-e}" ;;
        *) return 0 ;;   # non-record tickets (cap-wedged, config-broken…) have no callback
    esac
    body=$(printf '{"status":"%s","verdict":"%s","action_taken":"%s"}' \
           "$vo" "$(printf '%s' "${3:-no diagnosis line}" | tr '"\n' "' ")" \
                 "$(printf '%s' "${4:-}" | tr '"\n' "' ")")
    curl -sS -m 15 -o /dev/null -w "callback $ep -> HTTP %{http_code}\n" \
         -X POST -H 'Content-Type: application/json' -d "$body" \
         "${VO_URL%/}/$ep/ai_result" >>"$LOG" 2>&1
}

builtin_framing() {
    cat <<'FRAMING'
You are the on-call maintenance agent for a media-library pipeline. The JSON ticket below
describes ONE issue and the REST actions available to fix it. Diagnose first (start with the
read-this-first /context call if present), act via the API using Bash/curl, and finish your
FINAL message with exactly these three lines so the dispatcher can file the outcome:
STATUS: fixed | failed | needs-operator
DIAGNOSIS: <one line — what was wrong>
ACTION: <one line — what you did>
Also POST the ai_result callback described in the ticket's report_back yourself when present.
Do not modify library files directly; act only through the API.
FRAMING
}

run_ticket() {  # $1 = queue dir, $2 = ticket filename. Returns via side effects.
    local dir="$1" name="$2" app key stamp out stream attempt session resume
    local claimed="$dir/claimed/$name" t="$dir/$name"
    app=$(basename "$(dirname "$dir")")
    key="${app}-${name%.json}"
    stamp=$(date +%Y%m%d-%H%M%S)
    out="$DONE/${key}-${stamp}.md"
    stream="$DONE/${key}-${stamp}.jsonl"
    mkdir -p "$dir/claimed" "$dir/dead"
    mv "$t" "$claimed" 2>/dev/null || return 0     # raced / withdrawn by the app

    # Count the attempt BEFORE running: a reboot or an array stop must not give a wedged ticket
    # infinite free retries.
    attempt=$(( $(cat "$STATE/$key.attempt" 2>/dev/null || echo 0) + 1 ))
    echo "$attempt" > "$STATE/$key.attempt"
    session=$(cat "$STATE/$key.session" 2>/dev/null)
    if [ -z "$session" ]; then
        session=$(cat /proc/sys/kernel/random/uuid)
        echo "$session" > "$STATE/$key.session"
        resume=0
    else
        resume=1
    fi
    log "START $key attempt $attempt/$MAX_ATTEMPTS (resume=$resume session=$session)"

    if [ "$resume" = "1" ]; then
        # Resuming keeps the whole prior conversation, so re-send only the continuation
        # instruction. The warning matters: the previous run may have half-applied an action
        # (grabbed a release, queued a merge) and repeating it blindly would double it.
        printf '%s\n' \
            "Your previous run on this ticket was interrupted before it reported an outcome." \
            "Continue from where you stopped. FIRST re-read the current state (GET the record's" \
            "/context) — an action may have half-applied, so verify before repeating anything." \
            "Finish with the STATUS: / DIAGNOSIS: / ACTION: lines." > "$DISPATCH/.ticket-prompt"
        set -- --resume "$session"
    else
        {
            if [ -f "$PROMPT_FILE" ]; then cat "$PROMPT_FILE"; else builtin_framing; fi
            echo '```json'; cat "$claimed"; echo '```'
        } > "$DISPATCH/.ticket-prompt"
        set -- --session-id "$session"
    fi

    cd "$WORKDIR" || return 1
    # stream-json writes an event per tool call / text chunk AS IT HAPPENS, so the file's mtime
    # is a live heartbeat for the watchdog. Watch a run with:
    #   tail -f <the .jsonl> | grep --line-buffered -oP '"name":"\K[A-Za-z]+(?=","input")'
    "$CLAUDE" -p "$(cat "$DISPATCH/.ticket-prompt")" "$@" \
        --output-format stream-json --verbose \
        --allowedTools "$ALLOWED_TOOLS" \
        > "$stream" 2>&1 < /dev/null &
    local cpid=$! started rc="" now last wrc
    started=$(date +%s)

    # A flat timeout kills a job that is working fine but slow (a sync_probe over a 4K pair
    # legitimately runs tens of minutes) and waits far too long on one that is truly wedged.
    # Time the SILENCE instead, with a hard ceiling as a backstop.
    while kill -0 "$cpid" 2>/dev/null; do
        sleep "$WATCH_POLL"
        for d in "${TICKET_DIRS[@]}"; do touch "$d/.heartbeat"; done   # a long run isn't a dead dispatcher
        now=$(date +%s)
        last=$(stat -c %Y "$stream" 2>/dev/null || echo "$started")
        if [ $(( now - last )) -ge "$IDLE_TIMEOUT" ]; then
            log "KILL  $key — silent for $(( now - last ))s"
            kill -TERM "$cpid" 2>/dev/null; sleep 5; kill -KILL "$cpid" 2>/dev/null; rc=124; break
        fi
        if [ $(( now - started )) -ge "$HARD_TIMEOUT" ]; then
            log "KILL  $key — hard timeout at $(( now - started ))s"
            kill -TERM "$cpid" 2>/dev/null; sleep 5; kill -KILL "$cpid" 2>/dev/null; rc=125; break
        fi
    done
    wait "$cpid" 2>/dev/null; wrc=$?
    [ -z "$rc" ] && rc=$wrc
    runs=$((runs + 1)); echo "$runs" > "$BUDGET"

    # Human-readable digest beside the raw stream.
    if command -v jq >/dev/null 2>&1; then
        jq -r 'select(.type=="assistant") | .message.content[]?
               | if .type=="text" then .text
                 elif .type=="tool_use" then "[tool] \(.name)"
                 else empty end' "$stream" > "$out" 2>/dev/null
    else
        grep -oP '"type":"tool_use","id":"[^"]*","name":"\K[^"]+' "$stream" \
            | sed 's/^/[tool] /' > "$out"
    fi
    echo "--- attempt $attempt, rc=$rc, $(( $(date +%s) - started ))s, raw: $stream ---" >> "$out"

    # Text arrives as deltas, but each complete assistant message carries the whole string, so
    # an anchored match on the JSON stream works without jq.
    local status diag act
    status=$(grep -oP '(?<=STATUS: )[^"\\]+'    "$stream" | tail -1)
    diag=$(  grep -oP '(?<=DIAGNOSIS: )[^"\\]+' "$stream" | tail -1)
    act=$(   grep -oP '(?<=ACTION: )[^"\\]+'    "$stream" | tail -1)

    # A resume that produced almost nothing means the session is gone (plugin reinstall,
    # cleared /tmp). Drop it so the next attempt starts the ticket fresh.
    if [ "$resume" = "1" ] && [ "$(wc -l < "$stream")" -lt 5 ]; then
        rm -f "$STATE/$key.session"
        log "$key — resume produced nothing, session discarded"
    fi

    if [ "$rc" = "0" ] && [ -n "$status" ]; then
        # Finished properly: the ONLY path that archives the ticket (the ack).
        local icon="normal"
        case "$status" in needs-operator*|unknown*) icon="warning" ;; esac
        unraid_notify "$app: ${name%.json} -> $status" "${diag:-see $out}" "$icon"
        [ "$app" = "vo-merge" ] && vo_callback "${name%.json}" "$status" "$diag" "$act"
        mv "$claimed" "$DONE/${key}-${stamp}.json"
        rm -f "$STATE/$key.attempt" "$STATE/$key.session"
        log "DONE  $key -> $status"
    elif [ "$attempt" -lt "$MAX_ATTEMPTS" ]; then
        # Not finished. Back to the QUEUE (not claimed/): the app keeps the record with-the-AI
        # rather than staleness-flagging it, next pass sorts it ahead of new tickets, and the
        # same session resumes. No callback while work is still in progress.
        local why
        case "$rc" in
            124) why="killed after ${IDLE_TIMEOUT}s with no output" ;;
            125) why="killed at the ${HARD_TIMEOUT}s hard limit"    ;;
            0)   why="ended without a STATUS line"                  ;;
            *)   why="exited rc=$rc"                                ;;
        esac
        mv "$claimed" "$t"
        log "RETRY $key — $why (attempt $attempt/$MAX_ATTEMPTS)"
        unraid_notify "$app: ${name%.json} -> will resume" \
                      "$why — attempt $attempt of $MAX_ATTEMPTS, resuming next run." "warning"
    else
        # Out of attempts. Give up EXPLICITLY — callback so the record leaves 'with the AI',
        # dead/ so the give-up is a visible verdict, never silent rot.
        log "GIVEUP $key after $attempt attempts (rc=$rc)"
        alarm "$app: ${name%.json} -> gave up" \
              "Unfinished after $MAX_ATTEMPTS attempts (rc=$rc). Needs a human."
        [ "$app" = "vo-merge" ] && vo_callback "${name%.json}" "gave-up" \
            "unfinished after $MAX_ATTEMPTS dispatcher attempts (rc=$rc)" "${act:-none}"
        mv "$claimed" "$dir/dead/$name"
        rm -f "$STATE/$key.attempt" "$STATE/$key.session"
    fi
}

pass() {
    BUDGET="$DISPATCH/runs-$(date +%F)"
    runs=$(cat "$BUDGET" 2>/dev/null || echo 0)
    find "$DISPATCH" -maxdepth 1 -name 'runs-*' ! -name "runs-$(date +%F)*" -delete

    local d f name handled=0
    for d in "${TICKET_DIRS[@]}"; do
        mkdir -p "$d/claimed" "$d/dead"
        touch "$d/.heartbeat"
        # anything still claimed at pass start is a crashed dispatcher run — requeue it (its
        # attempt was already counted before it ran, so this is not a free retry)
        for f in "$d"/claimed/*.json; do
            [ -e "$f" ] || continue
            mv "$f" "$d/$(basename "$f")"
            log "requeued $(basename "$f") left claimed by a crashed run"
        done
    done
    check_vo

    # Unfinished work first: a ticket that already has attempts must be finished before any new
    # one is started, or a busy queue leaves half-done jobs behind indefinitely. Oldest first
    # within each class (ticket names carry no whitespace).
    local tickets=() entry app key
    for d in "${TICKET_DIRS[@]}"; do
        app=$(basename "$(dirname "$d")")
        for name in $(ls -tr "$d" 2>/dev/null | grep '\.json$'); do
            key="${app}-${name%.json}"
            if [ -f "$STATE/$key.attempt" ]; then tickets=("$d|$name" "${tickets[@]}")
            else                                  tickets+=("$d|$name"); fi
        done
    done

    for entry in ${tickets[@]+"${tickets[@]}"}; do
        [ "$handled" -ge "$PER_INVOCATION" ] && break
        if [ "$runs" -ge "$MAX_RUNS" ]; then
            if [ ! -f "$BUDGET.warned" ]; then      # once a day, not once a firing
                alarm "Daily AI budget reached ($MAX_RUNS)" \
                      "${#tickets[@]} ticket(s) still queued — will resume tomorrow."
                touch "$BUDGET.warned"
            fi
            log "budget exhausted, ${#tickets[@]} waiting"
            break
        fi
        run_ticket "${entry%%|*}" "${entry#*|}"
        handled=$((handled + 1))
        for d in "${TICKET_DIRS[@]}"; do touch "$d/.heartbeat"; done
    done
}

log "dispatcher up: dirs=${TICKET_DIRS[*]} cli=$CLAUDE mode=$([ "$ONESHOT" = 1 ] && echo oneshot || echo loop)"
if [ "$ONESHOT" = "1" ]; then
    pass
    exit 0
fi
while true; do
    pass
    sleep "$POLL_S"
done
