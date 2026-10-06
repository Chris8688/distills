#!/usr/bin/env python3
"""相对估值探索：自身历史估值能不能比「四进三出」的绝对股息率更好地判断买卖点？（只是探索，不改策略）

三个问题：
  Q1 股息率常年在 6% 是不是有问题？—— 现在股息率 ≥6% 的，按「过去 36 个月里有几个月 ≥6%」分新/中/老，比之后一年。
  Q2 PE 从自身常态（如 20）升到 25/30 是不是最佳退出点？—— 相对 PE（PE ÷ 自身 5 年中位）≥1.25/1.5、
     股息率处在自身 5 年最低 20% 分位、以及「常态 PE 15~25 的票 PE 升破 25/30」，各自首次触发后一年的超额。
  Q3 四进三出的绝对股息率能不能达到同样效果？—— 与上面各信号对比命中率、重合度。

口径：
  · 月度网格：每月最后一个交易日 t；样本 = 当年连续分红 ≥4 年的 A 股（今天仍能取到行情 → 有幸存者偏差）。
  · 防未来函数：年报 FY 在 FY+1 年 5 月起可用；季报 Q1/H1/Q3 分别在 5/1、9/1、11/1 起可用。
  · 送转口径：每股收益 / 每股分红 × 当期复权因子（前复权价 / 不复权价），再除以 t 的前复权价。
    送转不跳变；残余偏差是两次报告之间派发的股息（≤ 一年股息率），全程一致。
  · 收益 = 前复权价 t+12 月 / t - 1（含分红再投入）；超额 = 减去当月全样本中位数。
用法：python3 research/relative_valuation.py
"""
from __future__ import annotations

import bisect
import datetime as dt
import json
import os
import statistics as st
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

import screen as S  # noqa: E402
from lib import prices as PX  # noqa: E402
from yield_trap import klines_cached  # noqa: E402

TODAY = dt.date.today()
FY0, FY1 = 2011, TODAY.year - 1
COUNT = 3000
UNIV_FY0 = 2015                              # 样本股：这一年起任一年满足连续分红 ≥4 年
ROWS_FILE = "research_relval_rows.json"      # 月度指标落盘（backtest_exits 读它）
REPORT = True                                # research/backtest_bear.py 只建数据、不出报告


# ── 数据 ─────────────────────────────────────────────────────────────────────
def fy_tables():
    ps, tot = {}, {}
    for fy in range(FY0, FY1 + 1):
        ps[fy], tot[fy] = {}, {}
        for md in ("1231", "0630", "0930", "0331"):
            for c, rows in S._fhps_period(f"{fy}{md}", TODAY, False).items():
                for r in rows:
                    ps[fy][c] = ps[fy].get(c, 0.0) + r[2] / 10.0
                    tot[fy][c] = tot[fy].get(c, 0.0) + r[0]
    return ps, tot


def eps_history(codes: list[str]) -> dict:
    """{code: {报告期: 基本每股收益(累计)}}，新浪财务摘要；缓存 30 天（历史不变）。"""
    p = S._cpath("research_eps.json")
    have = json.load(open(p)) if os.path.exists(p) and S._fresh(p, 24 * 30) else {}
    need = [c for c in codes if c not in have]

    def one(c):
        import akshare as ak
        for att in range(3):
            try:
                with S._quiet_ak():
                    d = ak.stock_financial_abstract(symbol=c)
                periods = [x for x in d.columns if str(x).isdigit() and len(str(x)) == 8]
                row = d[d["指标"] == "基本每股收益"].iloc[0]
                return {q: float(row[q]) for q in periods if row[q] == row[q]}
            except Exception:      # noqa: BLE001
                time.sleep(2 * (att + 1))
        return None

    if need:
        print(f"… 每股收益历史 {len(need)} 只（新浪，4 并发）", file=sys.stderr, flush=True)
        with ThreadPoolExecutor(4) as ex:
            futs = {ex.submit(one, c): c for c in need}
            for i, f in enumerate(as_completed(futs), 1):
                r = f.result()
                if r:
                    have[futs[f]] = r
                if i % 200 == 0:
                    print(f"    {i}/{len(need)}", file=sys.stderr, flush=True)
                    json.dump(have, open(p, "w"))
        json.dump(have, open(p, "w"))
    return have


