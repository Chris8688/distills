#!/usr/bin/env python3
"""高股息是低估还是陷阱？—— A股全市场历史检验（每年 5 月底快照，看之后一年）。

做法：
  · 快照日 S = 每年 5 月最后一个交易日（年报已全部披露），财年 FY = S 年 - 1。
  · 股息率 = FY 全年每股现金分红（含中报）÷ S 日不复权收盘价。样本 = FY 有现金分红、代码可交易的 A 股。
  · 之后一年总收益 = 前复权价 S+1y / S - 1（前复权已把分红再投入算进去）；同时减去当年全样本中位数得到「超额」。
  · 下一年分红：FY+1 现金分红总额 / FY 现金分红总额（总额口径，避开送转摊薄）。
  · 「连续分红 ≥4 年」= FY 往前连续 4 个财年都有现金分红（策略的门槛之一）。

局限：
  · 只覆盖今天还能在 TickFlow 取到行情的股票 —— 退市股不在样本里（幸存者偏差，对高股息陷阱是**低估**风险）。
  · 没有逐期复现策略的全部判据（保底分红率 / 扣非需要逐期财报），只看股息率分组 + 连续分红门槛。

用法：python3 research/yield_trap.py [--years 2021 2022 2023 2024 2025]
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import statistics as st
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

import screen as S  # noqa: E402
from lib import prices as PX  # noqa: E402

BUCKETS = [(0, .02), (.02, .03), (.03, .04), (.04, .05), (.05, .06), (.06, .08), (.08, 1)]


def fy_cash(today: dt.date, fy: int) -> tuple[dict, dict]:
    """{code: 每股现金分红}, {code: 现金分红总额}（年报 + 中报 + 季报）。"""
    ps, tot = {}, {}
    for mmdd in ("1231", "0630", "0930", "0331"):
        for code, rows in S._fhps_period(f"{fy}{mmdd}", today, False).items():
            for r in rows:
                ps[code] = ps.get(code, 0.0) + r[2] / 10.0
                tot[code] = tot.get(code, 0.0) + r[0]
    return ps, tot


def klines_cached(syms: list[str], count: int, adjust: str) -> dict:
    """分批拉 TickFlow 日线；遇 429 退避重试；结果缓存 1 天（研究脚本，不进生产缓存）。"""
    import json
    import time
    import urllib.error
    p = S._cpath(f"research_klines_{adjust}_{count}.json")
    have = json.load(open(p)) if S._fresh(p, 24) else {}
    need = [s for s in syms if s not in have]
    for i in range(0, len(need), PX.BATCH_MAX):
        ch = need[i:i + PX.BATCH_MAX]
        for att in range(6):
            try:
                have.update(PX._klines(ch, count, adjust))
                break
            except urllib.error.HTTPError as e:
                if e.code != 429 or att == 5:
                    raise
                time.sleep(10 * (att + 1))
        time.sleep(1.0)
        if (i // PX.BATCH_MAX) % 10 == 9:
            print(f"    {adjust} {i + len(ch)}/{len(need)}", file=sys.stderr, flush=True)
            json.dump(have, open(p, "w"))
    json.dump(have, open(p, "w"))
    return have


def close_on(bars: list[dict], day: str) -> float | None:
    c = None
    for b in bars:
        if b["date"] > day:
            break
        c = b["close"]
    return c


def last_trading_day_of_may(bars_any: list[dict], year: int) -> str:
    ds = [b["date"] for b in bars_any if b["date"].startswith(f"{year}-05")]
    return ds[-1]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=int, nargs="+", default=[2021, 2022, 2023, 2024, 2025])
    a = ap.parse_args()
    today = dt.date.today()
    fys = sorted({y - 1 for y in a.years} | {y for y in a.years})
    lo = min(fys) - 4
    print(f"… 分红送配 {lo}~{max(fys)}", file=sys.stderr, flush=True)
    cash = {fy: fy_cash(today, fy) for fy in range(lo, max(fys) + 1)}
    names = {c: r.get("name", "") for c, r in S.market_caps(True).items()}
    codes = sorted({c for fy in (y - 1 for y in a.years) for c in cash[fy][0]
                    if S.tradable(c, names.get(c, ""))})
    print(f"… 行情 {len(codes)} 只（不复权 + 前复权）", file=sys.stderr, flush=True)
    sym = {PX.tf(c, "CN"): c for c in codes}
    count = (today.year - min(a.years) + 1) * 250 + 60
    raw_none = klines_cached(list(sym), count, "none")
    raw_fwd = klines_cached(list(sym), count, "forward")
    none = {sym[k]: v for k, v in raw_none.items() if k in sym}
    fwd = {sym[k]: v for k, v in raw_fwd.items() if k in sym}
    ref = max(none.values(), key=len)

    rows = []
    for y in a.years:
        s = last_trading_day_of_may(ref, y)
        s1 = last_trading_day_of_may(ref, y + 1) if y + 1 <= today.year and (today.year > y + 1 or today.month > 5) else None
        if not s1:
            print(f"  {y}: 之后一年未满，跳过", file=sys.stderr)
            continue
        fy = y - 1
        ps, tot = cash[fy]
        tot_next = cash.get(fy + 1, ({}, {}))[1]
        snap = []
        for c in codes:
            if c not in ps or c not in none or c not in fwd:
                continue
            p0 = close_on(none[c], s)
            f0, f1 = close_on(fwd[c], s), close_on(fwd[c], s1)
            if not p0 or not f0 or not f1 or none[c][0]["date"] > s:
                continue
            streak = 0
            while cash.get(fy - streak, ({}, {}))[0].get(c, 0) > 0:
                streak += 1
            snap.append({"y": y, "code": c, "yield": ps[c] / p0, "ret": f1 / f0 - 1, "streak": streak,
                         "div_chg": (tot_next.get(c, 0.0) / tot[c] - 1) if tot.get(c) else None})
        med = st.median(r["ret"] for r in snap)
        for r in snap:
            r["xs"] = r["ret"] - med
        rows += snap
        print(f"  {y}: 快照 {s} → {s1}，样本 {len(snap)}，全样本收益中位 {med:+.1%}", file=sys.stderr)

    def table(sel, title):
        print(f"\n### {title}（{len(sel)} 个股票·年）\n")
        print("| 股息率 | 样本 | 一年总收益中位 | 超额中位 | 胜率 | 跌超 20% | 次年分红砍 ≥30% | 次年不分红 |")
        print("|---|---|---|---|---|---|---|---|")
        for lo_, hi in BUCKETS:
            b = [r for r in sel if lo_ <= r["yield"] < hi]
            if len(b) < 10:
                continue
            dc = [r["div_chg"] for r in b if r["div_chg"] is not None]
            lab = f"≥{lo_:.0%}" if hi == 1 else f"{lo_:.0%}~{hi:.0%}"
            print(f"| {lab} | {len(b)} | {st.median(r['ret'] for r in b):+.1%} | {st.median(r['xs'] for r in b):+.1%} | "
                  f"{sum(r['ret'] > 0 for r in b) / len(b):.0%} | {sum(r['ret'] < -.2 for r in b) / len(b):.0%} | "
                  f"{sum(d <= -.3 for d in dc) / len(dc):.0%} | {sum(d <= -.999 for d in dc) / len(dc):.0%} |")

    table(rows, "全部分红股")
    table([r for r in rows if r["streak"] >= 4], "连续分红 ≥4 年")
    for y in sorted({r["y"] for r in rows}):
        table([r for r in rows if r["y"] == y and r["streak"] >= 4], f"{y} 年 5 月快照（连续分红 ≥4 年）")


if __name__ == "__main__":
    main()
