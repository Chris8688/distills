#!/usr/bin/env python3
"""四进三出拉长到 2007-06 起：补 2008 熊市与 2015 股灾的空白（只是检验，不改策略）。

数据与口径同 backtest_exits.py（月底调仓、按月底收盘成交、单边 0.15%、30 只等权、单行业 ≤6 只），只把起点拉长：
  · 分红表从 FY2003 起（2008 年初判断「连续分红 ≥4 年」需要 FY2003~2006）
  · 样本股 = 2007 年起任一年满足连续分红 ≥4 年、今天仍能取到行情的 A 股（→ 幸存者偏差比 2018 起更大）
  · 日线取 5200 根（约 2005 年起）；每股收益历史来自新浪财务摘要（老股票 1990 年代起）
规则 = 现行：V0（股息率 ≤3% 卖 + 业绩卖出）+ 买入过滤「相对飞刀 −25%」（过去一年涨跌 − 当月全样本中位数 ≤ −25% 不买，
2026-10-05 起）+「EPS 同比 ≤ +70%」；对照：旧版（绝对飞刀 −25%，2026-10-05 前）与原始（不加过滤）。
组合按 30 只等权回放（纸面账户 2026-10-06 起改为 15 只，见 docs/06；只数对熊市结论影响见 docs/07）。
⚠️ 回撤按**月末净值**算，比日频浅；对照的 510880 同时给月末与日频两种口径。

用法：python3 research/backtest_bear.py           # 首次要抓数（分红 32 期、日线、EPS），约 10~30 分钟
"""
from __future__ import annotations

import datetime as dt
import json
import os
import statistics as st
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, ".."))

import backtest_exits as B  # noqa: E402
import buy_filters as F     # noqa: E402
import relative_valuation as RV  # noqa: E402
import screen as S  # noqa: E402

COUNT = 5200
ROWS = "research_relval_rows_2007.json"
KL = f"research_klines_forward_{COUNT}.json"
START = "2007-06"
SEGMENTS = [("2007-06", "2008-12", "2008 熊市（含 2007 下半年）"),
            ("2007-10", "2008-10", "2007-10 顶 → 2008-10 底"),
            ("2015-06", "2016-02", "2015 股灾 + 2016 熔断"),
            ("2018-01", "2018-12", "2018 熊市"),
            ("2007-06", "2018-05", "2018-06 以前（原报告未覆盖）"),
            ("2018-06", "2026-09", "2018-06 以后（原报告区间）"),
            ("2007-06", "2026-09", "全程")]


def add_relative(by_t):
    """r['p12_rel'] = 个股过去 12 月涨跌 − 当月全样本中位数；r['mkt12'] = 当月中位数（screen.py KNIFE_REL 的回测口径）。"""
    for rows in by_t.values():
        ps = sorted(r["p12"] for r in rows.values() if r.get("p12") is not None)
        med = st.median(ps) if ps else None
        for r in rows.values():
            r["mkt12"] = med
            r["p12_rel"] = (r["p12"] - med) if (r.get("p12") is not None and med is not None) else None


def knife_rel(th):
    return lambda r: r["p12_rel"] is None or r["p12_rel"] > th


def eps_cap(th):
    return lambda r: r["eps_yoy"] is None or r["eps_yoy"] <= th


CURRENT = "现行（相对飞刀 −25% + EPS ≤+70%）"
OLD = "旧版（绝对飞刀 −25% + EPS ≤+70%）"


def build():
    if os.path.exists(S._cpath(ROWS)):
        return
    RV.FY0, RV.COUNT, RV.UNIV_FY0, RV.ROWS_FILE, RV.REPORT = 2003, COUNT, 2007, ROWS, False
    RV.main()


def seg_stats(nav, a, b):
    seg = [x for x in nav if a <= x[0][:7] <= b]
    prev = [x for x in nav if x[0][:7] < a]
    base = prev[-1][1] if prev else seg[0][1]
    v = [base] + [x[1] for x in seg]
    peak, mdd = v[0], 0.0
    for x in v:
        peak = max(peak, x)
        mdd = min(mdd, x / peak - 1)
    yrs = max(len(seg), 1) / 12
    tot = v[-1] / base
    return tot - 1, (tot ** (1 / yrs) - 1) if yrs >= 1 else None, mdd