def avail_date(period: str) -> str:
    y, md = int(period[:4]), period[4:]
    return {"1231": f"{y + 1}-05-01", "0331": f"{y}-05-01", "0630": f"{y}-09-01", "0930": f"{y}-11-01"}[md]


class Series:
    """一只股票的日线（不复权 + 前复权），按日期查价与复权因子。"""
    def __init__(self, none: list[dict], fwd: list[dict]):
        fm = {b["date"]: b["close"] for b in fwd}
        self.d = [b["date"] for b in none if b["date"] in fm]
        self.n = [b["close"] for b in none if b["date"] in fm]
        self.f = [fm[x] for x in self.d]

    def at(self, day: str):
        i = bisect.bisect_right(self.d, day) - 1
        return (self.n[i], self.f[i]) if i >= 0 else (None, None)


# ── 主流程 ───────────────────────────────────────────────────────────────────
def main() -> None:
    print("… 分红送配", file=sys.stderr, flush=True)
    ps, tot = fy_tables()
    names = {c: r.get("name", "") for c, r in S.market_caps(True).items()}

    def streak(c, fy):
        n = 0
        while ps.get(fy - n, {}).get(c, 0) > 0:
            n += 1
        return n

    universe = sorted({c for fy in range(UNIV_FY0, FY1 + 1) for c in ps[fy]
                       if S.tradable(c, names.get(c, "")) and streak(c, fy) >= 4})
    print(f"… 样本股 {len(universe)} 只", file=sys.stderr, flush=True)
    sym = {PX.tf(c, "CN"): c for c in universe}
    none = {sym[k]: v for k, v in klines_cached(list(sym), COUNT, "none").items() if k in sym}
    fwd = {sym[k]: v for k, v in klines_cached(list(sym), COUNT, "forward").items() if k in sym}
    eps = eps_history(universe)
    ser = {c: Series(none[c], fwd[c]) for c in universe if c in none and c in fwd and c in eps}

    ref = max(ser.values(), key=lambda s: len(s.d)).d
    months = sorted({d[:7] for d in ref})
    mend = {m: max(d for d in ref if d.startswith(m)) for m in months}
    grid = [mend[m] for m in months]

    rec: dict = {}          # (code, t) → 指标
    for c, s in ser.items():
        e = eps[c]
        ann = sorted(q for q in e if q.endswith("1231"))
        for t in grid:
            pn, pf = s.at(t)
            if not pn:
                continue
            # 可用的最新年报（股息率）与最新报告期（TTM EPS）
            fys = [int(q[:4]) for q in ann if avail_date(q) <= t]
            if not fys:
                continue
            fy = max(fys)
            if streak(c, fy) < 4:
                continue
            a_day = avail_date(f"{fy}1231")
            _, fa = s.at(a_day)
            na, _ = s.at(a_day)
            if not fa or not na:
                continue
            k_a = fa / na                                  # 年报可用日的复权因子
            dps = ps[fy].get(c, 0.0)
            yld = dps * k_a / pf
            qs = [q for q in e if avail_date(q) <= t]
            if not qs:
                continue
            q = max(qs)

            def adj(qq):                                    # 该期每股收益 × 期末复权因子
                nn, ff = s.at(f"{qq[:4]}-{qq[4:6]}-{qq[6:]}")
                return e[qq] * (ff / nn) if nn else None
            if q.endswith("1231"):
                ttm = adj(q)
            else:
                prev_fy, prev_same = f"{int(q[:4]) - 1}1231", f"{int(q[:4]) - 1}{q[4:]}"
                parts = [adj(x) if x in e else None for x in (q, prev_fy, prev_same)]
                ttm = (parts[0] + parts[1] - parts[2]) if None not in parts else None
            pe = pf / ttm if ttm and ttm > 0 else None
            rec[(c, t)] = {"y": yld, "pe": pe, "fy": fy, "pf": pf}

    # 前瞻收益、相对估值
    rows = []
    for c, s in ser.items():
        ts = [t for t in grid if (c, t) in rec]
        for i, t in enumerate(ts):
            r = rec[(c, t)]
            t12 = grid[grid.index(t) + 12] if grid.index(t) + 12 < len(grid) else None
            r["ret"] = (s.at(t12)[1] / r["pf"] - 1) if t12 else None      # 最近 12 个月没有前瞻收益（回测仍要用）
            hist = [rec[(c, x)] for x in grid[max(0, grid.index(t) - 60):grid.index(t)] if (c, x) in rec]
            pes = [h["pe"] for h in hist if h["pe"]]
            ys = [h["y"] for h in hist]
            r["rel_pe"] = (r["pe"] / st.median(pes)) if (r["pe"] and len(pes) >= 36) else None
            r["med_pe"] = st.median(pes) if len(pes) >= 36 else None
            r["y_pct"] = (sum(y < r["y"] for y in ys) / len(ys)) if len(ys) >= 36 else None
            r["n6"] = sum(h["y"] >= 0.06 for h in hist[-36:])
            r["had4"] = any(h["y"] >= 0.04 for h in hist[-24:]) or r["y"] >= 0.04
            nxt = tot.get(r["fy"] + 1, {}).get(c)
            r["div_chg"] = (nxt / tot[r["fy"]][c] - 1) if (nxt is not None and tot[r["fy"]].get(c)) else None
            r.update(code=c, t=t)
            rows.append(r)
    by_t: dict = {}
    for r in rows:
        if r["ret"] is not None:
            by_t.setdefault(r["t"], []).append(r["ret"])
    med = {t: st.median(v) for t, v in by_t.items()}
    for r in rows:
        r["xs"] = (r["ret"] - med[r["t"]]) if r["ret"] is not None else None
    print(f"… 股票·月 {len(rows)}（{rows and min(r['t'] for r in rows)} ~ {rows and max(r['t'] for r in rows)}）",
          file=sys.stderr, flush=True)
    json.dump(rows, open(S._cpath(ROWS_FILE), "w"))   # 全部月份（回测用）
    if not REPORT:
        return
    rows = [r for r in rows if r["ret"] is not None]
    report(rows)
    report_combo(rows)


