"""The out-of-band alarm channel — how a human hears about the automation itself breaking.

Everything else in this app is pull-based: the Review tab, the On-call AI panel, the log. That
is fine for work the pipeline is doing, and useless for the class of failure where the pipeline
(or its fixer) has stopped doing anything — a dead dispatcher, an unparseable config that
silently flipped `enabled` off, a full disk, an indexer that has been down for days. Those wait
for someone to happen to open the UI, which on an unattended install is never.

One deliberately small mechanism: POST a title+body to `notify_url`. The URL's shape picks the
payload — a Discord/Slack webhook gets their JSON envelope, anything else gets an ntfy-style
plain-text POST with a `Title` header, which also covers Gotify-via-ntfy bridges and plain
webhook receivers. Empty URL (the default) turns the whole thing off.

**Rate-limited per `kind`, not per message.** A standing condition (disk still full, dispatcher
still dead) re-fires every sweep, and a channel that repeats itself every 3 minutes gets muted
by its human within a day — which is worse than no channel. One alarm per kind per
`notify_repeat_h` (default 24h); `clear(kind)` is called when the condition is observed healthy
again, so a RE-occurrence alerts immediately instead of waiting out the window. State is
persisted so a container restart doesn't replay every standing alarm.

Never raises: an unreachable notifier must not take down the sweep that called it.
"""
import json
import os
import time

import requests

from . import core

STATE_FILE = os.path.join(core.CONFIG_DIR, "notify_state.json")
_LOCK_KINDS = {}          # in-process mirror of the state file, so a stat/read per call is rare


def _load_state():
    try:
        with open(STATE_FILE) as f:
            return {str(k): float(v) for k, v in json.load(f).items()}
    except Exception:
        return {}


def _save_state(st):
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(st, f)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        core.log(f"notify: state save failed: {e}")


def _payload(url, title, body):
    """(json, data, headers) for the POST, by webhook family. Discord and Slack refuse anything
    that isn't their envelope; everything else takes text."""
    u = url.lower()
    if "discord.com/api/webhooks" in u or "discordapp.com/api/webhooks" in u:
        return {"content": f"**{title}**\n{body}"[:1900]}, None, {}
    if "hooks.slack.com" in u:
        return {"text": f"*{title}*\n{body}"[:2900]}, None, {}
    # ntfy-style: body is the message, title travels in a header
    return None, body.encode("utf-8"), {"Title": title, "Priority": "high",
                                        "X-Title": title}


def send(kind, title, body, cfg=None, force=False):
    """Fire one alarm. Returns True only when a POST was actually made.

    `kind` is the dedup key — "dispatcher", "disk", "config", "dep-prowlarr", "needs_human" —
    NOT the message, so an evolving message for the same standing condition still counts as the
    same alarm. `force=True` skips the limiter (operator-initiated tests)."""
    cfg = cfg if cfg is not None else core.load_config()
    url = str(cfg.get("notify_url") or "").strip()
    if not url:
        return False
    repeat = max(0.0, float(cfg.get("notify_repeat_h", 24))) * 3600
    st = _load_state()
    last = st.get(kind)
    if not force and last and repeat and time.time() - last < repeat:
        return False
    js, data, headers = _payload(url, f"vo-merge: {title}", body)
    try:
        r = requests.post(url, json=js, data=data, headers=headers, timeout=10)
        r.raise_for_status()
    except Exception as e:
        core.log(f"notify {kind}: POST failed ({e})")
        return False
    st[kind] = time.time()
    _save_state(st)
    core.log(f"notify {kind}: {title}")
    return True


def clear(kind):
    """The condition behind `kind` was observed healthy — drop its limiter so the NEXT
    occurrence alerts immediately. A dispatcher that dies, recovers and dies again the same day
    is two events, not one."""
    st = _load_state()
    if kind in st:
        st.pop(kind, None)
        _save_state(st)
