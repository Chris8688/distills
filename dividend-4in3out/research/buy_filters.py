#!/usr/bin/env python3
"""目标：保留大涨股的前提下降低「买错」—— 买入端过滤的组合回测（只是探索）。

先诊断：V0（现行规则）的交易里，「买错」（单笔亏超 10%）和「大赢家」（涨超 50%）在**买入那一刻**有什么不同；
再把有区分度的特征做成买入过滤，跑组合回测，并分前后两段检验稳健性（两段都改善才算数）。

指标：年化 / 回撤；买错率 = 单笔亏超 10% 的交易占比；陷阱率 = 因业绩卖出且亏损的占比；
     大赢家 = 涨超 50% 的笔数与平均收益；大赢家保留 = V0 的大赢家在新规则下同期仍买到且也涨超 50% 的比例。
用法：python3 research/buy_filters.py [--diagnose]
"""
from __future__ import annotations

import statistics as st
import sys
import os

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, ".."))

import backtest_exits as B  # noqa: E402
import relative_valuation as RV  # noqa: E402
import screen as S  # noqa: E402

HALVES = [("2018-06", "2022-06"), ("2022-07", "2026-09")]


def enrich(by_t):
    """加入买入时可见的特征：上年分红相对再前一年（是否刚砍过 / 刚暴增）、分红率、可持续股息率。"""
    ps, _ = RV.fy_tables()
    for rows in by_t.values():
        for r in rows.values():
            d0, d1 = ps.get(r["fy"], {}).get(r["code"]), ps.get(r["fy"] - 1, {}).get(r["code"])
            r["dps_chg"] = (d0 / d1 - 1) if (d0 and d1) else None
            r["payout"] = r["y"] * r["pe"] if r["pe"] else None
            r["sus"] = min(r["y"], 1 / r["pe"]) if r["pe"] else None
    return by_t


FEATURES = {
    "股息率": lambda r: r["y"], "可持续股息率": lambda r: r["sus"], "PE": lambda r: r["pe"],
    "分红率": lambda r: r["payout"], "EPS 同比": lambda r: r["eps_yoy"], "过去一年股价": lambda r: r["p12"],
    "上年分红变化": lambda r: r["dps_chg"], "相对 PE": lambda r: r["rel_pe"], "股息率自身分位": lambda r: r["y_pct"],
    "过去 36 月 ≥6% 月数": lambda r: r["n6"],
}


def diagnose(trades):
    bad = [t for t in trades if t["ret"] < -0.10 and t.get("entry")]
    big = [t for t in trades if t["ret"] > 0.50 and t.get("entry")]
    mid = [t for t in trades if -0.10 <= t["ret"] <= 0.50 and t.get("entry")]
    print(f"\n## 诊断：V0 的 {len(trades)} 笔交易里，买错 {len(bad)}（亏超 10%）、大赢家 {len(big)}（涨超 50%）、其余 {len(mid)}\n")
    print("| 买入时特征（中位） | 买错 | 其余 | 大赢家 |\n|---|---|---|---|")
    for k, f in FEATURES.items():
        def med(L):
            v = [f(t["entry"]) for t in L if f(t["entry"]) is not None]
            return f"{st.median(v):.3g}" if v else "—"
        print(f"| {k} | {med(bad)} | {med(mid)} | {med(big)} |")


def metrics(nav, trades, base_big):
    s = B.stats(nav, trades)
    n = len(trades)
    bad = sum(t["ret"] < -0.10 for t in trades) / n
    trap = sum(t["why"] == "业绩" and t["ret"] < 0 for t in trades) / n
    big = [t for t in trades if t["ret"] > 0.5]
    keep = 0
    for b in base_big:
        if any(t["code"] == b["code"] and t["ret"] > 0.5 and t["in"] <= b["out"] and (t["out"] == "持有中" or t["out"] >= b["in"])
               for t in trades):
            keep += 1
    return s, bad, trap, big, keep


CYC = S.CYCLICAL_HINT
_IND = S.industries(__import__("datetime").date.today(), True)


def cyc(r):
    return any(k in _IND.get(r["code"], "") for k in CYC)


def spike(r, dps=1.0, eps=0.5):
    """峰值利润 / 峰值分红：上年分红比前一年翻倍以上，或 EPS 同比 > +50%。"""
    return (r["dps_chg"] is not None and r["dps_chg"] > dps) or (r["eps_yoy"] is not None and r["eps_yoy"] > eps)