def line(lab, L):
    if len(L) < 15:
        return f"| {lab} | {len(L)} | 样本太少 | | | | |"
    dc = [r["div_chg"] for r in L if r["div_chg"] is not None]
    return (f"| {lab} | {len(L)} | {st.median(r['ret'] for r in L):+.1%} | {st.median(r['xs'] for r in L):+.1%} | "
            f"{sum(r['ret'] > 0 for r in L) / len(L):.0%} | {sum(r['ret'] < -.2 for r in L) / len(L):.0%} | "
            f"{(sum(d <= -.3 for d in dc) / len(dc)) if dc else 0:.0%} |")


HDR = "| 组 | 股票·月 | 一年收益中位 | 超额中位 | 胜率 | 跌超 20% | 次年分红砍 ≥30% |\n|---|---|---|---|---|---|---|"


def first_trigger(rows, cond):
    """每只股票每段「持仓」(had4) 里信号首次触发的那个月（之后 12 个月内不重复计）。"""
    out, last = [], {}
    for r in sorted(rows, key=lambda r: (r["code"], r["t"])):
        if not r["had4"] or not cond(r):
            continue
        lt = last.get(r["code"])
        if lt and (dt.date.fromisoformat(r["t"]) - dt.date.fromisoformat(lt)).days < 365:
            continue
        last[r["code"]] = r["t"]
        out.append(r)
    return out


