#!/usr/bin/env python3
"""买入过滤长样本检验（2007-06 起）：2026-10-05 落地的「相对飞刀 −25% + EPS ≤+70%」在 2007~2017 是否成立，
以及有没有更优方案。那次决定只用了 2018-06 起的数据，2007~2017 对它是样本外。

背景：旧版用绝对跌幅 —— 2008 年全市场普跌后，几乎所有便宜股过去一年都跌超 25%，2009 年被整体挡住。
相对飞刀只挡比同期长期分红股中位数跌得多的（个股自己的问题），不挡全市场普跌。

评判（不能因为一年的大牛市放弃大多数年份）：
  · 全程年化 / 月末回撤；去掉 2009 年后的年化
  · 逐年：跑赢现行（相对飞刀）的年数
  · 滚动 3 年、5 年窗口（每月一个起点）：跑赢现行的比例、最差窗口
  · 两段：2007-06~2016-12 / 2017-01~2026-09 都不差于现行才算稳
数据 = research/backtest_bear.py 建好的 2007 起月度指标。用法：python3 research/buy_filters_long.py
"""
from __future__ import annotations

import datetime as dt
import os
import statistics as st
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, ".."))

import backtest_bear as BB  # noqa: E402
from backtest_bear import add_relative, eps_cap, knife_rel  # noqa: E402
import backtest_exits as B  # noqa: E402
import screen as S  # noqa: E402

HALVES = [("2007-06", "2016-12"), ("2017-01", "2026-09")]


def knife_abs(th):
    return lambda r: r["p12"] is None or r["p12"] > th


def knife_regime(th, crash=-0.20):
    """绝对飞刀，但全市场中位数跌超 crash 时暂停（普跌不算飞刀）。"""
    return lambda r: (r["mkt12"] is not None and r["mkt12"] <= crash) or r["p12"] is None or r["p12"] > th


def both(*fs):
    fs = [f for f in fs if f]
    return (lambda r: all(f(r) for f in fs)) if fs else None


CANDS = {
    "原始（不加过滤）": None,
    "旧版：绝对飞刀 −25% + EPS ≤+70%": both(knife_abs(-0.25), eps_cap(0.7)),
    "只 EPS ≤+70%": eps_cap(0.7),
    "只绝对飞刀 −25%": knife_abs(-0.25),
    "绝对飞刀 −35% + EPS": both(knife_abs(-0.35), eps_cap(0.7)),
    "相对飞刀 −15% + EPS": both(knife_rel(-0.15), eps_cap(0.7)),
    "相对飞刀 −20% + EPS": both(knife_rel(-0.20), eps_cap(0.7)),
    "现行：相对飞刀 −25% + EPS": both(knife_rel(-0.25), eps_cap(0.7)),
    "相对飞刀 −30% + EPS": both(knife_rel(-0.30), eps_cap(0.7)),
    "普跌暂停的绝对飞刀 −25% + EPS": both(knife_regime(-0.25), eps_cap(0.7)),
}


def cagr(nav, a=None, b=None, skip_year=None):
    seg = [(t, v) for t, v in nav if (a or "") <= t[:7] <= (b or "9999")]
    rets = [(seg[i][0], seg[i][1] / seg[i - 1][1]) for i in range(1, len(seg))]
    if skip_year:
        rets = [x for x in rets if x[0][:4] != skip_year]
    g = 1.0
    for _, x in rets:
        g *= x
    return g ** (12 / max(1, len(rets))) - 1


def mdd(nav):
    peak, m = nav[0][1], 0.0
    for _, v in nav:
        peak = max(peak, v)
        m = min(m, v / peak - 1)
    return m


def yearly(nav):
    out, prev = {}, None
    for y in sorted({t[:4] for t, _ in nav}):
        seg = [v for t, v in nav if t[:4] == y]
        base = prev if prev is not None else seg[0]
        out[y] = seg[-1] / base - 1
        prev = seg[-1]
    return out


def rolling(nav, months):
    v = [x[1] for x in nav]
    return [(nav[i][0], (v[i + months] / v[i]) ** (12 / months) - 1) for i in range(len(v) - months)]


def main():
    BB.build()
    B.ROWS_FILE, B.KL_FILE, B.START = BB.ROWS, BB.KL, BB.START
    by_t, px = B.load()
    add_relative(by_t)
    inds = S.industries(dt.date.today(), True)
    v0 = B.VARIANTS["V0 三出（现行）"]
    navs, ntr = {}, {}
    for name, f in CANDS.items():
        nav, trades = B.run(by_t, px, inds, "V0 三出（现行）", v0, buy_ok=f, start=BB.START)
        navs[name] = [(t, v) for t, v, _ in nav]
        closed = [x for x in trades if x["out"] != "持有中"]
        ntr[name] = (sum(x["ret"] < -0.10 for x in closed) / max(1, len(closed)), len(closed))
        print(f"… {name}", file=sys.stderr, flush=True)
    cur = navs["现行：相对飞刀 −25% + EPS"]
    yc, r3c, r5c = yearly(cur), dict(rolling(cur, 36)), dict(rolling(cur, 60))

    print(f"## 买入过滤重新评估（{BB.START} ~ {cur[-1][0][:7]}，卖出规则 = 现行 V0，月末口径）\n")
    print("| 方案 | 全程年化 | 月末回撤 | 去掉 2009 年化 | 前段 07~16 | 后段 17~26 | 近 5 年年化 / 回撤 | 跑赢现行的年数 | 滚动 3 年跑赢现行 | 滚动 5 年跑赢现行 | 最差 5 年年化 | 买错率 |")
    print("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for name, nav in navs.items():
        y = yearly(nav)
        win_y = sum(y[k] > yc[k] + 1e-9 for k in y)
        r3, r5 = rolling(nav, 36), rolling(nav, 60)
        w3 = sum(v > r3c[t] + 1e-9 for t, v in r3) / len(r3)
        w5 = sum(v > r5c[t] + 1e-9 for t, v in r5) / len(r5)
        print(f"| {name} | {cagr(nav):+.1%} | {mdd(nav):.0%} | {cagr(nav, skip_year='2009'):+.1%} | "
              f"{cagr(nav, *HALVES[0]):+.1%} | {cagr(nav, *HALVES[1]):+.1%} | "
              f"{cagr(nav, '2021-09'):+.1%} / {mdd([x for x in nav if x[0][:7] >= '2021-09']):.0%} | "
              f"{win_y}/{len(y)} | {w3:.0%} | {w5:.0%} | "
              f"{min(v for _, v in r5):+.1%} | {ntr[name][0]:.0%} |")
    print("\n### 逐年（收益）\n")
    years = sorted(yc)
    show = ["原始（不加过滤）", "旧版：绝对飞刀 −25% + EPS ≤+70%", "现行：相对飞刀 −25% + EPS", "相对飞刀 −20% + EPS",
            "普跌暂停的绝对飞刀 −25% + EPS", "只 EPS ≤+70%"]
    print("| 年 | " + " | ".join(show) + " |")
    print("|---|" + "---|" * len(show))
    ys = {k: yearly(navs[k]) for k in show}
    for yy in years:
        print(f"| {yy} | " + " | ".join(f"{ys[k][yy]:+.1%}" for k in show) + " |")


if __name__ == "__main__":
    main()
