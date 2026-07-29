"""Resilient A/V offset detection: multiple analysis windows, video scene-cut matching
primary, audio music/SFX cross-correlation as cross-check + fallback. Centralizes the
logic used by both the movie and episode merges."""
from . import core
from .offdet_video import detect_offset_video_ms, ratio_scan
from .offdet import detect_offset_ms

NTSC = 24000.0 / 1001.0                 # 23.976
# Standard transfer rate ratios, as donor->base factors. A PAL transfer plays 24/23.976fps
# content at 25fps, so the PAL copy runs ~4.3% SHORT — grafting audio across the two needs a
# time STRETCH, not a constant offset. These are the textbook values; `ratio_candidates` also
# derives the exact ratio from the two files' measured framerates.
RATE_RATIOS = (
    1.0,                                # no rate change (control hypothesis)
    25.0 / NTSC,                        # 1.042708  film(23.976) -> PAL(25)
    NTSC / 25.0,                        # 0.959041  PAL -> film
    25.0 / 24.0,                        # 1.041667  film(24) -> PAL
    24.0 / 25.0,                        # 0.960000  PAL -> film(24)
    24.0 / NTSC,                        # 1.001     NTSC pulldown
    NTSC / 24.0,                        # 0.999
)


def _decode_timeout(cfg):
    """Seconds after which one decode pass is killed. Each pass reads a bounded window, so a
    run past this is wedged, not slow — and an unbounded one holds the merge worker forever."""
    return max(60, int(cfg.get("sync_decode_timeout_min", 30)) * 60)


def fps_close(a, b, tol=0.03):
    """True if framerates are close enough that a constant offset still works.
    23.976 vs 24.0 (0.1%) -> ok; 25 vs 23.976 (4%) -> not (PAL speedup, would drift)."""
    if not a or not b:
        return True
    return abs(a - b) / max(a, b) <= tol


def ratio_candidates(base_fps=None, donor_fps=None, base_dur=None, donor_dur=None):
    """Rate ratios worth testing, most-likely first. `k` maps donor timestamps onto the base
    timeline (base_t = k*donor_t + offset), so k = donor_fps / base_fps.
    The measured framerates give the exact answer when they're trustworthy; the standard
    transfer ratios cover rounded/missing/VFR metadata. The duration ratio is a third
    independent guess — a PAL copy really is ~4% shorter."""
    out = []
    if base_fps and donor_fps:
        out.append(donor_fps / base_fps)
    if base_dur and donor_dur:
        out.append(base_dur / donor_dur)
    out.extend(RATE_RATIOS)
    uniq = []
    for k in out:
        if 0.9 <= k <= 1.11 and not any(abs(k - u) < 1e-4 for u in uniq):
            uniq.append(k)
    return uniq