def report(rows):
    print("\n## Q1 股息率常年 ≥6% 有没有问题（当月股息率 ≥6%，按过去 36 个月里 ≥6% 的月数分组）\n")
    print(HDR)
    hi = [r for r in rows if r["y"] >= 0.06]
    for lab, lo, up in (("新近（≤6 个月）", 0, 6), ("中（7~23 个月）", 7, 23), ("常年（≥24 个月）", 24, 36)):
        print(line(lab, [r for r in hi if lo <= r["n6"] <= up]))
    print(line("对照：股息率 4%~6%", [r for r in rows if 0.04 <= r["y"] < 0.06]))

    held = [r for r in rows if r["had4"]]
    print(f"\n## Q2 退出信号：信号首次触发后一年（样本 = 过去 24 个月股息率曾 ≥4% 的「持仓」，{len(held)} 个股票·月）\n")
    print("超额越负 = 卖得越对（卖掉之后它跑输）。\n")
    print(HDR)
    print(line("基准：持仓中、无信号", [r for r in held if r["y"] > 0.03 and (r["rel_pe"] or 0) < 1.25]))
    sig = {
        "绝对：股息率 ≤3%（三出）": lambda r: r["y"] <= 0.03,
        "绝对：股息率 ≤3.5%": lambda r: r["y"] <= 0.035,
        "相对：PE ≥ 自身 5 年中位 ×1.25": lambda r: r["rel_pe"] and r["rel_pe"] >= 1.25,
        "相对：PE ≥ 自身 5 年中位 ×1.5": lambda r: r["rel_pe"] and r["rel_pe"] >= 1.5,
        "相对：股息率落到自身 5 年最低 20% 分位": lambda r: r["y_pct"] is not None and r["y_pct"] <= 0.2,
        "常态 PE 15~25 的票：PE 升破 25": lambda r: r["med_pe"] and 15 <= r["med_pe"] <= 25 and r["pe"] and r["pe"] >= 25,
        "常态 PE 15~25 的票：PE 升破 30": lambda r: r["med_pe"] and 15 <= r["med_pe"] <= 25 and r["pe"] and r["pe"] >= 30,
        "绝对 ≤3% 且 相对 PE ≥1.25（两者同时）": lambda r: r["y"] <= 0.03 and r["rel_pe"] and r["rel_pe"] >= 1.25,
        "相对 PE ≥1.25 但股息率仍 >3%（三出不会卖）": lambda r: r["y"] > 0.03 and r["rel_pe"] and r["rel_pe"] >= 1.25,
        "股息率 ≤3% 但相对 PE <1.25（只有三出会卖）": lambda r: r["y"] <= 0.03 and r["rel_pe"] and r["rel_pe"] < 1.25,
    }
    trig = {}
    for k, f in sig.items():
        trig[k] = first_trigger(held, f)
        print(line(k, trig[k]))

    a = {(r["code"], r["t"][:7]) for r in held if r["y"] <= 0.03}
    b = {(r["code"], r["t"][:7]) for r in held if r["rel_pe"] and r["rel_pe"] >= 1.25}
    print(f"\n重合度（持仓股票·月层面）：股息率 ≤3% {len(a)}，相对 PE ≥1.25 {len(b)}，两者同时 {len(a & b)}"
          f"（占前者 {len(a & b) / max(1, len(a)):.0%}、后者 {len(a & b) / max(1, len(b)):.0%}）")

    print("\n## Q3 买入信号对照（全部长期分红股·月）\n")
    print(HDR)
    print(line("全样本", rows))
    print(line("绝对：股息率 ≥4%（四进）", [r for r in rows if r["y"] >= 0.04]))
    print(line("相对：PE ≤ 自身 5 年中位 ×0.8", [r for r in rows if r["rel_pe"] and r["rel_pe"] <= 0.8]))
    print(line("相对：股息率在自身 5 年最高 20% 分位", [r for r in rows if r["y_pct"] is not None and r["y_pct"] >= 0.8]))
    print(line("两者：股息率 ≥4% 且 处在自身最高 20%", [r for r in rows if r["y"] >= 0.04 and r["y_pct"] is not None and r["y_pct"] >= 0.8]))
    print(line("股息率 ≥4% 但处在自身最低 50%（常年高息）", [r for r in rows if r["y"] >= 0.04 and r["y_pct"] is not None and r["y_pct"] < 0.5]))