FILTERS = {
    "F0 现行（不加过滤）": None,
    "F1 上年分红没被砍（≥前一年的 80%）": lambda r: r["dps_chg"] is None or r["dps_chg"] >= -0.2,
    "F2 不接飞刀（过去一年股价跌幅 <25%）": lambda r: r["p12"] is None or r["p12"] > -0.25,
    "F3 EPS 同比 ≥0（业绩没在下滑）": lambda r: r["eps_yoy"] is not None and r["eps_yoy"] >= 0,
    "F4 分红率 ≤80%": lambda r: r["payout"] is not None and r["payout"] <= 0.8,
    "F5 刚到 6% 的不买（≥6% 时要求过去 36 月里 ≥6 个月 ≥6%）": lambda r: r["y"] < 0.06 or r["n6"] >= 6,
    "F6 PE ≤15": lambda r: r["pe"] <= 15,
    "F7 F1 + F2": lambda r: (r["dps_chg"] is None or r["dps_chg"] >= -0.2) and (r["p12"] is None or r["p12"] > -0.25),
    "F8 F1 + F3": lambda r: (r["dps_chg"] is None or r["dps_chg"] >= -0.2) and r["eps_yoy"] is not None and r["eps_yoy"] >= 0,
    "F9 F2 + F3": lambda r: (r["p12"] is None or r["p12"] > -0.25) and r["eps_yoy"] is not None and r["eps_yoy"] >= 0,
    "G1 上年分红没翻倍（≤前一年 2 倍）": lambda r: r["dps_chg"] is None or r["dps_chg"] <= 1.0,
    "G2 EPS 同比 ≤ +50%（不在利润暴增期）": lambda r: r["eps_yoy"] is None or r["eps_yoy"] <= 0.5,
    "G3 G1 + G2（不买峰值利润/峰值分红）": lambda r: not spike(r),
    "G4 强周期行业不买峰值（周期股 + 峰值 → 不买）": lambda r: not (cyc(r) and spike(r)),
    "G5 强周期行业一律不买": lambda r: not cyc(r),
    "G6 G3 + F2（再加不接飞刀）": lambda r: not spike(r) and (r["p12"] is None or r["p12"] > -0.25),
    "G7 宽松峰值：分红 ≤3 倍 且 EPS ≤ +100%": lambda r: not spike(r, 2.0, 1.0),
    "G8 严格峰值：分红 ≤ +50% 且 EPS ≤ +30%": lambda r: not spike(r, 0.5, 0.3),
}


