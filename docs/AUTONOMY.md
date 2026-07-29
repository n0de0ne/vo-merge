# Full autonomy — gap analysis

What separates vo-merge today from "100% autonomous, failures solved by itself, no human
intervention". Written against the July 2026 codebase; file references are to `backend/app/`.

The system is already unusually self-healing for this class of tool: stalled and dead torrents
are dropped, blocklisted and replaced; sync failures spend a budget of *different releases*
before settling; settled states re-open when a scan finds a remaining gap; an unreachable
indexer parks nothing; wedged muxes and decodes are killed on a timeout; every failure is paged
to an on-call AI within 3 minutes with a full action surface. The remaining distance to "no
human, ever" is not more of those per-case fixes — it is **five structural properties** the
current design lacks, plus a tail of specific dead-ends.

---

## 1. The error-solving brain is outside the system, unsupervised

Everything terminal funnels into the AI round-trip (`agent.py`, `pipeline.ai_health_check`),
and that loop's executor is **an Unraid cron script on the host running the Claude Code CLI**
— outside the container, outside the repo, outside any supervision. `agent.py`'s own module
docstring names the consequence: when the script stops, the failure is silent, tickets pile
up, and the whole backlog flips to `needs_human` — "which reads exactly like an agent that
examined each one and gave up. That is the state this install was found in."

As long as the fixer can die silently, every other autonomy investment bottoms out at
"needs a human to notice the fixer died". This is the single highest-leverage change:

- **Bring the agent into the deployment.** Either:
  - **(a) sidecar container** in the same compose/template: a loop that watches
    `/config/ai-tickets/`, runs the CLI per ticket, deletes the ticket, and POSTs the
    callback. `restart: unless-stopped` + a healthcheck make the *platform* supervise it, and
    vo-merge can watch its heartbeat (below). Smallest step from today's design; the ticket
    protocol and briefs are unchanged.
  - **(b) in-app dispatcher**: a worker thread in vo-merge that drives the Anthropic API
    (tool-use mapped onto the app's own REST actions — `/context`, `/sync_probe`,
    `/set_sync`, `/assign`, `/another`, `/search_releases`, `/unfixable`). One deployable,
    natively metered, retried and logged by the app itself. Clean end-state; requires an API
    key and budget controls in config.
- **Heartbeat + liveness verdict.** The dispatcher touches `/config/ai-tickets/.heartbeat`
  every cycle; `agent.status()` then *knows* dead-vs-slow instead of inferring it from ticket
  age. `ai_health_check` raises an alarm (see §4) when the heartbeat is stale AND tickets
  wait.
- **Queue semantics, not one-file-per-kind.** `core.ticket` refuses to overwrite a same-kind
  ticket, so new failures queue behind an unconsumed `errors-review.json` — and records
  inside a sitting ticket are (correctly) excluded from the staleness flip, so with a dead
  dispatcher they show "AI working" indefinitely. Per-record ticket files with take/ack
  (rename into `claimed/`, delete on callback) give throughput, retry and a dead-letter
  directory for ~100 lines.
- **Budgets.** Max agent invocations per hour/day, max spend per ticket, and the existing
  `ai_tickets` flag as the kill switch. An autonomous fixer without a meter is its own
  failure mode.

## 2. States that still dead-end into a human

Each of these currently has no automated exit. In order of impact:

- **`review` / `sync_fail` — run the wider probe *before* parking.** Both ticket templates
  tell the AI: "on any couldn't-sync, call `/sync_probe` FIRST" (±300 s search vs the merge
  path's ±`sync_max_lag_s`). That first line of the runbook is deterministic — so it should
  be pipeline code, not an agent instruction. When `sync.detect` fails and the retry budget
  is spent, run the wide probe once; windows agree → `set_sync` + `enqueue_merge`
  automatically; windows disagree → *then* park with the probe result attached to the
  ticket. This converts the most common `review` class (large constant offset: sponsor card,
  "previously on") into a closed loop and reserves the AI for genuinely different cuts.
- **`no_release` exhaustion is a silent forever-loop.** After `max_sync_retries` stalls, or
  when nothing scores, a record sits out `no_release_retry_h` and re-searches with the *same
  composed query* and a *never-aging blocklist* (`tried` only ever grows; nothing clears it).
  Titles the query never matches — alternate romanisation, original title, wrong year — can
  never be found "however often it re-searches" (the `/search_releases` docstring says
  exactly this), yet **`no_release` is never escalated to the AI**: `ai_health_check` pages
  only `error`/`review`/`sync_fail`. Three fixes:
  - a built-in **query ladder** in `candidates()`/`tv._search`: original title → title
    without year → romaji/alternate from Radarr/Sonarr's `alternateTitles` — tried in order
    before settling `no_release`;
  - after N fruitless cooldown cycles (track a `search_rounds` counter), **escalate once**
    to the AI with a "compose a better query" brief;
  - **blocklist aging**: a `tried` entry older than ~30 days is eligible again (a release
    that stalled once at 0 seeds may be healthy now). Without this, a title whose only
    release had one bad day is permanently unfixable.
- **`needs_human` after AI failure.** Fine as a last rung — but it must *notify* (§4), not
  wait to be noticed in a pull-based UI panel.
- **`ignored`/`unfixable` deserve a slow revisit.** A deliberate give-up survives rescans by
  design, but "no release exists" decays as truth. A scheduled re-examination (e.g. every 90
  days, one cheap search, re-ignore on the same verdict) makes the terminal state honest
  without flooding indexers.
- **`grab_mode: "approval"` is a human by definition.** Autonomy requires `auto` (the
  default). Document it as such.

## 3. Crash-recovery holes: `searching` and `grabbed` are unswept

`stage_finish` re-queues stale `merging` (>15 min, not in `_merging_now()`) and reconciles
`downloading` against qB — but **nothing ever recovers `searching` or `grabbed`**:

- `search_movie` claims `pending → searching` atomically; a crash mid-search strands the
  record in `searching` forever (`stage_search` reads only `pending`).
- `search_movie` writes `grabbed`, then calls `grab()`; a crash between the write and the
  qB add — or a `grabbed` row left over after switching `grab_mode` back to auto — strands
  it in `grabbed`. The orphan sweep can't even see it (no `dl_hash` yet).

Both need a staleness rule in the 3-minute sweep, mirroring the `merging` one:
`searching` older than ~15 min → back to `pending`; `grabbed` older than ~30 min in auto
mode → back to `pending` (blocklist untouched — the grab may simply have never happened).

Also in this class:
- **Episode `review` records are excluded from the staleness flip.** `ai_health_check` pages
  episodes in `review`, but its stale query covers only `('error','sync_fail')` for episodes
  (movies include `'review'`). An episode in `review` whose ticket was consumed and never
  answered stays "AI working" forever. One-word fix.
- **Dead merge workers are only re-spawned on start/`reschedule`.** `merge_worker` survives
  per-item exceptions, but if the thread itself dies (OOM, interpreter error), nothing
  notices until a settings change. Call `scheduler.ensure_merge_workers()` from the
  3-minute stall job.

## 4. The automation has no alarm channel — and some of its own failures are silent

"100% no human intervention" really means: *humans never do routine repair; they are told,
out-of-band, when the machine has proven something impossible or the machine itself is
broken.* Today there is no push channel at all — the Review tab and the On-call AI panel are
pull-based, so every alarm is "eventually, if someone opens the UI".

- **Add one notifier** (ntfy / Discord webhook / Apprise URL in config, ~50 lines, used
  sparingly) for exactly these events:
  - dispatcher heartbeat stale while tickets wait (§1);
  - `config.json` unparseable — today this logs once and **silently runs on DEFAULTS with
    `enabled: False`**, i.e. the whole pipeline stops with no page of any kind
    (`core.load_config`). It should also file a ticket: the AI can often fix a truncated
    JSON file itself;
  - disk headroom below threshold (see below);
  - a dependency down beyond a grace window;
  - a record newly landing in `needs_human` (the digest, not per-record spam).
- **Dependency health with memory.** Every sweep currently logs "qB error …" and returns —
  correct per-cycle behaviour, but nothing tracks *duration*, so "Prowlarr has been
  unreachable for 3 days" is indistinguishable from a blip. Keep a `down_since` per client;
  alarm past a grace period; expose it on `/status`. The existing `/test/{which}` endpoints
  already know how to ping each service.
- **Disk headroom gate.** `run_mux`'s classic rc≥2 cause is disk-full — it cleans the partial
  output but only after burning the attempt, once per retry. Before starting a mux, require
  free space ≥ (base + donor size + margin) on the target filesystem; below a floor, pause
  merging (the `paused` brake already exists), sweep `_merged/` leftovers and orphan donors,
  and alarm. Also: `review`/`sync_fail`/`error` keep donors on disk indefinitely
  (`KEEP_DONOR_STATES`) — with a dead fixer this grows without bound; give kept donors a TTL
  or a size cap.
- **DB self-check.** Nightly `VACUUM INTO` backups exist (`core.backup_db`); nothing ever
  reads one. Run `PRAGMA integrity_check` at startup; on failure, restore the newest backup
  automatically and alarm. Give `config.json` the same nightly copy (its atomic-write +
  refuse-to-clobber protects against *crashes*, not against a bad-but-parseable save).
- **Transient-vs-permanent error classification.** Several transient conditions mint `error`
  records that then consume an AI invocation for what a retry would fix: `grab()` on a
  momentary qB failure ("grab: …"), tv `_grab` → "grab: torrent never appeared", "merge:
  probe failed" on an NFS hiccup, "path never became visible". Route transient classes
  through pending-with-backoff (attempts++, the machinery exists) and reserve `error` — and
  the AI — for conditions a retry cannot change. This one change removes most of the AI's
  routine caseload.

## 5. Trust needs verification: the silent desync is permanent today

At full autonomy nobody watches the output, so the pipeline must verify its own work — and
right now the one failure that slips the gate is **unrecoverable and invisible**:

- A graft whose sync detection was confident-but-wrong writes a desynced language track into
  the library file. `finish_movie` re-audits *languages* only, so the gap decision sees
  `eng` present → record `merged`, scans see the profile met → closed. The donor is deleted
  (`_free_donor`), the original file was replaced in place. Nothing in the system can ever
  notice; a re-merge can't fix it (a new donor "contributes nothing" — the language is
  present); only a human watching the film finds it, which is the definition of
  non-autonomous. The repair primitive (`resync_movie`, audio-vs-audio consensus between the
  base track and the grafted track *inside one file*) already exists — it just never runs
  unprompted.