def report_combo(rows):
    """Q4 股息率 × 相对估值叠加。"""
    YB = [(0, .03, "<3%"), (.03, .04, "3~4%"), (.04, .05, "4~5%"), (.05, .06, "5~6%"), (.06, .08, "6~8%"), (.08, 1, "≥8%")]
    RB = [(0, .8, "<0.8"), (.8, 1.0, "0.8~1.0"), (1.0, 1.25, "1.0~1.25"), (1.25, 1.5, "1.25~1.5"), (1.5, 99, "≥1.5")]
    R = [r for r in rows if r["rel_pe"]]

    def grid(sel, title):
        print(f"\n### {title}：一年超额中位（括号内为样本数）\n")
        print("| 股息率 \\ 相对 PE | " + " | ".join(b[2] for b in RB) + " |")
        print("|---|" + "---|" * len(RB))
        for ylo, yhi, yl in YB:
            cells = []
            for rlo, rhi, _ in RB:
                c = [r["xs"] for r in sel if ylo <= r["y"] < yhi and rlo <= r["rel_pe"] < rhi]
                cells.append(f"{st.median(c):+.1%} ({len(c)})" if len(c) >= 30 else f"· ({len(c)})")
            print(f"| {yl} | " + " | ".join(cells) + " |")

    print("\n## Q4 股息率 × 相对估值叠加")
    grid(R, "全部长期分红股")
    grid([r for r in R if r["had4"]], "持仓（过去 24 个月股息率曾 ≥4%）")

    held = [r for r in rows if r["had4"]]
    print("\n### 组合退出信号（首次触发后一年；超额越负越好）\n")
    print(HDR)
    combos = {
        "三出：股息率 ≤3%": lambda r: r["y"] <= 0.03,
        "或：股息率 ≤3% 或 相对 PE ≥1.5": lambda r: r["y"] <= 0.03 or (r["rel_pe"] and r["rel_pe"] >= 1.5),
        "或：股息率 ≤3.5% 或 相对 PE ≥1.25": lambda r: r["y"] <= 0.035 or (r["rel_pe"] and r["rel_pe"] >= 1.25),
        "且：股息率 ≤4% 且 相对 PE ≥1.25": lambda r: r["y"] <= 0.04 and r["rel_pe"] and r["rel_pe"] >= 1.25,
        "且：股息率 ≤4% 且 股息率处自身最低 20%": lambda r: r["y"] <= 0.04 and r["y_pct"] is not None and r["y_pct"] <= 0.2,
    }
    for k, f in combos.items():
        print(line(k, first_trigger(held, f)))

    print("\n### 组合买入信号（全部长期分红股·月）\n")
    print(HDR)
    buys = {
        "四进：股息率 ≥4%": lambda r: r["y"] >= 0.04,
        "股息率 ≥4% 且 相对 PE ≤1.0": lambda r: r["y"] >= 0.04 and r["rel_pe"] and r["rel_pe"] <= 1.0,
        "股息率 ≥4% 且 相对 PE ≤0.8": lambda r: r["y"] >= 0.04 and r["rel_pe"] and r["rel_pe"] <= 0.8,
        "股息率 ≥4% 且 相对 PE >1.25（贵了还在 4% 以上）": lambda r: r["y"] >= 0.04 and r["rel_pe"] and r["rel_pe"] > 1.25,
        "股息率 ≥5% 且 股息率处自身最高 20%": lambda r: r["y"] >= 0.05 and r["y_pct"] is not None and r["y_pct"] >= 0.8,
    }
    for k, f in buys.items():
        print(line(k, [r for r in rows if f(r)]))


if __name__ == "__main__":
    if "--report" in sys.argv:                      # 只用缓存的月度指标重出报告
        _rows = [r for r in json.load(open(S._cpath("research_relval_rows.json"))) if r["ret"] is not None]
        report(_rows)
        report_combo(_rows)
    else:
        main()
