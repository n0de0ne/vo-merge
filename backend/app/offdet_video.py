"""Video-based offset detection via scene-cut cross-correlation.

Two releases of the same film cut between shots at the same content moments, so the
*timing pattern* of scene changes is an identical, language-independent fingerprint.
We detect cuts in a window of each file (downscaled for speed), turn them into spike
trains, and cross-correlate to find the constant offset. More robust than audio when
the music/effects beds differ. Returns (offset_ms, confidence); offset_ms > 0 means
file_b is shifted later than file_a.
"""
import re, subprocess
import numpy as np


def scene_cuts(path, start, dur, thresh=0.3, scale=160):
    """Timestamps (s, relative to `start`) of scene cuts in the window."""
    cmd = ["ffmpeg", "-v", "info", "-ss", str(start), "-t", str(dur), "-i", path,
           "-vf", f"scale={scale}:-2,select='gt(scene,{thresh})',showinfo",
           "-an", "-sn", "-f", "null", "-"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    return np.array([float(m.group(1)) for m in re.finditer(r"pts_time:([0-9.]+)", r.stderr)])


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


def detect_offset_video_ms(file_a, file_b, start=300, dur=600, max_lag_s=20, bin_ms=20):
    ca, cb = scene_cuts(file_a, start, dur), scene_cuts(file_b, start, dur)
    if len(ca) < 5 or len(cb) < 5:
        return None, 0.0
    sr = 1000 // bin_ms
    a, b = _train(ca, dur, sr), _train(cb, dur, sr)
    n = len(a)
    nfft = 1 << int(np.ceil(np.log2(2 * n)))
    cc = np.fft.irfft(np.fft.rfft(a, nfft) * np.conj(np.fft.rfft(b, nfft)), nfft)
    ml = int(max_lag_s * sr)
    cc = np.concatenate((cc[-ml:], cc[:ml + 1]))
    lag = int(np.argmax(cc)) - ml
    conf = float(cc.max() / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9))
    return lag * 1000.0 / sr, conf
