"""Protocol helpers shared by the listener plugins (spec 4.4): the v1 names that are not plugins, copied verbatim
from mira_listen_v1.py, plus a few small helpers the plugins need that v1 never had (path expansion, host rules,
time windows).

FIXED: RATE = 16000 (mic and Silero sample rate), CHUNK = 1600 (100 ms per read).
"""

import io
import sys
import time
import urllib.request
import wave
from pathlib import Path

import numpy as np

RATE = 16000
CHUNK = 1600  # 100 ms; small enough for snappy VAD, large enough that Python overhead stays low


def log(msg):
    print(f"{time.strftime('%H:%M:%S')} {msg}", file=sys.stderr, flush=True)


def resample(x: np.ndarray, r_in: int, r_out: int) -> np.ndarray:
    """Band-limited resample by FFT; only used for --wav inputs and tests, never in the live loop."""
    if r_in == r_out:
        return x
    n_out = int(round(len(x) * r_out / r_in))
    X = np.fft.rfft(x.astype(np.float64))
    Y = np.zeros(n_out // 2 + 1, dtype=complex)
    m = min(len(X), len(Y))
    Y[:m] = X[:m]
    return (np.fft.irfft(Y, n_out) * (n_out / max(len(x), 1))).astype(np.float32)


def read_wav_float(path) -> np.ndarray:
    return read_wav_float_bytes(path)


def read_wav_float_bytes(src) -> np.ndarray:
    """Path or WAV bytes -> float32 mono at 16 kHz."""
    with wave.open(io.BytesIO(src) if isinstance(src, bytes) else str(src), "rb") as w:
        n, ch, sw, rate = w.getnframes(), w.getnchannels(), w.getsampwidth(), w.getframerate()
        raw = w.readframes(n)
    if sw != 2:
        raise ValueError("only 16-bit WAV is supported")
    x = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    return resample(x, rate, RATE)


def pcm16_wav_bytes(x: np.ndarray, rate: int = RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes((np.clip(x, -1, 1) * 32767.0).astype("<i2").tobytes())
    return buf.getvalue()


def http(method, url, body=None, headers=None, timeout=10.0):
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


def read_secret(path):
    if not path:
        return ""
    try:
        return Path(path).read_text().strip()
    except OSError:
        return ""


# ---------------------------------------------------------------------------------- helpers new in P4

def expand_path(value, paths):
    """`~`, `${HOME}` and `${HERE}` in a path knob. Idempotent, so it is safe whether or not the core expanded first."""
    s = str(value)
    if not s:
        return s
    s = s.replace("${HERE}", str(paths.here)).replace("${HOME}", str(paths.home))
    if s == "~" or s.startswith("~/"):
        s = str(paths.home) + s[1:]
    return s


def _host(url):
    from urllib.parse import urlsplit
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _is_loopback(host):
    import ipaddress
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _is_tailnet(host):
    import ipaddress
    try:
        return ipaddress.ip_address(host) in ipaddress.ip_network("100.64.0.0/10")
    except ValueError:
        return False


def bind_is_loopback(bind):
    """A bare bind address (no scheme, no port) is loopback: 127.0.0.0/8, ::1 or localhost."""
    return _is_loopback(str(bind).strip().lower())


def host_is_loopback(url):
    """mirakit.schema's rule when that module has one, else the same rule here (a hook must not need P3 to be present)."""
    try:
        from mirakit.schema import host_is_loopback as rule
    except ImportError:
        return _is_loopback(_host(url))
    return bool(rule(url))


def host_is_loopback_or_tailnet(url):
    try:
        from mirakit.schema import host_is_loopback as lo, host_is_tailnet as tail
    except ImportError:
        host = _host(url)
        return _is_loopback(host) or _is_tailnet(host)
    return bool(lo(url) or tail(url))


def parse_window(window):
    """"HH:MM-HH:MM" -> (start_min, end_min) in minutes after midnight, or None for "" (no window)."""
    if not window:
        return None
    a, b = window.split("-")
    ah, am = a.split(":")
    bh, bm = b.split(":")
    return int(ah) * 60 + int(am), int(bh) * 60 + int(bm)


def in_window(window, minute_of_day):
    """True when the minute of the day lies inside the window; a window that ends before it starts wraps midnight.
    Start is inclusive, end exclusive; start == end is an empty window."""
    w = parse_window(window)
    if w is None:
        return False
    start, end = w
    if start == end:
        return False
    if start < end:
        return start <= minute_of_day < end
    return minute_of_day >= start or minute_of_day < end


def local_minute():
    t = time.localtime()
    return t.tm_hour * 60 + t.tm_min
