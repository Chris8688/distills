#!/usr/bin/env python3
"""作者 16% vs 我们 12~14%：差距从哪来（只是探索）。

在 backtest_exits 的月度框架上加两个旋钮，逐项看对收益的影响：
- 持仓只数 N（作者 2021~2024 实际只持 15~18 只，我们 30 只）；
- 检查频率：月度（现行回测）/ 每周 / 每日。月内用日线收盘价把月底的股息率、PE 按价格缩放
  （y_now = y × p_月底 / p_今天），「三出」与「四进」都按当天价格判断、当天收盘成交；业绩数据仍按月更新。
买入规则 = 现行（四进 + 业绩 + 相对飞刀 -25% + 利润暴增 ≤+70%），可选关掉两条过滤。
用法：python3 research/author_gap.py
"""
from __future__ import annotations

import bisect
import statistics as st
import sys
import os

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, ".."))

import backtest_exits as B  # noqa: E402
import buy_filters as BF  # noqa: E402
import screen as S  # noqa: E402

R2 = BF.REL["R2 相对飞刀 -25% + 利润暴增"]


def scaled(r, k):
    """把月底指标按价格比例 k = p_月底 / p_今天 缩放。"""
    if k == 1.0:
        return r
    x = dict(r)
    x["y"] = r["y"] * k
    x["pe"] = r["pe"] / k if r["pe"] else r["pe"]
    return x


def run(by_t, px, inds, days, n=30, step=0, buy_ok=R2, start="2018-06", end="2026-09"):
    """step=0 月度；step=5 每周；step=1 每日。"""
    months = sorted(t for t in by_t if start <= t[:7] <= end)
    cash, pos, nav, trades = 1.0, {}, [], []
    for mi, t in enumerate(months):
        rows = by_t[t]
        nxt = months[mi + 1] if mi + 1 < len(months) else t
        checks = [t]
        if step:
            i0, i1 = bisect.bisect_right(days, t), bisect.bisect_right(days, nxt) - 1   # 月底之后、下个月底之前
            checks += days[i0:i1:step]
        for di, day in enumerate(checks):
            for c, p in pos.items():
                pr = B.price(px, c, day)
                if pr:
                    p["last"] = pr
            eq = cash + sum(p["sh"] * p["last"] for p in pos.values())
            for c, p in list(pos.items()):
                r = rows.get(c)
                if di == 0:
                    why = "失去分红资格" if r is None else ("业绩" if B.earn_sell(r) else None)
                else:
                    why = None
                if not why and r is not None:
                    p0 = B.price(px, c, t)
                    if p0 and r["y"] * p0 / p["last"] <= 0.03:
                        why = "估值"
                if why:
                    cash += p["sh"] * p["last"] * (1 - B.COST)
                    trades.append({"code": c, "in": p["entry_t"], "out": day, "ret": p["last"] / p["entry_px"] - 1, "why": why})
                    del pos[c]
            free = n - len(pos)
            if free <= 0:
                if di == 0:
                    nav.append((t, cash + sum(p["sh"] * p["last"] for p in pos.values()), len(pos)))
                continue
            cnt: dict = {}
            for c in pos:
                cnt[inds.get(c, "?")] = cnt.get(inds.get(c, "?"), 0) + 1
            cap = max(1, round(n * 0.2))
            cands = []
            for c, r in rows.items():
                if c in pos:
                    continue
                p0, pr = B.price(px, c, t), B.price(px, c, day)
                if not p0 or not pr:
                    continue
                x = scaled(r, p0 / pr)
                if B.buyable(x, False) and (buy_ok is None or buy_ok(x)):
                    cands.append((-min(x["y"], 1 / x["pe"]), c, pr))
            cands.sort()
            target = eq / n
            for _, c, pr in cands:
                if free == 0 or cash < target * 0.5:
                    break
                ind = inds.get(c, "?")
                if cnt.get(ind, 0) >= cap:
                    continue
                amt = min(target, cash)
                pos[c] = {"sh": amt * (1 - B.COST) / pr, "entry_px": pr, "entry_t": day, "last": pr}
                cash -= amt
                cnt[ind] = cnt.get(ind, 0) + 1
                free -= 1
            if di == 0:                                 # 净值记在月底（月内交易计入下个月）
                nav.append((t, cash + sum(p["sh"] * p["last"] for p in pos.values()), len(pos)))
    for c, p in pos.items():
        trades.append({"code": c, "in": p["entry_t"], "out": "持有中", "ret": p["last"] / p["entry_px"] - 1, "why": "—"})
    return nav, trades


