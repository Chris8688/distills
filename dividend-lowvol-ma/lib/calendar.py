"""A股交易日历：最近一个**已收盘**的交易日。

交易日表来自 akshare `tool_trade_date_hist_sina`（新浪，含当年剩余交易日），缓存 30 天。
今天是交易日、且北京时间已过 15:00，才把今天算进来。
"""
from __future__ import annotations

import datetime as dt
import json
import os

from . import CACHE

PATH = os.path.join(CACHE, "cn_trade_dates.json")
TTL_DAYS = 30
CLOSE = "15:00"


def trade_dates(*, refresh: bool = False) -> list[str]:
    if os.path.exists(PATH) and not refresh:
        d = json.load(open(PATH))
        if (dt.date.today() - dt.date.fromisoformat(d["_ts"])).days <= TTL_DAYS:
            return d["dates"]
    import akshare as ak
    dates = sorted(str(x)[:10] for x in ak.tool_trade_date_hist_sina()["trade_date"])
    os.makedirs(CACHE, exist_ok=True)
    json.dump({"_ts": dt.date.today().isoformat(), "dates": dates}, open(PATH, "w"))
    return dates


def last_completed_session(now: dt.datetime | None = None) -> dt.date | None:
    """覆盖不到（交易日表还没出到今天）返回 None，调用方自行处理。"""
    from zoneinfo import ZoneInfo
    now = now or dt.datetime.now(ZoneInfo("Asia/Shanghai"))
    dates = trade_dates()
    today = now.date().isoformat()
    if not dates or dates[-1] < today:
        return None
    done = [d for d in dates if d < today or (d == today and now.strftime("%H:%M") >= CLOSE)]
    return dt.date.fromisoformat(done[-1]) if done else None
