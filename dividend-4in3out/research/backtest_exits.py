#!/usr/bin/env python3
"""组合回测：同一套买入规则，不同退出规则（只是探索，不改策略）。

数据 = research/relative_valuation.py 落盘的月度指标（先跑它），价格直接取缓存的前复权日线（含分红再投入）。
组合：30 只等权建仓、不再平衡，单行业 ≤6 只（行业用今天的东财分类，近似），月底调仓、按月底收盘价成交，
单边成本 0.15%（佣金 + 印花税 + 滑点的粗略合计）。2018-06 起（相对估值需要 3 年历史）到 2026-09。

买入（所有方案相同，近似纸面账户 observed 档）：股息率 ≥4%、连续分红 ≥4 年、PE ≤25（= 保底分红率 ≤100%）、
  每股收益 TTM 同比 ≥ -10%（扣非的近似）；按可持续股息率 min(股息率, 1/PE) 排序。
业绩卖出（所有方案相同）：EPS 同比 < -30%，或 < -10% 且 股息率×(1+同比) < 4%；失去「连续分红」资格。
不同的只是「估值卖出」：见 VARIANTS。

近似与局限：EPS 用基本每股收益（不是扣非）；没有「分红>利润连续 2 年不买」；行业是今天的分类；退市股不在样本。
用法：python3 research/backtest_exits.py
"""
from __future__ import annotations

import bisect
import json
import math
import os
import statistics as st
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

import screen as S  # noqa: E402
from lib import prices as PX  # noqa: E402

N, IND_CAP, COST = 30, 6, 0.0015
START, END = "2018-06", "2026-09"


def price_driven(r):
    return r.get("p12") is not None and r["p12"] > 0.20


VARIANTS = {
    "V0 三出（现行）": lambda r: r["y"] <= 0.03,
    "V1 三出 + 相对PE≥1.25且股价驱动": lambda r: r["y"] <= 0.03 or (r["rel_pe"] and r["rel_pe"] >= 1.25 and price_driven(r)),
    "V2 三出 + 股息率≤4%且处自身最低20%": lambda r: r["y"] <= 0.03 or (r["y"] <= 0.04 and r["y_pct"] is not None and r["y_pct"] <= 0.2),
    "V3 三出 + 相对PE≥1.5": lambda r: r["y"] <= 0.03 or (r["rel_pe"] and r["rel_pe"] >= 1.5),
    "V4 只用相对估值（去掉三出，留 2% 兜底）": lambda r: r["y"] <= 0.02 or (r["rel_pe"] and r["rel_pe"] >= 1.25 and price_driven(r))
                                              or (r["y"] <= 0.04 and r["y_pct"] is not None and r["y_pct"] <= 0.2),
    "V5 三出 + 相对PE≥1.5 + 股价驱动相对PE≥1.25": lambda r: r["y"] <= 0.03 or (r["rel_pe"] and r["rel_pe"] >= 1.5)
                                                   or (r["rel_pe"] and r["rel_pe"] >= 1.25 and price_driven(r)),
}
VARIANTS["V6 三出（同 V0），只在买入时排除相对PE≥1.5"] = VARIANTS["V0 三出（现行）"]
VARIANTS["V7 三出，买入排除相对PE≥1.25"] = VARIANTS["V0 三出（现行）"]
BUY_EXCLUDE_REL15 = {"V5 三出 + 相对PE≥1.5 + 股价驱动相对PE≥1.25", "V6 三出（同 V0），只在买入时排除相对PE≥1.5"}
BUY_EXCLUDE_REL125 = {"V7 三出，买入排除相对PE≥1.25"}


def load():
    rows = json.load(open(S._cpath("research_relval_rows.json")))
    by_t: dict = {}
    by_c: dict = {}
    for r in rows:
        by_t.setdefault(r["t"], {})[r["code"]] = r
        by_c.setdefault(r["code"], {})[r["t"][:7]] = r
    for c, m in by_c.items():                       # 过去 12 个月股价涨幅、EPS TTM 同比
        for ym, r in m.items():
            y, mo = int(ym[:4]), int(ym[5:])
            back = f"{y - 1}-{mo:02d}"
            p = m.get(back)
            r["p12"] = (r["pf"] / p["pf"] - 1) if p else None
            e1 = r["pf"] / r["pe"] if r["pe"] else None
            e0 = p["pf"] / p["pe"] if (p and p["pe"]) else None
            r["eps_yoy"] = (e1 / e0 - 1) if (e1 and e0 and e0 > 0) else None
    kl = json.load(open(S._cpath("research_klines_forward_3000.json")))
    px = {}
    for sym, bars in kl.items():
        code = sym.split(".")[0]
        px[code] = ([b["date"] for b in bars], [b["close"] for b in bars])
    return by_t, px