def main():
    by_t, px = B.load()
    by_t = enrich(by_t)
    inds = S.industries(__import__("datetime").date.today(), True)
    v0 = B.VARIANTS["V0 三出（现行）"]
    nav0, tr0 = B.run(by_t, px, inds, "V0", v0)
    base_big = [t for t in tr0 if t["ret"] > 0.5]
    if "--diagnose" in sys.argv:
        diagnose(tr0)
        return
    diagnose(tr0)
    print("\n## 买入过滤的组合回测（卖出规则 = 现行 V0；2018-06 ~ 2026-09）\n")
    print("| 方案 | 年化 | 回撤 | 前半段 | 后半段 | 交易笔数 | 买错率 | 陷阱率 | 大赢家 笔/均值 | V0 大赢家保留 |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for name, f in FILTERS.items():
        nav, tr = B.run(by_t, px, inds, "V0", v0, buy_ok=f)
        s, bad, trap, big, keep = metrics(nav, tr, base_big)
        halves = []
        for a, b in HALVES:
            n2, t2 = B.run(by_t, px, inds, "V0", v0, buy_ok=f, start=a, end=b)
            halves.append(B.stats(n2, t2)["cagr"])
        print(f"| {name} | {s['cagr']:+.1%} | {s['mdd']:.1%} | {halves[0]:+.1%} | {halves[1]:+.1%} | {len(tr)} | "
              f"{bad:.0%} | {trap:.0%} | {len(big)} / {st.mean(t['ret'] for t in big) if big else 0:+.0%} | {keep}/{len(base_big)} |")


if __name__ == "__main__" and not ({"--robust", "--windows", "--relknife"} & set(sys.argv)):
    main()


# ── 多起点稳健性：单一路径受「哪个月开始」影响很大（30 只，换几只就差 ±1pp）──────────────
STARTS = [f"{y}-{m:02d}" for y in range(2018, 2023) for m in (3, 6, 9, 12)
          if "2018-06" <= f"{y}-{m:02d}" <= "2022-06"]


def robust(by_t, px, inds, f, base_by_start):
    v0 = B.VARIANTS["V0 三出（现行）"]
    cagr, xs, bad, trap, big, keep, tot = [], [], [], [], [], 0, 0
    for s0 in STARTS:
        nav, tr = B.run(by_t, px, inds, "V0", v0, buy_ok=f, start=s0)
        s = B.stats(nav, tr)
        cagr.append(s["cagr"])
        xs.append(s["cagr"] - base_by_start[s0]["cagr"])
        n = len(tr)
        bad.append(sum(t["ret"] < -0.10 for t in tr) / n)
        trap.append(sum(t["why"] == "业绩" and t["ret"] < 0 for t in tr) / n)
        big.append(sum(t["ret"] > 0.5 for t in tr))
        for b in base_by_start[s0]["big"]:
            tot += 1
            keep += any(t["code"] == b["code"] and t["ret"] > 0.5 for t in tr)
    return {"cagr": st.mean(cagr), "win": sum(x > 0 for x in xs), "xs": st.mean(xs), "bad": st.mean(bad),
            "trap": st.mean(trap), "big": st.mean(big), "keep": keep / tot if tot else 0}


def knife(th):
    return lambda r: r["p12"] is None or r["p12"] > th


def epscap(th):
    return lambda r: r["eps_yoy"] is None or r["eps_yoy"] <= th


def both(k, e):
    return lambda r: knife(k)(r) and epscap(e)(r)


ROBUST2 = {"F0 现行": None}
for _k in (-0.20, -0.25, -0.30, -0.35):
    ROBUST2[f"不接飞刀 {_k:.0%}"] = knife(_k)
for _e in (0.6, 0.7, 0.8):
    ROBUST2[f"EPS ≤+{_e:.0%}"] = epscap(_e)
for _k in (-0.20, -0.25, -0.30):
    for _e in (0.6, 0.7, 0.8):
        ROBUST2[f"组合：飞刀 {_k:.0%} + EPS ≤+{_e:.0%}"] = both(_k, _e)

ROBUST = {
    "F0 现行": None,
    "F2 不接飞刀": FILTERS["F2 不接飞刀（过去一年股价跌幅 <25%）"],
    "F6 PE ≤15": FILTERS["F6 PE ≤15"],
    "F7 分红没砍 + 不接飞刀": FILTERS["F7 F1 + F2"],
    "G1 分红没翻倍": FILTERS["G1 上年分红没翻倍（≤前一年 2 倍）"],
    "G2 EPS 同比 ≤+50%": FILTERS["G2 EPS 同比 ≤ +50%（不在利润暴增期）"],
    "G2' EPS 同比 ≤+30%": lambda r: r["eps_yoy"] is None or r["eps_yoy"] <= 0.3,
    "G2'' EPS 同比 ≤+70%": lambda r: r["eps_yoy"] is None or r["eps_yoy"] <= 0.7,
    "G2''' EPS 同比 ≤+100%": lambda r: r["eps_yoy"] is None or r["eps_yoy"] <= 1.0,
    "G3 不买峰值（分红没翻倍 + EPS ≤+50%）": FILTERS["G3 G1 + G2（不买峰值利润/峰值分红）"],
    "G4 周期股不买峰值": FILTERS["G4 强周期行业不买峰值（周期股 + 峰值 → 不买）"],
    "G6 不买峰值 + 不接飞刀": FILTERS["G6 G3 + F2（再加不接飞刀）"],
    "H1 G2 + 不接飞刀": lambda r: (r["eps_yoy"] is None or r["eps_yoy"] <= 0.5) and (r["p12"] is None or r["p12"] > -0.25),
    "H2 G2 + PE ≤15": lambda r: (r["eps_yoy"] is None or r["eps_yoy"] <= 0.5) and r["pe"] <= 15,
}


def main_robust():
    by_t, px = B.load()
    by_t = enrich(by_t)
    inds = S.industries(__import__("datetime").date.today(), True)
    v0 = B.VARIANTS["V0 三出（现行）"]
    base = {}
    for s0 in STARTS:
        nav, tr = B.run(by_t, px, inds, "V0", v0, start=s0)
        base[s0] = {"cagr": B.stats(nav, tr)["cagr"], "big": [t for t in tr if t["ret"] > 0.5]}
    print(f"\n## 多起点稳健性（{len(STARTS)} 个起点：{STARTS[0]} ~ {STARTS[-1]} 每季度一个，都跑到 2026-09）\n")
    print("| 方案 | 平均年化 | 比现行多 | 跑赢现行的起点 | 买错率 | 陷阱率 | 大赢家笔数 | 现行大赢家保留 |")
    print("|---|---|---|---|---|---|---|---|")
    for name, f in (ROBUST2 if "--grid" in sys.argv else ROBUST).items():
        r = robust(by_t, px, inds, f, base)
        print(f"| {name} | {r['cagr']:+.1%} | {r['xs']:+.2%} | {r['win']}/{len(STARTS)} | {r['bad']:.1%} | {r['trap']:.1%} | "
              f"{r['big']:.1f} | {r['keep']:.0%} |")


if __name__ == "__main__" and "--robust" in sys.argv and "--relknife" not in sys.argv:
    main_robust()


# ── 两段独立窗口（各自多起点，互不重叠）：阈值是在全期上挑的，要看两段是否都成立 ─────────────
WINDOWS = {"前段 2018-06~2022-06": ([f"{y}-{m:02d}" for y in (2018, 2019, 2020) for m in (3, 6, 9, 12)
                                    if "2018-06" <= f"{y}-{m:02d}" <= "2020-06"], "2022-06"),
           "后段 2022-07~2026-09": ([f"{y}-{m:02d}" for y in (2022, 2023, 2024) for m in (3, 6, 9, 12)
                                    if "2022-07" <= f"{y}-{m:02d}" <= "2024-09"], "2026-09")}
FINAL = {
    "F0 现行": None,
    "A 不接飞刀 -25%": knife(-0.25),
    "B EPS ≤+70%": epscap(0.7),
    "C 飞刀 -25% + EPS ≤+70%": both(-0.25, 0.7),
    "D 飞刀 -30% + EPS ≤+70%": both(-0.30, 0.7),
}


def main_windows():
    by_t, px = B.load()
    by_t = enrich(by_t)
    inds = S.industries(__import__("datetime").date.today(), True)
    v0 = B.VARIANTS["V0 三出（现行）"]
    for wname, (starts, end) in WINDOWS.items():
        base = {}
        for s0 in starts:
            nav, tr = B.run(by_t, px, inds, "V0", v0, start=s0, end=end)
            base[s0] = {"cagr": B.stats(nav, tr)["cagr"], "big": [t for t in tr if t["ret"] > 0.5]}
        print(f"\n### {wname}（{len(starts)} 个起点）\n")
        print("| 方案 | 平均年化 | 比现行多 | 跑赢现行的起点 | 买错率 | 陷阱率 | 大赢家笔数 |")
        print("|---|---|---|---|---|---|---|")
        for name, f in FINAL.items():
            cg, xs, bad, trap, big = [], [], [], [], []
            for s0 in starts:
                nav, tr = B.run(by_t, px, inds, "V0", v0, buy_ok=f, start=s0, end=end)
                c = B.stats(nav, tr)["cagr"]
                cg.append(c); xs.append(c - base[s0]["cagr"])
                n = len(tr)
                bad.append(sum(t["ret"] < -0.10 for t in tr) / n)
                trap.append(sum(t["why"] == "业绩" and t["ret"] < 0 for t in tr) / n)
                big.append(sum(t["ret"] > 0.5 for t in tr))
            print(f"| {name} | {st.mean(cg):+.1%} | {st.mean(xs):+.2%} | {sum(x > 0 for x in xs)}/{len(starts)} | "
                  f"{st.mean(bad):.1%} | {st.mean(trap):.1%} | {st.mean(big):.1f} |")


if __name__ == "__main__" and "--windows" in sys.argv:
    main_windows()


# ── 相对飞刀（2026-10-05）：飞刀按「比当月全部长期分红股的一年涨跌中位数多跌多少」算 ─────────
#   绝对飞刀会把市场整体暴跌后的反弹（2018-12、2020-03）一起挡掉；相对版只挡「自己出问题」的。
def enrich_rel(by_t):
    for rows in by_t.values():
        ps = [r["p12"] for r in rows.values() if r.get("p12") is not None]
        med = st.median(ps) if ps else 0.0
        for r in rows.values():
            r["p12x"] = (r["p12"] - med) if r.get("p12") is not None else None
    return by_t


def rknife(th):
    return lambda r: r.get("p12x") is None or r["p12x"] > th


REL = {
    "现行 V0（无过滤）": None,
    "B 只用利润暴增（EPS ≤+70%）": epscap(0.7),
    "C 已落地：绝对飞刀 -25% + 利润暴增": both(-0.25, 0.7),
    "R1 相对飞刀 -20% + 利润暴增": lambda r: rknife(-0.20)(r) and epscap(0.7)(r),
    "R2 相对飞刀 -25% + 利润暴增": lambda r: rknife(-0.25)(r) and epscap(0.7)(r),
    "R3 相对飞刀 -30% + 利润暴增": lambda r: rknife(-0.30)(r) and epscap(0.7)(r),
    "R4 绝对 -25% 且 相对 -15% + 利润暴增": lambda r: (knife(-0.25)(r) or rknife(-0.15)(r)) and epscap(0.7)(r),
}


def main_rel():
    by_t, px = B.load()
    by_t = enrich_rel(enrich(by_t))
    inds = S.industries(__import__("datetime").date.today(), True)
    v0 = B.VARIANTS["V0 三出（现行）"]
    base = {}
    for s0 in STARTS:
        nav, tr = B.run(by_t, px, inds, "V0", v0, start=s0)
        base[s0] = {"cagr": B.stats(nav, tr)["cagr"], "big": [t for t in tr if t["ret"] > 0.5], "tr": tr}
    print(f"\n## 相对飞刀 vs 绝对飞刀（{len(STARTS)} 个起点）\n")
    print("| 方案 | 比现行年化 | 跑赢现行的起点 | 买错率 | 陷阱率 | 大赢家笔数 | 挡掉笔数 | 错过大涨 | 躲过买错 |")
    print("|---|---|---|---|---|---|---|---|---|")
    uniq = {}
    for s0 in STARTS:
        for t in base[s0]["tr"]:
            uniq.setdefault((t["code"], t["in"]), t)
    for name, f in REL.items():
        r = robust(by_t, px, inds, f, base)
        blk = [t for t in uniq.values() if f and t.get("entry") and not f(t["entry"])]
        print(f"| {name} | {r['xs']:+.2%} | {r['win']}/{len(STARTS)} | {r['bad']:.1%} | {r['trap']:.1%} | {r['big']:.1f} | "
              f"{len(blk)} | {sum(t['ret'] > 0.5 for t in blk)} | {sum(t['ret'] < -0.1 for t in blk)} |")
    for wname, (starts, end) in WINDOWS.items():
        bw = {}
        for s0 in starts:
            nav, tr = B.run(by_t, px, inds, "V0", v0, start=s0, end=end)
            bw[s0] = B.stats(nav, tr)["cagr"]
        print(f"\n### {wname}（{len(starts)} 个起点）\n")
        print("| 方案 | 比现行年化 | 跑赢起点 | 买错率 | 陷阱率 | 大赢家笔数 |\n|---|---|---|---|---|---|")
        for name, f in REL.items():
            xs, bad, trap, big = [], [], [], []
            for s0 in starts:
                nav, tr = B.run(by_t, px, inds, "V0", v0, buy_ok=f, start=s0, end=end)
                xs.append(B.stats(nav, tr)["cagr"] - bw[s0])
                n = len(tr)
                bad.append(sum(t["ret"] < -0.10 for t in tr) / n)
                trap.append(sum(t["why"] == "业绩" and t["ret"] < 0 for t in tr) / n)
                big.append(sum(t["ret"] > 0.5 for t in tr))
            print(f"| {name} | {st.mean(xs):+.2%} | {sum(x > 0 for x in xs)}/{len(starts)} | {st.mean(bad):.1%} | "
                  f"{st.mean(trap):.1%} | {st.mean(big):.1f} |")


if __name__ == "__main__" and "--relknife" in sys.argv:
    main_rel()