def ratio_detect(base, donor, dur, cfg, base_fps=None, donor_fps=None,
                 base_dur=None, donor_dur=None, tag="", on_progress=None):
    """Rate-ratio hypothesis test — the PAL-speedup path.

    Window-by-window matching CANNOT see a rate difference: it smears the cut pattern inside
    each window and makes every window disagree (the classic symptom is per-window offsets
    fanning out over tens of seconds). Instead of measuring drift from those broken windows,
    we take the handful of ratios that physically occur in film/TV transfers, warp the donor's
    timeline by each, and keep the one that actually correlates.

    Returns (offset_ms, conf, method, k) or None. A win requires the best ratio to clear
    `sync_ratio_min_conf` AND to beat the no-stretch hypothesis by `sync_ratio_margin` — so a
    pair that merely needs a constant offset is never handed a bogus stretch."""
    ratios = ratio_candidates(base_fps, donor_fps, base_dur, donor_dur)
    if len(ratios) < 2:
        return None
    dur = dur or 0
    span = max(600.0, min(cfg.get("sync_ratio_span", 2400), dur * 0.6)) if dur else 2400.0
    start = max(60.0, (dur - span) / 2.0) if dur else 600.0
    if on_progress:
        on_progress(f"sync: testing {len(ratios)} rate ratios over {int(span/60)}min")
    core.log(f"sync{tag}: rate-ratio scan, {len(ratios)} hypotheses @ {int(start)}s +{int(span)}s")
    try:
        res = ratio_scan(base, donor, ratios, start=int(start), dur=int(span),
                         threads=cfg.get("sync_ffmpeg_threads", 4),
                         hwaccel=cfg.get("sync_hwaccel", "vaapi"),
                         device=cfg.get("sync_hwaccel_device", "/dev/dri/renderD128"),
                         timeout=_decode_timeout(cfg))
    except Exception as e:
        core.log(f"sync{tag}: rate-ratio scan failed: {e}")
        return None
    if not res:
        return None
    k, off, conf = res[0]
    flat = next((c for kk, _, c in res if abs(kk - 1.0) < 1e-9), 0.0)
    core.log(f"sync{tag}: best ratio {k:.6f} -> {off:+.0f}ms (conf {conf:.2f}, no-stretch {flat:.2f})")
    if conf < cfg.get("sync_ratio_min_conf", 0.35):
        return None
    if abs(k - 1.0) < 1e-9:                       # no stretch needed after all
        return int(round(off)), conf, "video-ratio 1:1", None
    if conf < flat * cfg.get("sync_ratio_margin", 1.3):
        return None                               # not convincingly better than no stretch
    core.log(f"sync{tag}: RATE RATIO {k:.6f} ({(k-1)*100:+.2f}%) accepted, base {off:+.0f}ms")
    return int(round(off)), conf, f"video-ratio {k:.6f}", k


def _windows(dur, n=3, length=480):
    """n analysis windows spread across the middle of the content, clamped to runtime."""
    dur = dur or 0
    if dur < 240:                       # very short -> one best-effort window
        return [(max(0, dur * 0.1), max(60, dur * 0.6))] if dur else [(120, length)]
    out, step = [], (dur * 0.8) / (n + 1)
    for i in range(1, n + 1):
        s = max(30, min(dur * 0.1 + step * i - length / 2, dur - length - 5))
        out.append((s, min(length, dur - s - 2)))
    # de-dupe near-identical windows
    uniq = []
    for w in out:
        if not uniq or abs(w[0] - uniq[-1][0]) > 30:
            uniq.append(w)
    return uniq


def audio_consensus(path, ref_ai, shift_ai, dur, cfg, tag=""):
    """Offset between two audio tracks IN THE SAME FILE (for re-syncing a finished merge,
    where there's only one video). Multi-window consensus. Returns (offset_ms|None, conf)."""
    import statistics
    wins = _windows(dur, n=cfg.get("sync_windows", 4), length=cfg.get("sync_window_dur", 480))
    res = []
    for (s, d) in wins:
        try:
            m, c = detect_offset_ms(path, ref_ai, path, shift_ai, start=int(s), dur=int(d),
                                    timeout=_decode_timeout(cfg))
            if m is not None:
                res.append((m, c))
        except Exception:
            pass
    for m0, _ in sorted(res, key=lambda x: -x[1]):
        agree = [(m, c) for m, c in res if abs(m - m0) <= 150]
        if len(agree) >= 2:
            return statistics.median(m for m, _ in agree), max(c for _, c in agree)
    if res:
        m, c = max(res, key=lambda x: x[1])
        if c >= max(cfg.get("auto_sync_min_conf", 0.2), 0.35):
            return m, c
    return None, 0.0


def _linfit(xs, ys):
    n = len(xs); sx = sum(xs); sy = sum(ys); sxx = sum(x * x for x in xs); sxy = sum(x * y for x, y in zip(xs, ys))
    den = n * sxx - sx * sx
    if den == 0:
        return 0.0, sy / n, 0.0
    b = (n * sxy - sx * sy) / den; a = (sy - b * sx) / n
    mean = sy / n; ss_tot = sum((y - mean) ** 2 for y in ys)
    ss_res = sum((y - (a + b * x)) ** 2 for x, y in zip(xs, ys))
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 1.0
    return b, a, r2