def price(px, code, day):
    if code not in px:
        return None
    d, c = px[code]
    i = bisect.bisect_right(d, day) - 1
    return c[i] if i >= 0 else None


def earn_sell(r):
    g = r.get("eps_yoy")
    if g is None:
        return False
    return g < -0.30 or (g < -0.10 and r["y"] * (1 + g) < 0.04)


def buyable(r, excl15, excl125=False):
    g = r.get("eps_yoy")
    if r["y"] < 0.04 or not r["pe"] or r["pe"] > 25 or g is None or g < -0.10:
        return False
    if excl15 and r["rel_pe"] and r["rel_pe"] >= 1.5:
        return False
    if excl125 and r["rel_pe"] and r["rel_pe"] >= 1.25:
        return False
    return not earn_sell(r)


def run(by_t, px, inds, name, sell_val, buy_ok=None, start=None, end=None):
    """buy_ok：额外的买入过滤（research/buy_filters.py 用）；start/end：子区间回测。"""
    months = sorted(t for t in by_t if (start or START) <= t[:7] <= (end or END))
    cash, pos, nav, trades = 1.0, {}, [], []        # pos: code → {sh, entry_px, entry_t, peak}
    excl15 = name in BUY_EXCLUDE_REL15
    excl125 = name in BUY_EXCLUDE_REL125
    for t in months:
        rows = by_t[t]
        for c, p in list(pos.items()):              # 估值
            pr = price(px, c, t)
            if pr:
                p["last"] = pr
        eq = cash + sum(p["sh"] * p["last"] for p in pos.values())
        for c, p in list(pos.items()):              # 卖出
            r = rows.get(c)
            why = "失去分红资格" if r is None else ("业绩" if earn_sell(r) else ("估值" if sell_val(r) else None))
            if why:
                val = p["sh"] * p["last"]
                cash += val * (1 - COST)
                trades.append({"code": c, "in": p["entry_t"], "out": t, "ret": p["last"] / p["entry_px"] - 1, "why": why,
                               "entry": p.get("row")})
                del pos[c]
        free = N - len(pos)                         # 买入：不出则不进
        if free > 0:
            cnt: dict = {}
            for c in pos:
                cnt[inds.get(c, "?")] = cnt.get(inds.get(c, "?"), 0) + 1
            cands = sorted((r for c, r in rows.items() if c not in pos and buyable(r, excl15, excl125)
                            and (buy_ok is None or buy_ok(r))),
                           key=lambda r: -min(r["y"], 1 / r["pe"]))
            target = eq / N
            for r in cands:
                if free == 0 or cash < target * 0.5:
                    break
                ind = inds.get(r["code"], "?")
                if cnt.get(ind, 0) >= IND_CAP:
                    continue
                pr = price(px, r["code"], t)
                if not pr:
                    continue
                amt = min(target, cash)
                pos[r["code"]] = {"sh": amt * (1 - COST) / pr, "entry_px": pr, "entry_t": t, "last": pr, "row": r}
                cash -= amt
                cnt[ind] = cnt.get(ind, 0) + 1
                free -= 1
        nav.append((t, cash + sum(p["sh"] * p["last"] for p in pos.values()), len(pos)))
    for c, p in pos.items():                        # 期末未平仓按市值记
        trades.append({"code": c, "in": p["entry_t"], "out": "持有中", "ret": p["last"] / p["entry_px"] - 1, "why": "—",
                       "entry": p.get("row")})
    return nav, trades


