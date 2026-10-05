"""美股 SEC XBRL 单季 point-in-time 视图（研究数据仓 `fundamentals.jsonl.gz`，232 只）。

⚠️ 三个坑（都实测踩过）：
· 单季只有 10-Q 的 3 个月区间；**Q4 要用年报 − 同起点 9 个月 YTD 推**，否则每年少一季
· 同一期间多次报送 → 取**最早报送**的值（当时能看到的那个）
· 价格是拆股复权的而股数是当期原样报送 → 市值 = 复权价 × 报送股数 × **之后所有拆股比**
  （拆股从股数跳 ≥1.8 倍且接近整数比识别）
"""
from __future__ import annotations

import collections
import datetime as dt
import gzip
import json
import os

from . import DATA

FLOWS = ("net_income", "revenue", "operating_income", "op_cash_flow")
POINTS = ("equity", "assets")


def _d(s: str) -> dt.date:
    return dt.date.fromisoformat(s[:10])


def _months(a: dt.date, b: dt.date) -> float:
    return (b - a).days / 30.44


def load_raw() -> tuple[dict, dict, dict]:
    """→ quarters[tk][concept] = [(end, filed, val)]（单季，含 A−YTD9 推出的 Q4）
         points[tk][concept]   = [(end, filed, val)]
         shares[tk]            = [(end, val)]（用来识别拆股）"""
    recs = collections.defaultdict(list)
    with gzip.open(os.path.join(DATA, "fundamentals.jsonl.gz"), "rt") as fh:
        for line in fh:
            r = json.loads(line)
            c = r["concept"]
            if c in FLOWS or c in POINTS or c == "shares_diluted":
                recs[(r["ticker"], c)].append(r)
    quarters = collections.defaultdict(dict)
    points = collections.defaultdict(dict)
    shares = collections.defaultdict(list)
    for (tk, c), rs in recs.items():
        if c in POINTS:
            best = {}
            for r in rs:
                if r["kind"] != "point" or not r.get("filed"):
                    continue
                e = r["end"]
                if e not in best or r["filed"] < best[e][0]:
                    best[e] = (r["filed"], r["val"])
            points[tk][c] = sorted((e, f, v) for e, (f, v) in best.items())
            continue
        if c == "shares_diluted":
            for r in rs:
                if r["kind"] == "Q" and r.get("start") and 75 <= (_d(r["end"]) - _d(r["start"])).days <= 100:
                    shares[tk].append((r["end"], r["val"]))
            shares[tk] = sorted(set(shares[tk]))
            continue
        # 流量：单季 = 3 个月区间；Q4 = 年报 − 同起点 9 个月 YTD
        q, ytd9 = {}, {}
        for r in rs:
            if not r.get("start") or not r.get("filed"):
                continue
            days = (_d(r["end"]) - _d(r["start"])).days
            key = (r["start"], r["end"])
            if 75 <= days <= 100:
                if r["end"] not in q or r["filed"] < q[r["end"]][0]:
                    q[r["end"]] = (r["filed"], r["val"])
            elif 255 <= days <= 285:
                if key not in ytd9 or r["filed"] < ytd9[key][0]:
                    ytd9[key] = (r["filed"], r["val"])
        for r in rs:
            if r["kind"] != "A" or not r.get("start") or not r.get("filed"):
                continue
            if not 350 <= (_d(r["end"]) - _d(r["start"])).days <= 380 or r["end"] in q:
                continue
            for (s9, e9), (f9, v9) in ytd9.items():
                if s9 == r["start"]:
                    q[r["end"]] = (max(r["filed"], f9), r["val"] - v9)
                    break
        quarters[tk][c] = sorted((e, f, v) for e, (f, v) in q.items())
    return quarters, points, shares


def split_factors(sh: list) -> list:
    """拆股识别：相邻单季稀释股数跳 ≥1.8 倍且接近整数比 → 记 (end, 比例)。
    yfinance 价格是全程拆股复权的，所以历史市值 = 复权价 × 当期报送股数 × 之后所有拆股比。"""
    out = []
    for (e0, v0), (e1, v1) in zip(sh, sh[1:]):
        if v0 and v1 and v1 / v0 >= 1.8:
            r = v1 / v0
            k = round(r)
            if abs(r - k) / k < 0.12:
                out.append((e1, k))
    return out


def split_factor_after(splits: list, end: str) -> int:
    f = 1
    for e, k in splits:
        if e > end:
            f *= k
    return f


def visible(series: list, d: str) -> list:
    """filed ≤ d 且 end ≤ d 的那些（同一 end 只取最早报送值，已在载入时处理）。"""
    return [(e, v) for e, f, v in series if f <= d and e <= d]


def ttm_hist(qs: list) -> list:
    """可见单季序列 → [(end, TTM)]，要求 4 个季度首尾跨度 ≤ 13 个月（不许拼凑）。"""
    out = []
    for i in range(3, len(qs)):
        w = qs[i - 3:i + 1]
        if _months(_d(w[0][0]), _d(w[-1][0])) <= 10.5:
            out.append((w[-1][0], sum(v for _, v in w)))
    return out