def verify_hint(base, donor, dur, hint, cfg, tag=""):
    """Confirm a pack-mate's already-known offset with ONE window (≈5x faster than full
    detect). Returns (offset, conf, method, drift) if it agrees, else None to force full
    detect."""
    ho, hd = hint
    if hd:
        # A drift/ratio hint IS quick-verifiable: re-test just that one ratio on a single
        # window. Without this every episode of a PAL season pack redoes the whole scan.
        s = max(60.0, ((dur or 1200) - min(1200, (dur or 1200) * 0.5)) / 2.0)
        d = min(1200, (dur or 1200) * 0.5)
        try:
            res = ratio_scan(base, donor, [hd], start=int(s), dur=int(d),
                             threads=cfg.get("sync_ffmpeg_threads", 4),
                             hwaccel=cfg.get("sync_hwaccel", "vaapi"),
                             device=cfg.get("sync_hwaccel_device", "/dev/dri/renderD128"),
                             timeout=_decode_timeout(cfg))
        except Exception:
            return None
        if res and res[0][2] >= cfg.get("sync_ratio_min_conf", 0.35):
            k, off, c = res[0]
            core.log(f"sync{tag}: pack ratio {k:.6f} confirmed, {off:+.0f}ms (conf {c:.2f})")
            return int(round(off)), c, "pack-verify-ratio", k
        core.log(f"sync{tag}: pack ratio {hd:.6f} NOT confirmed -> full detect")
        return None
    s = (dur or 1200) * 0.45                       # one central window
    try:
        m, c = detect_offset_video_ms(
            base, donor, start=int(s), dur=int(cfg.get("sync_window_dur", 480)),
            threads=cfg.get("sync_ffmpeg_threads", 4),
            hwaccel=cfg.get("sync_hwaccel", "vaapi"),
            device=cfg.get("sync_hwaccel_device", "/dev/dri/renderD128"),
            timeout=_decode_timeout(cfg))
    except Exception:
        return None
    if m is not None and c >= 0.5 and abs(m - ho) <= 150:
        core.log(f"sync{tag}: pack offset {ho:+.0f}ms confirmed (1 window, conf {c:.2f})")
        return int(round(ho)), c, "pack-verify", None
    core.log(f"sync{tag}: pack offset {ho:+.0f}ms NOT confirmed (got {m}/{c:.2f}) -> full detect")
    return None