def yearly(nav):
    out = {}
    for i in range(1, len(nav)):
        out.setdefault(nav[i][0][:4], 1.0)
        out[nav[i][0][:4]] *= nav[i][1] / nav[i - 1][1]
    return {k: v - 1 for k, v in out.items()}


def main():
    by_t, px = B.load()
    by_t = BF.enrich_rel(BF.enrich(by_t))
    inds = S.industries(__import__("datetime").date.today(), True)
    days = px["600000"][0]
    combos = [("现行：30 只·月度·两条过滤", 30, 0, R2), ("30 只·月度·不加过滤", 30, 0, None),
              ("15 只·月度", 15, 0, R2), ("30 只·每周", 30, 5, R2), ("15 只·每周", 15, 5, R2),
              ("30 只·每日", 30, 1, R2), ("15 只·每日", 15, 1, R2)]
    print("| 方案 | 2018-06~ 年化 | 2019-12~ 年化 | 最大回撤 | 年换手(笔) | 17 起点平均年化 | " + " | ".join(str(y) for y in range(2018, 2027)) + " |")
    print("|---|---|---|---|---|---|" + "---|" * 9)
    for name, n, step, f in combos:
        nav, tr = run(by_t, px, inds, days, n, step, f)
        s = B.stats(nav, tr)
        sub = [x for x in nav if x[0][:7] >= "2019-12"]
        c2 = (sub[-1][1] / sub[0][1]) ** (12 / (len(sub) - 1)) - 1
        ms = []
        for s0 in BF.STARTS:
            nv, t2 = run(by_t, px, inds, days, n, step, f, start=s0)
            ms.append(B.stats(nv, t2)["cagr"])
        yr = yearly(nav)
        print(f"| {name} | {s['cagr']:+.1%} | {c2:+.1%} | {s['mdd']:.1%} | {s['trades_yr']:.0f} | {st.mean(ms):+.1%} | "
              + " | ".join(f"{yr.get(str(y), 0):+.0%}" for y in range(2018, 2027)) + " |", flush=True)


if __name__ == "__main__" and "--robust" not in sys.argv:
    main()


def main_robust():
    """两段独立窗口 × 持仓只数 × 频率；以及「15 只·每周」各笔交易的贡献。"""
    by_t, px = B.load()
    by_t = BF.enrich_rel(BF.enrich(by_t))
    inds = S.industries(__import__("datetime").date.today(), True)
    days = px["600000"][0]
    print("| 方案 | 前段年化（9 起点均值） | 后段年化（9 起点均值） | 全期 17 起点 | 最差起点 | 回撤中位 |\n|---|---|---|---|---|---|")
    for n in (10, 15, 20, 30):
        for step in (0, 5):
            row = []
            for wname, (starts, end) in BF.WINDOWS.items():
                row.append(st.mean(B.stats(*run(by_t, px, inds, days, n, step, R2, start=s0, end=end))["cagr"] for s0 in starts))
            full = [B.stats(*run(by_t, px, inds, days, n, step, R2, start=s0)) for s0 in BF.STARTS]
            print(f"| {n} 只·{'每周' if step else '月度'} | {row[0]:+.1%} | {row[1]:+.1%} | {st.mean(s['cagr'] for s in full):+.1%} | "
                  f"{min(s['cagr'] for s in full):+.1%} | {st.median(s['mdd'] for s in full):.1%} |", flush=True)


if __name__ == "__main__" and "--robust" in sys.argv:
    main_robust()
