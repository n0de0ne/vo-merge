"""Video-based offset detection via scene-cut cross-correlation.

Two releases of the same film cut between shots at the same content moments, so the
*timing pattern* of scene changes is an identical, language-independent fingerprint.
We detect cuts in a window of each file (downscaled for speed), turn them into spike
trains, and cross-correlate to find the constant offset. More robust than audio when
the music/effects beds differ. Returns (offset_ms, confidence); offset_ms > 0 means
file_b is shifted later than file_a.
"""
import re, subprocess
from . import core
import numpy as np

TIMEOUT_RC = -9        # our own marker for "killed on timeout" (see _run)


def _run(path, start, dur, thresh, scale, threads, hwaccel, device, timeout=None):
    pre = ["nice", "-n", "19", "ffmpeg", "-v", "info", "-threads", str(threads)]
    # Keep the downscale ON THE GPU (scale_vaapi/scale_qsv + hwdownload) so only tiny frames
    # cross PCIe — decode+scale of 4K stays on the iGPU. ~7x faster than '-hwaccel vaapi' alone
    # (which downloads full 4K surfaces to do a software scale).
    if hwaccel == "vaapi":
        pre += ["-hwaccel", "vaapi", "-hwaccel_device", device, "-hwaccel_output_format", "vaapi"]
        vf = f"scale_vaapi={scale}:-2,hwdownload,format=nv12,select='gt(scene,{thresh})',showinfo"
    elif hwaccel == "qsv":
        pre += ["-hwaccel", "qsv", "-hwaccel_output_format", "qsv"]
        vf = f"scale_qsv={scale}:-2,hwdownload,format=nv12,select='gt(scene,{thresh})',showinfo"
    else:
        vf = f"scale={scale}:-2,select='gt(scene,{thresh})',showinfo"
    cmd = pre + ["-ss", str(start), "-t", str(dur), "-i", path,
                 "-vf", vf, "-an", "-sn", "-f", "null", "-"]
    try:
        # core.run_proc: same contract, but killable by an operator abort
        r = core.run_proc(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        # A hung decode (the iGPU driver wedging is the known one) used to block the merge worker
        # forever. TIMEOUT is reported as its own return code so scene_cuts does NOT then retry in
        # software — that path would hang for exactly as long a second time.
        return [], TIMEOUT_RC
    cuts = [float(m.group(1)) for m in re.finditer(r"pts_time:([0-9.]+)", r.stderr)]
    return cuts, r.returncode


def scene_cuts(path, start, dur, thresh=0.3, scale=160, threads=4,
               hwaccel="vaapi", device="/dev/dri/renderD128", timeout=None):
    """Scene-cut timestamps. Offloads decode to the iGPU (Intel QSV/VAAPI) when
    available — ~70% less CPU — and transparently falls back to software decode if
    hwaccel isn't present or fails. Run at nice 19 and capped to `threads`."""
    cuts, rc = _run(path, start, dur, thresh, scale, threads, hwaccel, device, timeout)
    # only fall back to (slow, full-res) software decode on a real hwaccel failure — NOT on a
    # legitimately low-action window, which would needlessly software-decode 4K, and NOT on a
    # timeout, where the fallback would simply hang for the same duration again.
    if hwaccel and rc != TIMEOUT_RC and (rc != 0 or len(cuts) == 0):
        cuts, rc = _run(path, start, dur, thresh, scale, threads, None, device, timeout)
    return np.array(cuts)


def _train(cuts, dur, sr):
    n = int(dur * sr) + 1
    x = np.zeros(n)
    for t in cuts:
        i = int(round(t * sr))
        if 0 <= i < n:
            x[i] = 1.0
    # light gaussian smoothing so frame-level jitter still correlates
    k = np.exp(-0.5 * (np.arange(-4, 5) / 1.5) ** 2)
    return np.convolve(x, k / k.sum(), mode="same")


def ratio_scan(file_a, file_b, ratios, start=600, dur=2400, max_lag_s=180, bin_ms=20,
               threads=4, hwaccel="vaapi", device="/dev/dri/renderD128", timeout=None):
    """Test candidate RATE RATIOS between two files (PAL speedup & friends).

    A plain cross-correlation can only find a constant shift, so it is blind to a rate
    difference — worse, it is actively broken by one: at the PAL ratio (4.27%) the cut pattern
    smears ~20s *within* a single 480s window, the peak flattens, and confidence collapses.
    That is why a PAL pair reports "no reliable drift could be measured".

    Here the expensive part — one ffmpeg scene-cut pass per file — runs ONCE over a long span.
    Each candidate ratio `k` then only costs a numpy rescale + FFT, so testing a dozen
    hypotheses is nearly free.

    `k` maps donor timestamps onto the base timeline: base_t = k * donor_t + offset, which is
    exactly what `mkvmerge --sync TID:offset,k` applies.
    Returns [(k, offset_ms, conf), ...] sorted by confidence, best first (empty if too few cuts).
    """
    ca = scene_cuts(file_a, start, dur, threads=threads, hwaccel=hwaccel, device=device,
                    timeout=timeout)
    cb = scene_cuts(file_b, start, dur, threads=threads, hwaccel=hwaccel, device=device,
                    timeout=timeout)
    if len(ca) < 12 or len(cb) < 12:      # need a real pattern to match, not a handful of cuts
        return []
    sr = 1000 // bin_ms
    span = dur * max(1.0, max(ratios)) + max_lag_s + 5
    a = _train(ca, span, sr)
    na = np.linalg.norm(a)
    nfft = 1 << int(np.ceil(np.log2(2 * len(a))))
    fa = np.fft.rfft(a, nfft)
    ml = int(max_lag_s * sr)
    out = []
    for k in ratios:
        b = _train(cb * k, span, sr)       # rescale the donor's time axis onto the base's
        nb = np.linalg.norm(b)
        if nb <= 0:
            continue
        cc = np.fft.irfft(fa * np.conj(np.fft.rfft(b, nfft)), nfft)
        cc = np.concatenate((cc[-ml:], cc[:ml + 1]))
        lag = (int(np.argmax(cc)) - ml) * 1000.0 / sr        # ms, in window-relative coords
        conf = float(cc.max() / (na * nb + 1e-9))
        # back to absolute: base_abs = k*donor_abs + off, with both windows read at `start`
        off = lag + start * 1000.0 * (1.0 - k)
        out.append((k, off, conf))
    out.sort(key=lambda x: -x[2])
    return out


def detect_offset_video_ms(file_a, file_b, start=300, dur=600, max_lag_s=120, bin_ms=20,
                           threads=4, hwaccel="vaapi", device="/dev/dri/renderD128",
                           timeout=None):
    """Constant offset between two files, from their scene-cut patterns over one window.

    `max_lag_s` is the largest offset that can be FOUND, and it used to be 20s — which is a real
    ceiling, not a tuning knob: a BD-vs-WEB anime pair routinely differs by 30–60s (a sponsor or
    logo card the WEB version carries, a "previously on" the BD drops). Past 20s the true
    correlation peak was sliced away before the argmax, every window locked onto noise, the
    windows disagreed, and detect() concluded "different cut" — for a plain constant offset that
    `--sync` would have fixed. Verified on synthetic cut trains: 33s/34s/52s/67s are all missed
    at ±20s and recovered at ±90s with conf 0.91-0.98.

    Widening costs nothing: the FFT is already computed over the whole window, and only the slice
    the argmax runs over changes. It is also safe — the confidence is normalised, so an unrelated
    pair does not correlate at ANY lag (0/200 random pairs cleared the 0.30 gate at ±20, ±90 or
    ±180s), and detect() still requires several windows to agree within 150ms."""
    ca = scene_cuts(file_a, start, dur, threads=threads, hwaccel=hwaccel, device=device,
                    timeout=timeout)
    cb = scene_cuts(file_b, start, dur, threads=threads, hwaccel=hwaccel, device=device,
                    timeout=timeout)
    if len(ca) < 5 or len(cb) < 5:
        return None, 0.0
    sr = 1000 // bin_ms
    a, b = _train(ca, dur, sr), _train(cb, dur, sr)
    n = len(a)
    nfft = 1 << int(np.ceil(np.log2(2 * n)))
    cc = np.fft.irfft(np.fft.rfft(a, nfft) * np.conj(np.fft.rfft(b, nfft)), nfft)
    ml = max(1, min(int(max_lag_s * sr), n - 1))   # can't search further than the window is long
    cc = np.concatenate((cc[-ml:], cc[:ml + 1]))
    lag = int(np.argmax(cc)) - ml
    conf = float(cc.max() / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9))
    return lag * 1000.0 / sr, conf