def detect(base, donor, base_ai, donor_ai, dur, cfg, tag="", on_progress=None, hint=None,
           base_fps=None, donor_fps=None, base_dur=None, donor_dur=None):
    """Returns (offset_ms|None, confidence, method, drift_ratio|None).

    Measures the video offset at several points across the movie:
      - a MAJORITY agree on one value      -> constant offset (drift=None)
      - they fall on a straight LINE        -> linear drift (framerate mismatch); returns
                                               the base offset + an o1/o2 stretch ratio
      - a known RATE RATIO matches          -> PAL-speedup & friends; returns offset + ratio
      - they're INCONSISTENT (different cut)-> reject (offset=None) so the caller tries
                                               another release / a MULTI instead of guessing
    """
    import statistics
    if hint is not None:                           # try the fast pack-mate path first
        r = verify_hint(base, donor, dur, hint, cfg, tag)
        if r:
            return r
    amin = cfg.get("auto_sync_min_conf", 0.2)
    ratio_ok = cfg.get("sync_ratio_test", True)
    tried_ratios = False
    # Known rate mismatch (e.g. FR 25fps PAL vs EN 23.976): go straight to the ratio test.
    # Running the window scan first would just burn five ffmpeg passes to produce the
    # fan-shaped offsets that can't be fitted anyway.
    if ratio_ok and base_fps and donor_fps and not fps_close(base_fps, donor_fps):
        tried_ratios = True
        r = ratio_detect(base, donor, dur, cfg, base_fps, donor_fps, base_dur, donor_dur,
                         tag, on_progress)
        if r:
            return r
    wins = _windows(dur, n=max(5, cfg.get("sync_windows", 5)), length=cfg.get("sync_window_dur", 480))
    n = len(wins)
    vres = []                                          # (center_time_s, offset_ms, conf)
    for i, (s, d) in enumerate(wins):
        if on_progress:
            on_progress(f"sync: scanning window {i+1}/{n} (@{int(s//60)}min)")
        core.log(f"sync{tag}: window {i+1}/{n} @ {int(s)}s ({int(d)}s)")
        try:
            m, c = detect_offset_video_ms(
                base, donor, start=int(s), dur=int(d),
                max_lag_s=cfg.get("sync_max_lag_s", 120),
                threads=cfg.get("sync_ffmpeg_threads", 4),
                hwaccel=cfg.get("sync_hwaccel", "vaapi"),
                device=cfg.get("sync_hwaccel_device", "/dev/dri/renderD128"),
                timeout=_decode_timeout(cfg))
        except Exception as e:
            core.log(f"sync{tag}: video window {int(s)}s error: {e}"); m, c = None, 0.0
        if m is not None and c >= 0.3:
            vres.append((s + d / 2.0, m, c))
            core.log(f"sync{tag}: window {i+1}/{n} -> {int(m):+d}ms (conf {c:.2f})")
    if on_progress:
        on_progress("sync: computing offset")
    if len(vres) >= 2:
        offs = [o for _, o, _ in vres]
        # largest cluster agreeing within 150ms
        best = []
        for o0 in offs:
            cl = [(t, o, c) for t, o, c in vres if abs(o - o0) <= 150]
            if len(cl) > len(best):
                best = cl
        if len(best) >= max(2, (len(vres) + 1) // 2) and len(best) >= len(vres) * 0.6:
            off = statistics.median(o for _, o, _ in best)
            core.log(f"sync{tag}: constant {off:+.0f}ms ({len(best)}/{len(vres)} windows)")
            return int(round(off)), max(c for _, _, c in best), f"video x{len(best)}", None
        if len(vres) >= 3:                             # linear drift?
            ts = [t for t, _, _ in vres]
            b, a, r2 = _linfit(ts, offs)
            if r2 >= 0.93 and abs(b * dur) >= 300:     # meaningful, well-fit drift
                k = 1.0 + b / 1000.0                    # audio runs b ms fast per s -> stretch
                core.log(f"sync{tag}: LINEAR DRIFT {b*dur:+.0f}ms over movie, base {a:+.0f}ms, ratio {k:.6f} (R²={r2:.2f})")
                return int(round(a)), r2, "video-drift", k
        # Windows disagreed. Before calling it a different cut, check whether they're fanning
        # out because of a RATE difference — offsets spread over tens of seconds across the
        # runtime is the PAL signature, not a re-edit.
        if ratio_ok and not tried_ratios:
            tried_ratios = True
            r = ratio_detect(base, donor, dur, cfg, base_fps, donor_fps, base_dur, donor_dur,
                             tag, on_progress)
            if r:
                return r
        core.log(f"sync{tag}: inconsistent offsets {[int(o) for o in offs]} "
                 f"(searched +/-{cfg.get('sync_max_lag_s', 120)}s) -> reject")
        return None, max((c for _, _, c in vres), default=0.0), None, None
    # too few windows resolved — a rate mismatch smears every window, so try the ratios
    if ratio_ok and not tried_ratios:
        tried_ratios = True
        r = ratio_detect(base, donor, dur, cfg, base_fps, donor_fps, base_dur, donor_dur,
                         tag, on_progress)
        if r:
            return r
    # audio fallback at a central window
    s, d = wins[len(wins) // 2]
    try:
        am, ac = detect_offset_ms(base, base_ai, donor, donor_ai, start=int(s), dur=int(d),
                                  max_lag_s=cfg.get("sync_max_lag_s", 120),
                                  timeout=_decode_timeout(cfg))
    except Exception:
        am, ac = None, 0.0
    if am is not None and ac >= max(amin, 0.35):
        return int(round(am)), ac, "audio", None
    return None, ac, None, None
