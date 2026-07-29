"""Audio offset detection via energy-envelope cross-correlation.

Two releases of the same film share the music & sound-effects bed even when the
dialogue language differs, so cross-correlating the amplitude envelopes finds the
constant offset between an added audio track and the base. Returns (offset_ms,
confidence). offset_ms > 0 means `shift` track lags `ref` (plays late).
"""
import json, subprocess
import numpy as np


def audio_langs(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a",
                        "-show_entries", "stream_tags=language", "-of", "json", path],
                       capture_output=True, text=True, timeout=120)
    out = []
    for s in json.loads(r.stdout or '{"streams":[]}').get("streams", []):
        out.append((s.get("tags") or {}).get("language", "und"))
    return out


def _pcm(path, ai, start, dur, sr, timeout=None):
    """Decoded mono PCM for one window, or an empty array if the decode failed or hung.

    An empty array leaves the caller below its minimum-samples gate, so a wedged ffmpeg degrades
    to "no audio answer" instead of blocking the merge worker forever."""
    try:
        p = subprocess.run(["nice", "-n", "19", "ffmpeg", "-v", "error", "-threads", "2",
                            "-ss", str(start), "-t", str(dur),
                            "-i", path, "-map", f"0:a:{ai}", "-ac", "1", "-ar", str(sr),
                            "-f", "f32le", "-"], capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return np.array([], dtype=np.float64)
    return np.frombuffer(p.stdout, dtype=np.float32).astype(np.float64)


def detect_offset_ms(ref_file, ref_ai, shift_file, shift_ai,
                     sr=8000, start=300, dur=240, max_lag_s=120, timeout=None):
    a = _pcm(ref_file, ref_ai, start, dur, sr, timeout)
    b = _pcm(shift_file, shift_ai, start, dur, sr, timeout)
    n = min(len(a), len(b))
    if n < sr * 20:                       # need a decent window
        return None, 0.0
    a = np.abs(a[:n]); b = np.abs(b[:n])  # energy envelope (shared music/SFX)
    a -= a.mean(); b -= b.mean()
    nfft = 1 << int(np.ceil(np.log2(2 * n)))
    cc = np.fft.irfft(np.fft.rfft(a, nfft) * np.conj(np.fft.rfft(b, nfft)), nfft)
    ml = max(1, min(int(max_lag_s * sr), len(a) - 1))   # never search past the window length
    cc = np.concatenate((cc[-ml:], cc[:ml + 1]))
    lag = int(np.argmax(cc)) - ml
    conf = float(cc.max() / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9))
    return lag * 1000.0 / sr, conf
