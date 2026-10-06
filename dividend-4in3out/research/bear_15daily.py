#!/usr/bin/env python3
"""现行纸面账户口径（15 只等权、单行业 ≤3、每日检查）在 2007-06 起长样本上的表现，对照 30 只·月度。

复用 author_gap.run（月内按日线收盘把股息率 / PE 缩放，「三出」「四进」按当天价格判断、当天收盘成交）；
数据 = research/backtest_bear.py 建好的 2007 起月度指标（先跑它）。净值记在月底 → 回撤为月末口径。
用法：python3 research/bear_15daily.py
"""
from __future__ import annotations

import datetime as dt
import os
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, ".."))

import author_gap as AG  # noqa: E402
import backtest_bear as BB  # noqa: E402
import backtest_exits as B  # noqa: E402
import buy_filters as BF  # noqa: E402
import relative_valuation as RV  # noqa: E402
import screen as S  # noqa: E402

SEGS = [("全程 2007-06 ~ 2026-09", "2007-06", "2026-09"), ("2007-10 顶 → 2008-10 底", "2007-10", "2008-10"),
        ("2008 全年", "2008-01", "2008-12"), ("2009 全年", "2009-01", "2009-12"),
        ("2015-06 ~ 2016-02", "2015-06", "2016-02"), ("2018 全年", "2018-01", "2018-12"),
        ("2007-06 ~ 2018-05", "2007-06", "2018-05"), ("2018-06 ~ 2026-09", "2018-06", "2026-09"),
        ("2019-12 ~ 2026-09", "2020-01", "2026-09"), ("近 5 年（2021-09 ~ 2026-09）", "2021-10", "2026-09")]


def seg(nav, a, b):
    s = [x for x in nav if a <= x[0][:7] <= b]
    prev = [x for x in nav if x[0][:7] < a]
    base = prev[-1][1] if prev else s[0][1]
    v = [base] + [x[1] for x in s]
    peak, m = v[0], 0.0
    for x in v:
        peak = max(peak, x)
        m = min(m, x / peak - 1)
    tot = v[-1] / base
    return tot - 1, (tot ** (12 / len(s)) - 1) if len(s) >= 12 else None, m


def main():
    BB.build()
    RV.FY0 = 2003                                   # enrich() 的分红表与建数据时一致
    B.ROWS_FILE, B.KL_FILE, B.START = BB.ROWS, BB.KL, BB.START
    by_t, px = B.load()
    by_t = BF.enrich_rel(BF.enrich(by_t))
    inds = S.industries(dt.date.today(), True)
    days = px["600000"][0]
    navs = {}
    for name, n, step in (("15 只 · 每日（现行）", 15, 1), ("30 只 · 月度", 30, 0)):
        nav, _ = AG.run(by_t, px, inds, days, n, step, AG.R2, start=BB.START, end=B.END)
        navs[name] = [(t, v) for t, v, _ in nav]
        print(f"… {name}", file=sys.stderr, flush=True)
    print("| 区间 | " + " | ".join(navs) + " |")
    print("|---|" + "---|" * len(navs))
    for lab, a, b in SEGS:
        cells = []
        for nav in navs.values():
            tot, cg, m = seg(nav, a, b)
            cells.append(f"{tot:+.1%} / {('%+.1f%%' % (cg * 100)) if cg is not None else '—'} / {m:.0%}")
        print(f"| {lab} | " + " | ".join(cells) + " |")


if __name__ == "__main__":
    main()