def stats(nav, trades):
    v = [x[1] for x in nav]
    yrs = len(v) / 12
    cagr = v[-1] ** (1 / yrs) - 1
    peak, mdd = v[0], 0.0
    for x in v:
        peak = max(peak, x)
        mdd = min(mdd, x / peak - 1)
    mr = [v[i] / v[i - 1] - 1 for i in range(1, len(v))]
    vol = st.pstdev(mr) * math.sqrt(12)
    closed = [t for t in trades if t["out"] != "持有中"]
    rets = sorted((t["ret"] for t in trades), reverse=True)
    gains = sum(x for x in rets if x > 0)
    top = sum(x for x in rets[:max(1, len(rets) // 10)] if x > 0)
    hold = [((int(t["out"][:4]) - int(t["in"][:4])) * 12 + int(t["out"][5:7]) - int(t["in"][5:7])) for t in closed]
    yearly = {}
    for i in range(1, len(nav)):
        yearly.setdefault(nav[i][0][:4], 1.0)
        yearly[nav[i][0][:4]] *= nav[i][1] / nav[i - 1][1]
    return {"cagr": cagr, "mdd": mdd, "vol": vol, "end": v[-1], "trades_yr": len(closed) / yrs,
            "hold_m": st.median(hold) if hold else 0, "big": sum(x > 0.5 for x in rets),
            "top10": top / gains if gains else 0, "why": {k: sum(t["why"] == k for t in closed) for k in ("估值", "业绩", "失去分红资格")},
            "yearly": {k: v - 1 for k, v in yearly.items()}, "avg_pos": st.mean(x[2] for x in nav)}


def main():
    by_t, px = load()
    inds = S.industries(__import__("datetime").date.today(), True)
    res = {}
    for name, f in VARIANTS.items():
        nav, trades = run(by_t, px, inds, name, f)
        res[name] = (stats(nav, trades), nav, trades)
        print(f"… {name}", file=sys.stderr, flush=True)
    # 等权全样本基准（每月全部长期分红股等权）
    months = sorted(t for t in by_t if START <= t[:7] <= END)
    eqw = [1.0]
    for a, b in zip(months, months[1:]):
        rs = [price(px, c, b) / price(px, c, a) - 1 for c in by_t[a] if price(px, c, a) and price(px, c, b)]
        eqw.append(eqw[-1] * (1 + st.mean(rs)))
    yrs = len(eqw) / 12
    print(f"\n## 组合回测 {months[0][:7]} ~ {months[-1][:7]}（{yrs:.1f} 年，30 只等权，单边成本 0.15%）\n")
    print(f"基准：长期分红股全样本等权（月度再平衡）年化 {eqw[-1] ** (1 / yrs) - 1:+.1%}\n")
    print("| 方案 | 年化 | 最大回撤 | 波动 | 期末净值 | 年换手(笔) | 持有月数中位 | 涨超50%的笔数 | 前10%交易占盈利 | 卖出原因 估值/业绩/失格 |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for name, (s, _, _) in res.items():
        w = s["why"]
        print(f"| {name} | {s['cagr']:+.1%} | {s['mdd']:.1%} | {s['vol']:.1%} | {s['end']:.2f} | {s['trades_yr']:.0f} | "
              f"{s['hold_m']:.0f} | {s['big']} | {s['top10']:.0%} | {w['估值']}/{w['业绩']}/{w['失去分红资格']} |")
    years = sorted(next(iter(res.values()))[0]["yearly"])
    print("\n### 分年收益\n")
    print("| 方案 | " + " | ".join(years) + " |")
    print("|---|" + "---|" * len(years))
    for name, (s, _, _) in res.items():
        print(f"| {name.split(' ')[0]} | " + " | ".join(f"{s['yearly'][y]:+.0%}" for y in years) + " |")
    # 大赢家被提前卖掉的代价：V0 里涨超 50% 的交易，在 V1 里同一只票是不是更早卖、少赚多少
    base = [t for t in res["V0 三出（现行）"][2] if t["ret"] > 0.5]
    print(f"\n### V0 的大赢家（涨超 50%，{len(base)} 笔）在其他方案里的结果\n")
    print("| 方案 | 同一只票同期也买到 | 平均收益（V0 → 该方案） |")
    print("|---|---|---|")
    for name, (_, _, tr) in res.items():
        if name.startswith("V0"):
            continue
        got, pairs = 0, []
        for b in base:
            m = [t for t in tr if t["code"] == b["code"] and t["in"] <= b["out"] and (t["out"] == "持有中" or t["out"] >= b["in"])]
            if m:
                got += 1
                pairs.append((b["ret"], max(m, key=lambda t: t["ret"])["ret"]))
        if pairs:
            print(f"| {name} | {got}/{len(base)} | {st.mean(p[0] for p in pairs):+.0%} → {st.mean(p[1] for p in pairs):+.0%} |")


if __name__ == "__main__":
    main()
