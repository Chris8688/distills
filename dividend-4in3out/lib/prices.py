"""A股日K与市值（TickFlow）。

数据源 TickFlow（`free-api.tickflow.org`，免费无 key）。
⚠️ 港股 `adjust=forward` 不生效（返回值同 `none`），必须取 `backward`
再乘「最新原始价 ÷ 最新后复权价」，这个换算是精确的。
A股 `forward` 生效。
指标（收益率 / 回撤 / Calmar）都与尺度无关，前后复权结论相同；统一前复权只为口径一致。
"""
from __future__ import annotations

import datetime
import gzip
import json
import os
import threading
import time
import urllib.parse
import urllib.request

from . import CACHE

BASE = "https://free-api.tickflow.org"
UA = {"User-Agent": "Mozilla/5.0", "Accept-Encoding": "gzip"}
BATCH_MAX = 100          # 硬上限，别调大
THROTTLE = 0.35          # 秒

_lock = threading.Lock()
_last = 0.0


def tf(code: str, market: str) -> str:
    """任意格式 → TickFlow 代码。港股 5 位补零 + .HK；A股 6 位 + .SH/.SZ/.BJ；美股裸 ticker。"""
    c = str(code).strip().upper()
    if market == "HK":
        raw = c.split(".", 1)[1] if c.startswith("HK.") else c.split(".")[0]
        return f"{int(raw):05d}.HK"
    if market == "CN":
        raw = c.split(".")[0].zfill(6)
        if "." in c and c.split(".")[1] in ("SH", "SZ", "BJ"):
            return f"{raw}.{c.split('.')[1]}"
        # ⚠️ 5xxxxx 是上交所基金 / ETF，不是深市（518880、588000、512400 都在上交所）
        #    SH: 6xx / 688 / 9xx / 5xx　SZ: 00x / 30x / 15x / 16x / 18x / 2xx　BJ: 4xx / 8xx
        sfx = ("SH" if raw[0] in "695" else
               "BJ" if raw[0] in "48" else "SZ")
        return f"{raw}.{sfx}"
    return c.split(".", 1)[1] if c.startswith("US.") else c


def _get(path: str, params: dict, timeout: float = 60.0) -> dict:
    global _last
    with _lock:
        w = _last + THROTTLE - time.time()
        if w > 0:
            time.sleep(w)
        _last = time.time()
    url = f"{BASE}{path}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA),
                               timeout=timeout) as r:
        raw = r.read()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return json.loads(raw.decode("utf-8"))


def _klines(symbols: list[str], count: int, adjust: str) -> dict:
    out: dict[str, list[dict]] = {}
    for i in range(0, len(symbols), BATCH_MAX):
        ch = symbols[i:i + BATCH_MAX]
        d = _get("/v1/klines/batch",
                 {"symbols": ",".join(ch), "period": "1d",
                  "count": int(count), "adjust": adjust},
                 timeout=max(30.0, 30.0 + len(ch) * 0.5))
        for s, blk in ((d.get("data") or {}) or {}).items():
            ts = (blk or {}).get("timestamp") or []
            if not ts:
                continue
            cl, hi, lo = blk["close"], blk["high"], blk["low"]
            op = blk.get("open") or cl
            vo = blk.get("volume") or [0] * len(ts)
            out[s] = [{
                # 时间戳是 UTC+8 零点
                "date": datetime.datetime.fromtimestamp(
                    int(t) / 1000 + 8 * 3600, datetime.UTC).strftime("%Y-%m-%d"),
                "open": float(op[j]), "close": float(cl[j]), "high": float(hi[j]),
                "low": float(lo[j]), "volume": float(vo[j]),
            } for j, t in enumerate(ts)]
    return out


OWN_CACHE = CACHE


def market_caps(market: str = "CN", *, refresh: bool = False) -> list[dict]:
    """全市场市值排序（`total_shares × 最新原始价`）。
    ⚠️ 必须用 `adjust=none` 的价 —— 复权价乘股本不是市值。"""
    market = market.upper()
    p = os.path.join(CACHE, f"{market.lower()}_mcap.json")
    if os.path.exists(p) and not refresh:
        return json.load(open(p))
    exchanges = {"CN": ("SH", "SZ"), "HK": ("HK",), "US": ("US",)}[market]
    inst = []
    for ex in exchanges:
        d = _get(f"/v1/exchanges/{ex}/instruments", {"type": "stock"}, timeout=180.0)
        inst += (d.get("data") if isinstance(d, dict) else d) or []
    have = [x for x in inst if (x.get("ext") or {}).get("total_shares")]
    px1: dict[str, float] = {}
    for i in range(0, len(have), BATCH_MAX):
        ch = [x["symbol"] for x in have[i:i + BATCH_MAX]]
        try:
            px1.update({s: b[-1]["close"] for s, b in _klines(ch, 1, "none").items() if b})
        except Exception:      # noqa: BLE001
            pass
    rows = [{"symbol": x["symbol"], "code": x["code"], "name": x.get("name", ""),
             "price": px1[x["symbol"]],
             "shares": float(x["ext"]["total_shares"]),
             "float_shares": (x["ext"] or {}).get("float_shares"),
             "listing_date": (x["ext"] or {}).get("listing_date"),
             "mcap": px1[x["symbol"]] * float(x["ext"]["total_shares"])}
            for x in have if x["symbol"] in px1]
    rows.sort(key=lambda r: -r["mcap"])
    os.makedirs(CACHE, exist_ok=True)
    json.dump(rows, open(p, "w"), ensure_ascii=False)
    return rows