- **Post-merge QC pass:** after the mux, before `finish_movie` swaps the file, run
  `audio_consensus(out, base_track, grafted_track, …)`; |offset| beyond tolerance → treat
  exactly like a failed sync (blocklist, `reject_and_retry`), keeping the original file
  untouched. Cost: seconds per merge, against the only permanent silent failure the pipeline
  has.
- **Recycle bin for replacements.** The graft path preserves the base's tracks inside the
  output, but `_place_multi` and the TV direct-remux **discard the library file entirely**
  in favour of the release. Move the original into `/config`-side (or library-side) recycle
  with a TTL instead of deleting, so a bad replacement is reversible by machine.
- **`library/repair` can be autonomous — behind a flag.** Its guards are already the strong
  ones (only `no audio track`, cache-bypassed re-probe, *arr-known files only, deletion via
  the *arr so a replacement is searched). An `auto_repair: false` config flag that runs the
  real pass on a schedule when enabled fits the trust model; default stays plan-first.

## 6. Posture: config prerequisites for unattended operation

`enabled: true`, `grab_mode: "auto"`, `ai_tickets: true`, `scope_series` as desired,
`sync_review: true` only *with* a supervised fixer (it parks records for an actor — fine
when the actor is the resident agent, a trap when it is a dead cron). Ship a documented
"autonomous profile" of these so the mode is a decision, not an accident of defaults.

---

## Suggested order

| Phase | Work | Buys |
|---|---|---|
| 1 | Resident supervised agent (sidecar or in-app) + heartbeat + per-record ticket queue + notifier with the five alarms | The fixer can no longer die silently; humans only ever get *told* |
| 2 | Auto `sync_probe` before `review`; transient-error classification; `searching`/`grabbed` staleness sweep; episode-`review` staleness fix; periodic `ensure_merge_workers` | The biggest dead-end classes close themselves; AI caseload drops to the interesting failures |
| 3 | Post-merge QC + replacement recycle bin | Output is verified; the one permanent silent failure is gone |
| 4 | `no_release` query ladder + one-shot AI escalation + blocklist aging; disk gate + donor TTL; DB/config self-check & restore | The long tail: nothing loops forever, nothing fills the disk, state survives corruption |
| 5 | `ignored` slow revisit; `auto_repair` flag | Terminal verdicts stay honest without a human audit |

Phases 1–2 are where "needs a human weekly" becomes "needs a human when the machine says a
title is impossible". Phases 3–5 are what make that claim safe to believe.