def etf_510880():
    """510880 前复权：(月末净值, 日频净值)。从红利低波蒸馏的缓存读，没有就现抓。"""
    p = os.path.expanduser("~/.cache/distill/dividend-lowvol-ma/510880_forward.json")
    if os.path.exists(p):
        bars = json.load(open(p))
    else:
        from lib import prices as PX
        bars = PX._klines(["510880.SH"], COUNT, "forward")["510880.SH"]
    daily = [(b["date"], b["close"]) for b in bars]
    monthly = {}
    for d, c in daily:
        monthly[d[:7]] = (d, c)
    return [monthly[k] for k in sorted(monthly)], daily


def daily_mdd(daily, a, b):
    v = [c for d, c in daily if a <= d[:7] <= b]
    peak, mdd = v[0], 0.0
    for x in v:
        peak = max(peak, x)
        mdd = min(mdd, x / peak - 1)
    return mdd


def main():
    build()
    B.ROWS_FILE, B.KL_FILE, B.START = ROWS, KL, START
    by_t, px = B.load()
    add_relative(by_t)
    inds = S.industries(dt.date.today(), True)
    v0 = B.VARIANTS["V0 三出（现行）"]
    rules = {CURRENT: lambda r: knife_rel(-0.25)(r) and eps_cap(0.7)(r),
             OLD: F.both(-0.25, 0.7), "原始（不加过滤）": None}
    navs = {}
    for name, f in rules.items():
        nav, trades = B.run(by_t, px, inds, "V0 三出（现行）", v0, buy_ok=f, start=START)
        navs[name] = [(t, v) for t, v, _ in nav]
        navs[name + " 持仓数"] = [(t, n) for t, _, n in nav]
    months = sorted(t for t in by_t if START <= t[:7] <= B.END)
    eqw = [(months[0], 1.0)]
    for a, b in zip(months, months[1:]):
        rs = [B.price(px, c, b) / B.price(px, c, a) - 1 for c in by_t[a] if B.price(px, c, a) and B.price(px, c, b)]
        eqw.append((b, eqw[-1][1] * (1 + st.mean(rs))))
    etf_m, etf_d = etf_510880()
    cols = {k: v for k, v in navs.items() if not k.endswith("持仓数")}
    cols["长期分红股等权"] = eqw
    cols["510880 上证红利（月末）"] = etf_m

    n_univ = {t[:4]: len(by_t[t]) for t in months}
    print(f"## 四进三出 2007-06 ~ {months[-1][:7]}（月末调仓，30 只等权，单边 0.15%，回撤为月末口径）\n")
    print("每年 6 月样本股数（满足连续分红 ≥4 年、今天仍有行情）：" +
          "，".join(f"{y} {n_univ[y]}" for y in sorted(n_univ) if y in {t[:4] for t in months if t[5:7] == '06'}))
    print("\n### 分段（总收益 / 年化 / 区间内最大回撤）\n")
    print("| 区间 | " + " | ".join(cols) + " | 510880 日频回撤 |")
    print("|---|" + "---|" * (len(cols) + 1))
    for a, b, lab in SEGMENTS:
        cells = []
        for nav in cols.values():
            tot, cagr, mdd = seg_stats(nav, a, b)
            cells.append(f"{tot:+.1%} / {('%+.1f%%' % (cagr * 100)) if cagr is not None else '—'} / {mdd:.0%}")
        print(f"| {lab}（{a}~{b}） | " + " | ".join(cells) + f" | {daily_mdd(etf_d, a, b):.0%} |")
    print("\n### 逐年\n")
    years = sorted({t[:4] for t in months})
    print("| 年 | " + " | ".join(cols) + " | 现行持仓数（年末） |")
    print("|---|" + "---|" * (len(cols) + 1))
    hold = dict(navs[CURRENT + " 持仓数"])
    for y in years:
        cells = []
        for nav in cols.values():
            tot, _, mdd = seg_stats(nav, f"{y}-01", f"{y}-12")
            cells.append(f"{tot:+.1%} / {mdd:.0%}")
        last = max(t for t in hold if t[:4] == y)
        print(f"| {y} | " + " | ".join(cells) + f" | {hold[last]} |")


if __name__ == "__main__":
    main()
