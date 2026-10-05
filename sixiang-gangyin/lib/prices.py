"""日K行情（CN / HK / US），返回前复权 bars。

主源 TickFlow（`free-api.tickflow.org`，免费无 key），美股和港股 ETF 走 yfinance。
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
from typing import Iterable

from . import CACHE, DATA

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


def _cache_files(market: str) -> tuple[str, ...]:
    """研究数据仓里已有的行情缓存，先读它省得重抓。"""
    if market == "CN":
        return ("cn_wide_px.json", "cn_candidate_px.json", "cn_expand_px.json",
                "cn_expand2_px.json", "cn_ks_px.json", "cn_bankins_px.json",
                "cn_star50_px.json", "cn_cyb50_px.json", "cn_orphan_px.json")
    if market == "HK":
        return ("hk_tf_px.json",)
    return ()


def fetch(codes: Iterable[str], market: str, *, count: int = 1600,
          cache: bool = True, quiet: bool = False, need_open: bool = True,
          max_stale_days: int = 5, offline: bool = False) -> dict[str, list[dict]]:
    """前复权日K，返回 {TickFlow代码: bars}。先读缓存，过期 / 偏短 / 缺失的才联网。

    offline=True：只读缓存，过期也照用，缺的就缺。
    need_open=True：缓存里没有 open 的条目算未命中。
    """
    market = market.upper()
    syms = [tf(c, market) for c in codes]
    px: dict[str, list[dict]] = {}
    own = os.path.join(CACHE, f"{market.lower()}_px.json")
    if cache:
        for f in _cache_files(market):
            p = os.path.join(DATA, f)
            if os.path.exists(p):
                try:
                    px.update(json.load(open(p)))
                except Exception:      # noqa: BLE001  缓存坏了就当没有
                    pass
        if os.path.exists(own):
            px.update(json.load(open(own)))
    cut = (datetime.date.today() - datetime.timedelta(days=min(max_stale_days, 36500))).isoformat()
    short_ok = set(px["_short_ok"]) if isinstance(px.get("_short_ok"), dict) else set()

    def _ok(s: str) -> bool:
        b = px.get(s)
        if not b or str(b[-1].get("date", ""))[:10] < cut:
            return False
        # 比请求短的缓存也算未命中，否则长窗口会静默塌缩成短窗口
        if len(b) < count * 0.95 and s not in short_ok:
            return False
        return (not need_open) or (b[-1].get("open") is not None)

    todo = [s for s in syms if not _ok(s)]
    if offline:
        if todo and not quiet:
            print(f"  ⚠️ offline：{len(todo)} 只缓存过期/缺失/偏短，照旧用或跳过", flush=True)
        return {s: px[s] for s in syms if s in px}
    if not todo:
        return {s: px[s] for s in syms if s in px}
    if market == "US":
        new = _fetch_yf(todo, count, quiet)
    else:
        if not quiet:
            print(f"  抓 {len(todo)} 只日K（TickFlow，前复权）…", flush=True)
        new = {}
        for i in range(0, len(todo), BATCH_MAX):
            ch = todo[i:i + BATCH_MAX]
            try:
                if market == "HK":
                    bw = _klines(ch, count, "backward")
                    rw = _klines(ch, 1, "none")
                    for s in ch:
                        b = bw.get(s)
                        if not b:
                            continue
                        last = (rw.get(s) or [{}])[-1].get("close")
                        k = (last / b[-1]["close"]) if last else 1.0
                        new[s] = [{**x, "open": x["open"] * k, "close": x["close"] * k,
                                   "high": x["high"] * k, "low": x["low"] * k} for x in b]
                else:
                    new.update(_klines(ch, count, "forward"))
            except Exception as e:      # noqa: BLE001
                print(f"  ⚠️ 批 {i} {type(e).__name__} {str(e)[:60]}")
        if market == "HK":
            # TickFlow 没有港股 ETF → yfinance 兜底（只认 4 位代码）
            miss = [s for s in todo if s not in new]
            if miss:
                new.update(_fetch_yf(miss, count, quiet, hk=True))
    confirmed = {s: len(new[s]) for s in todo if s in new and len(new[s]) < count * 0.95}
    px.update(new)
    if cache and new:
        os.makedirs(CACHE, exist_ok=True)
        old = json.load(open(own)) if os.path.exists(own) else {}
        old.update(new)
        if confirmed:
            so = old.get("_short_ok") if isinstance(old.get("_short_ok"), dict) else {}
            so.update(confirmed)
            old["_short_ok"] = so
        json.dump(old, open(own, "w"))
    return {s: px[s] for s in syms if s in px}


def _fetch_yf(syms: list[str], count: int, quiet: bool, hk: bool = False) -> dict:
    """yfinance，auto_adjust=True（含股息）。NaN 行剔除，否则会沿均线传播。"""
    import yfinance as yf_
    out = {}
    for s in syms:
        tk = f"{int(s.split('.')[0]):04d}.HK" if hk else s
        try:
            h = yf_.Ticker(tk).history(period="max", auto_adjust=True)
        except Exception as e:      # noqa: BLE001
            if not quiet:
                print(f"  ⚠️ {s} yfinance {type(e).__name__}")
            continue
        if len(h) < 10:
            continue
        out[s] = [{"date": str(i)[:10], "open": float(o), "close": float(c),
                   "high": float(a), "low": float(b), "volume": float(v) if v == v else 0.0}
                  for i, o, c, a, b, v in zip(h.index, h["Open"], h["Close"],
                                              h["High"], h["Low"], h["Volume"])
                  if c == c and o == o][-count:]
    return out
