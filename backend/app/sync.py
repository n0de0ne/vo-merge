"""Resilient A/V offset detection: multiple analysis windows, video scene-cut matching
primary, audio music/SFX cross-correlation as cross-check + fallback. Centralizes the
logic used by both the movie and episode merges."""
from . import core
from .offdet_video import detect_offset_video_ms
from .offdet import detect_offset_ms


def fps_close(a, b, tol=0.03):
    """True if framerates are close enough that a constant offset still works.
    23.976 vs 24.0 (0.1%) -> ok; 25 vs 23.976 (4%) -> not (PAL speedup, would drift)."""
    if not a or not b:
        return True
    return abs(a - b) / max(a, b) <= tol


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


def detect(base, donor, base_ai, donor_ai, dur, cfg, tag=""):
    """Returns (offset_ms|None, confidence, method). offset>0 delays the donor track."""
    vmin = cfg.get("sync_video_min_conf", 0.4)
    amin = cfg.get("auto_sync_min_conf", 0.2)
    wins = _windows(dur, n=cfg.get("sync_windows", 3), length=cfg.get("sync_window_dur", 480))
    vbest = (None, 0.0, None)
    for (s, d) in wins:
        try:
            m, c = detect_offset_video_ms(
                base, donor, start=int(s), dur=int(d),
                threads=cfg.get("sync_ffmpeg_threads", 4),
                hwaccel=cfg.get("sync_hwaccel", "vaapi"),
                device=cfg.get("sync_hwaccel_device", "/dev/dri/renderD128"))
        except Exception as e:
            core.log(f"sync{tag}: video window {int(s)}s error: {e}"); m, c = None, 0.0
        if m is not None and c > vbest[1]:
            vbest = (m, c, (s, d))
        if vbest[1] >= 0.6:             # confident enough, stop early
            break
    # audio at the best video window (fallback / cross-check)
    s, d = vbest[2] or wins[0]
    am, ac = None, 0.0
    try:
        am, ac = detect_offset_ms(base, base_ai, donor, donor_ai, start=int(s), dur=int(d))
        am = am if am is not None else None
    except Exception as e:
        core.log(f"sync{tag}: audio error: {e}")
    vm, vc, _ = vbest
    if vc >= vmin:
        return vm, vc, "video"
    if vm is not None and am is not None and vc >= 0.25 and abs(vm - am) <= 150:
        return vm, max(vc, ac), "video+audio"     # moderate video confirmed by audio
    if ac >= amin and am is not None:
        return am, ac, "audio"
    return None, max(vc, ac), None
