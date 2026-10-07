"""全市场 A 股不复权日线（含退市股），自包含取数：TickFlow 免费端点。

- adjust=none 给不复权 OHLC；再拉 adjust=forward，用两者之比得复权因子 F_t，
  推出除权调整后的昨收：preclose_t = close_{t-1} × F_{t-1} / F_t（非除权日 ≈ close_{t-1}）。
- 股票池：缓存里的 stock_basic.csv（含退市股；首次由 baostock 生成，见 SKILL.md）。
- 缓存：~/.trade-strategy/distill/youzi/market/daily.parquet（单表，code/date 主键）。
"""
from __future__ import annotations

import gzip
import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

CACHE = Path.home() / ".trade-strategy/distill/youzi/market"
DAILY = CACHE / "daily.parquet"
BASIC = CACHE / "stock_basic.csv"
URL = "https://free-api.tickflow.org/v1/klines/batch?"
UA = {"User-Agent": "Mozilla/5.0 (distill-youzi)", "Accept-Encoding": "gzip"}
START = "2009-01-01"


def tf_symbol(code: str) -> str:
    c = code.split(".")[-1]
    if c.startswith(("6", "9")) and not c.startswith("92"):
        return f"{c}.SH"
    if c.startswith(("8", "4", "92")):
        return f"{c}.BJ"
    return f"{c}.SZ"


def _req(symbols: list[str], count: int, adjust: str, tries: int = 5) -> dict:
    q = urllib.parse.urlencode({"symbols": ",".join(symbols), "period": "1d", "count": count, "adjust": adjust})
    for k in range(tries):
        try:
            time.sleep(0.4)
            with urllib.request.urlopen(urllib.request.Request(URL + q, headers=UA), timeout=120) as r:
                raw = r.read()
            if raw[:2] == b"\x1f\x8b":
                raw = gzip.decompress(raw)
            return (json.loads(raw.decode()) or {}).get("data") or {}
        except Exception:
            time.sleep(3 * (k + 1))
    raise RuntimeError(f"TickFlow 请求失败: {symbols[:3]}… adjust={adjust}")


def _frame(blk: dict) -> pd.DataFrame:
    ts = blk.get("timestamp") or []
    if not ts:
        return pd.DataFrame()
    d = pd.to_datetime(np.array(ts, dtype="int64") + 8 * 3600 * 1000, unit="ms").normalize()
    return pd.DataFrame({"date": d, "open": blk["open"], "high": blk["high"], "low": blk["low"],
                         "close": blk["close"], "volume": blk.get("volume"), "amount": blk.get("amount")})


def fetch(codes: list[str], count: int = 6000, batch: int = 40, log=print) -> pd.DataFrame:
    out = []
    syms = [tf_symbol(c) for c in codes]
    for i in range(0, len(syms), batch):
        chunk = syms[i:i + batch]
        raw = _req(chunk, count, "none")
        fwd = _req(chunk, count, "forward")
        for s in chunk:
            r = _frame(raw.get(s) or {})
            if r.empty:
                continue
            f = _frame(fwd.get(s) or {})
            r["code"] = s.split(".")[0]
            if not f.empty:
                f = f[["date", "close"]].rename(columns={"close": "fclose"})
                r = r.merge(f, on="date", how="left")
                fac = (r["fclose"] / r["close"]).replace([np.inf, -np.inf], np.nan).ffill().bfill()
                ratio = fac.shift(1) / fac
                ratio = ratio.where((ratio - 1).abs() > 0.002, 1.0)  # 复权价有舍入噪声：非除权日强制 = 1
                r["preclose"] = (r["close"].shift(1) * ratio).round(2)
                r = r.drop(columns="fclose")
            else:
                r["preclose"] = r["close"].shift(1)
            out.append(r[r["date"] >= START])
        log(f"  {min(i + batch, len(syms))}/{len(syms)}")
    if not out:
        return pd.DataFrame()
    df = pd.concat(out, ignore_index=True)
    for c in ["open", "high", "low", "close", "preclose"]:
        df[c] = df[c].astype("float32")
    return df


def universe() -> pd.DataFrame:
    b = pd.read_csv(BASIC, dtype=str).fillna("")
    b["code6"] = b["code"].str.split(".").str[-1]
    return b


def update(full: bool = False, log=print) -> pd.DataFrame:
    """full=True 全量重拉；否则只拉最近 40 根并合并（每日收盘后跑）。"""
    CACHE.mkdir(parents=True, exist_ok=True)
    u = universe()
    if full or not DAILY.exists():
        codes = u[(u["outDate"] == "") | (u["outDate"] >= START)]["code6"].tolist()
        df = fetch(codes, 6000, log=log)
    else:
        old = pd.read_parquet(DAILY)
        codes = u[u["outDate"] == ""]["code6"].tolist()
        new = fetch(codes, 40, batch=100, log=log)
        df = pd.concat([old, new]).drop_duplicates(["code", "date"], keep="last")
    df = df.sort_values(["code", "date"]).reset_index(drop=True)
    df.to_parquet(DAILY, index=False)
    return df


def load() -> pd.DataFrame:
    return pd.read_parquet(DAILY)
