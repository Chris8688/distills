"""A股 ETF 日K（TickFlow，`free-api.tickflow.org`，免费无 key）。

A股 `adjust=forward` 生效（前复权，含分红与份额拆分）。512890 在 2021-10-25 有份额拆分，
用不复权价算均线会在拆分后半年内全部失真 —— 信号一律用前复权。
"""
from __future__ import annotations

import datetime
import gzip
import json
import time
import urllib.parse
import urllib.request

BASE = "https://free-api.tickflow.org"
UA = {"User-Agent": "Mozilla/5.0", "Accept-Encoding": "gzip"}
THROTTLE = 0.35          # 秒
_last = 0.0


def tf(code: str) -> str:
    """6 位 A股代码 → TickFlow 代码。⚠️ 5xxxxx 是上交所基金 / ETF（512890、510880 都在上交所）。"""
    raw = str(code).strip().upper().split(".")[0].zfill(6)
    return f"{raw}.{'SH' if raw[0] in '695' else 'BJ' if raw[0] in '48' else 'SZ'}"


def klines(code: str, count: int = 6000, adjust: str = "forward") -> list[dict]:
    """[{date, open, high, low, close, volume}]，按日期升序。adjust: forward / none。"""
    global _last
    w = _last + THROTTLE - time.time()
    if w > 0:
        time.sleep(w)
    _last = time.time()
    sym = tf(code)
    q = urllib.parse.urlencode({"symbols": sym, "period": "1d", "count": int(count), "adjust": adjust})
    with urllib.request.urlopen(urllib.request.Request(f"{BASE}/v1/klines/batch?{q}", headers=UA),
                                timeout=60) as r:
        raw = r.read()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    blk = (json.loads(raw.decode("utf-8")).get("data") or {}).get(sym) or {}
    ts = blk.get("timestamp") or []
    cl = blk.get("close") or []
    op = blk.get("open") or cl
    return [{
        # 时间戳是 UTC+8 零点
        "date": datetime.datetime.fromtimestamp(int(t) / 1000 + 8 * 3600, datetime.UTC).strftime("%Y-%m-%d"),
        "open": float(op[j]), "close": float(cl[j]),
        "high": float(blk["high"][j]), "low": float(blk["low"][j]),
        "volume": float((blk.get("volume") or [0] * len(ts))[j]),
    } for j, t in enumerate(ts)]
