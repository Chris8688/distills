"""申万二级行业指数日K（124 个，1999-12 起），作 ETF 的长历史代理。

来源 akshare：`index_realtime_sw("二级行业")` 拿代码表，`index_hist_sw(code, "day")` 拿日K。
全量约 3~5 分钟，缓存 7 天。
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import time

from . import CACHE

PATH = os.path.join(CACHE, "sw2_ohlc.json")
TTL_DAYS = 7


def _stale(d: dict, ttl: int) -> bool:
    ts = d.get("_ts")
    if not ts:
        return True
    return (_dt.date.today() - _dt.date.fromisoformat(ts)).days > ttl


def fetch_sw2(*, ttl_days: int = TTL_DAYS, offline: bool = False,
              quiet: bool = False) -> dict[str, list[dict]]:
    """{行业名: [{date, open, high, low, close, volume, amount}]}。缓存过期才联网。

    offline=True：只读缓存（过期也用），没有缓存返回 {}。单个行业抓取失败会提示并跳过。
    """
    old = json.load(open(PATH)) if os.path.exists(PATH) else {}
    if offline or (old and not _stale(old, ttl_days)):
        return {k: v for k, v in old.items() if not k.startswith("_")}
    import akshare as ak
    if not quiet:
        print("  抓申万二级行业指数日K（124 个，约 3~5 分钟，缓存 7 天）…", flush=True)
    info = ak.index_realtime_sw(symbol="二级行业")
    out: dict = {}
    miss = []
    for code, name in zip(info["指数代码"], info["指数名称"]):
        h = None
        for _ in range(3):
            try:
                h = ak.index_hist_sw(symbol=str(code), period="day")
                break
            except Exception:      # noqa: BLE001  申万接口偶发超时，重试
                time.sleep(2)
        if h is None or h.empty:
            miss.append(name)
            continue
        h = h.rename(columns={"日期": "date", "开盘": "open", "最高": "high", "最低": "low",
                              "收盘": "close", "成交量": "volume", "成交额": "amount"})
        cols = [k for k in ("open", "high", "low", "close", "volume", "amount") if k in h.columns]
        out[str(name)] = [{"date": str(r["date"])[:10], **{k: float(r[k]) for k in cols}}
                          for _, r in h.iterrows()]
    if miss and not quiet:
        print(f"  ⚠️ 申万二级 {len(miss)} 个取不到：{miss[:8]}", flush=True)
    if not out:                      # 全挂了：别用空表覆盖旧缓存
        return {k: v for k, v in old.items() if not k.startswith("_")}
    out["_ts"] = _dt.date.today().isoformat()
    os.makedirs(CACHE, exist_ok=True)
    json.dump(out, open(PATH, "w"))
    return {k: v for k, v in out.items() if not k.startswith("_")}
