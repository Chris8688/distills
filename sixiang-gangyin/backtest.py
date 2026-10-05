#!/usr/bin/env python3
"""思想钢印 · 可量化选股规则的假设检验（美股 SEC XBRL，point-in-time）。

每条规则都当「负向剔除」检验：每月末 d，按 d 当天**已报送**（filed ≤ d）的财报
给 232 只美股打标，看被标的那组未来 12 个月是不是更差；与「同样只数随机剔除」比。

  python3 sixiang-gangyin/backtest.py            # 全部规则
  python3 sixiang-gangyin/backtest.py --only C12 # 单条
  python3 sixiang-gangyin/backtest.py --json out.json

判据：fit 2016-01~2020-12 与 test 2021-01~2025-09
两段都要「剔除后组合」的 12 个月收益或回撤**优于随机剔除的 90 分位**才算过。
"""
from __future__ import annotations

import argparse
import bisect
import collections
import datetime as dt
import gzip
import json
import math
import os
import random
import statistics as st

import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib import CACHE as _CACHE_DIR, DATA  # noqa: E402
from lib.xbrl_quarters import (FLOWS, POINTS, _d, _months, load_raw, split_factors,  # noqa: E402
                               ttm_hist, visible)
from lib import frame as FR  # noqa: E402

CACHE = os.path.join(_CACHE_DIR, "bt_panel.json")


def load_prices() -> dict:
    raw = json.load(open(os.path.join(DATA, "wide_bars.json")))
    return {tk: sorted((b["date"], float(b["close"])) for b in v if b.get("close"))
            for tk, v in raw.items()}


def px_at(ser: list, d: str) -> float | None:
    i = bisect.bisect_right(ser, (d, float("inf"))) - 1
    return ser[i][1] if i >= 0 else None


def build_panel(quarters, points, shares, prices, dates: list[str]) -> dict:
    """每个 (d, tk) 一行：只用 d 当天可见的数据。"""
    panel = {}
    for tk in sorted(quarters):
        ser = prices.get(tk)
        if not ser:
            continue
        splits = split_factors(shares.get(tk, []))
        sh_all = shares.get(tk, [])
        for d in dates:
            p = px_at(ser, d)
            if p is None:
                continue
            row = {"px": p}
            for c in FLOWS:
                vq = visible(quarters[tk].get(c, []), d)
                h = ttm_hist(vq)
                row[c] = h                       # [(end, TTM)]
                row[c + "_q"] = vq[-12:]
            for c in POINTS:
                vp = visible(points[tk].get(c, []), d)
                row[c] = vp[-1][1] if vp else None
            # 市值：当期最近报送股数 × 之后拆股比 × 复权价
            shv = [(e, v) for e, v in sh_all if e <= d]
            if shv:
                f = 1
                for e, k in splits:
                    if e > shv[-1][0]:
                        f *= k
                row["mcap"] = p * shv[-1][1] * f
            panel[(d, tk)] = row
    return panel


# ------------------------------------------------------------------ 规则
def _ttm_at(hist: list, back_q: int) -> float | None:
    return hist[-1 - back_q][1] if len(hist) > back_q else None


def _yoy_series(hist: list) -> list:
    """TTM 同比序列（相隔 4 个 TTM 点）。"""
    out = []
    for i in range(4, len(hist)):
        a, b = hist[i - 4][1], hist[i][1]
        if a and a > 0:
            out.append(b / a - 1)
    return out


def flags(panel: dict, d: str, tks: list[str], hist_pe: dict) -> dict[str, dict[str, bool | None]]:
    """→ {规则: {tk: True/False/None}}。None = 数据不足，不进该规则的样本。"""
    F = collections.defaultdict(dict)
    acc, vol = {}, {}
    for tk in tks:
        r = panel.get((d, tk))
        if not r:
            continue
        ni, rev, oi, ocf = r["net_income"], r["revenue"], r["operating_income"], r["op_cash_flow"]
        ni0 = _ttm_at(ni, 0)
        eq = r["equity"]
        # C1 · 三年净利下降或亏损（TTM 版）
        ni12 = _ttm_at(ni, 12)
        F["C1_ni_3y_down"][tk] = None if ni0 is None or ni12 is None else (ni0 < 0 or ni0 < ni12)
        # C2 · ROE 长期 <10%（近 4 个年度点里 ≥3 个）
        if eq and eq > 0 and len(ni) >= 13:
            roes = [ni[-1 - 4 * k][1] / eq for k in range(4)]   # 近似：用当期权益
            F["C2_roe_low"][tk] = sum(x < 0.10 for x in roes) >= 3
        else:
            F["C2_roe_low"][tk] = None
        # C3 · ROE >30% 且不是负/极小权益造成（权益/资产 ≥ 20%）
        if eq and r["assets"] and ni0 is not None:
            F["C3_roe_too_high"][tk] = FR.e1_high_roe(ni0 / eq if eq > 0 else None,
                                                      eq_to_assets=eq / r["assets"])[0] if eq > 0 else False
        else:
            F["C3_roe_too_high"][tk] = None
        # C4 · 净现比：近 3 年 OCF 合计 / 净利合计 < 0.8（净利为正时）
        oq, nq = r["op_cash_flow_q"], r["net_income_q"]
        if len(oq) >= 12 and len(nq) >= 12:
            so, sn = sum(v for _, v in oq[-12:]), sum(v for _, v in nq[-12:])
            F["C4_ocf_ni_low"][tk] = (sn > 0 and so / sn < 0.8)
        else:
            F["C4_ocf_ni_low"][tk] = None
        # C5 · 应计 |营业利润−经营现金流| / 总资产（截面前 20%）
        oi0, ocf0 = _ttm_at(oi, 0), _ttm_at(ocf, 0)
        if oi0 is not None and ocf0 is not None and r["assets"]:
            acc[tk] = abs(oi0 - ocf0) / r["assets"]
        # C6 · 盈余波动：近 12 季单季净利 std/|mean|（截面前 20%）
        if len(nq) >= 12:
            vals = [v for _, v in nq[-12:]]
            m = st.mean(vals)
            vol[tk] = st.pstdev(vals) / abs(m) if m else float("inf")
        # C7 · 增速见顶：近 8 季 TTM 营收同比峰值 ≥100%，现在 ≤30% 且没再加速
        ys = _yoy_series(rev)
        F["C7_growth_peak"][tk] = FR.e2_growth_peak(ys)[0]          # 与线上同一实现
        ysn = _yoy_series(ni)
        if len(ysn) >= 3:
            F["C7b_ni_growth_peak"][tk] = max(ysn[-8:]) >= 1.0 and ysn[-1] <= 0.30 and ysn[-1] <= ysn[-2]
        else:
            F["C7b_ni_growth_peak"][tk] = None
        # 估值序列（月度、PIT）
        pe_hist = hist_pe.get(tk, [])
        pe_now = [x for x in pe_hist if x[0] <= d]
        # C8 · 股价近 52 周新高（2% 内）但 PE 比 52 周 PE 高点低 ≥10%
        last12 = [x for x in pe_now if x[0] > _shift(d, -365)]
        if len(last12) >= 10 and all(x[2] is not None for x in last12):
            pmax = max(x[1] for x in last12)
            pes = [x[2] for x in last12 if x[2] and x[2] > 0]
            pe0 = last12[-1][2]
            F["C8_newhigh_pe_lower"][tk] = (r["px"] >= 0.98 * pmax and pe0 is not None and pe0 > 0
                                           and bool(pes) and pe0 <= 0.9 * max(pes))
        else:
            F["C8_newhigh_pe_lower"][tk] = None
        # C9 · 估值扩张：2 年股价涨幅 − 2 年 TTM 净利涨幅 > 100pp
        two = [x for x in pe_now if x[0] <= _shift(d, -730)]
        ni8 = _ttm_at(ni, 8)
        if two and ni0 is not None and ni8 and ni8 > 0:
            pr = r["px"] / two[-1][1] - 1
            F["C9_val_expansion"][tk] = (pr - (ni0 / ni8 - 1)) > 1.0
        else:
            F["C9_val_expansion"][tk] = None
        # C10 · PBR 自身历史分位 ≥90（≥3 年历史）；C11 · 同时 PE 分位 ≥90
        pbs = [x[3] for x in pe_now if x[3]]
        if len(pbs) >= 36 and r.get("mcap") and eq and eq > 0:
            pb0 = r["mcap"] / eq
            pct = sum(x <= pb0 for x in pbs) / len(pbs)
            F["C10_pbr_p90"][tk] = pct >= 0.9
            pes_all = [x[2] for x in pe_now if x[2] and x[2] > 0]
            pe0 = r["mcap"] / ni0 if ni0 and ni0 > 0 else None
            F["C11_pbr_pe_p90"][tk] = (pct >= 0.9 and pe0 is not None and len(pes_all) >= 36
                                       and sum(x <= pe0 for x in pes_all) / len(pes_all) >= 0.9)
        else:
            F["C10_pbr_p90"][tk] = F["C11_pbr_pe_p90"][tk] = None
        # C12 · 极限赔率（2023-02）：上=TTM 净利×近 3 年「3 个月中位 PE」最高；
        #        下=近 8 季 TTM 最低值×同口径最低 PE。赔率 =(上−市值)/(市值−下)
        o = odds_at(r, ni, pe_now, d)
        F["C12_odds_lt1"][tk] = None if o is None else o < 1
        F["P12_odds_ge3"][tk] = None if o is None else o >= 3      # 正向：3:1 加满
        F["P12b_odds_ge2"][tk] = None if o is None else o >= 2
    for name, src in (("C5_accrual_top20", acc), ("C6_earn_vol_top20", vol)):
        if len(src) >= 20:
            cut = sorted(src.values())[int(len(src) * 0.8)]
            for tk in tks:
                F[name][tk] = None if tk not in src else src[tk] >= cut
    return F


def odds_at(r: dict, ni: list, pe_now: list, d: str) -> float | None:
    win = [x for x in pe_now if x[0] > _shift(d, -365 * 3)]
    pes = [x[2] for x in win if x[2] and x[2] > 0]
    if len(pes) < 24 or not r.get("mcap") or len(ni) < 8:
        return None
    med3 = [st.median(pes[i:i + 3]) for i in range(len(pes) - 2)]
    hi, lo = max(med3), min(med3)
    ni0 = ni[-1][1]
    ni_lo = min(v for _, v in ni[-8:])
    if ni0 <= 0 or ni_lo <= 0:
        return None
    up, dn, m = ni0 * hi, ni_lo * lo, r["mcap"]
    if m <= dn:
        return 99.0
    return (up - m) / (m - dn)


def _shift(d: str, days: int) -> str:
    return (_d(d) + dt.timedelta(days=days)).isoformat()


def pe_history(panel: dict, dates: list[str], tks: list[str]) -> dict:
    """→ {tk: [(d, px, PE_TTM, PBR)]}，每个点只用 d 当天可见的数据。"""
    out = collections.defaultdict(list)
    for d in dates:
        for tk in tks:
            r = panel.get((d, tk))
            if not r:
                continue
            ni0 = _ttm_at(r["net_income"], 0)
            m, eq = r.get("mcap"), r.get("equity")
            pe = m / ni0 if m and ni0 and ni0 > 0 else None
            pb = m / eq if m and eq and eq > 0 else None
            out[tk].append((d, r["px"], pe, pb))
    return out


# ------------------------------------------------------------------ 结果
import numpy as np

H = 250   # 前看交易日


def fwd_path(prices: dict, tk: str, d: str) -> "np.ndarray | None":
    """d 之后 H 个交易日的净值路径（起点 1）。不足 H−10 根不算。"""
    ser = prices[tk]
    i = bisect.bisect_right(ser, (d, float("inf"))) - 1
    if i < 0 or i + H - 10 >= len(ser):
        return None
    seg = [p for _, p in ser[i:i + H + 1]]
    if len(seg) < H + 1:
        seg = seg + [seg[-1]] * (H + 1 - len(seg))
    a = np.asarray(seg, dtype=float)
    if not (a[0] > 0) or not np.all(np.isfinite(a)) or (a <= 0).any():
        return None
    return a / a[0]


def _cal(path: "np.ndarray") -> "np.ndarray":
    """等权买入持有组合（行 = 组合）→ Calmar = 12m 收益 / |最大回撤|。"""
    peak = np.maximum.accumulate(path, axis=-1)
    mdd = (path / peak - 1).min(axis=-1)
    ret = path[..., -1] - 1
    return ret / np.maximum(np.abs(mdd), 0.02), ret, mdd


SEGS = (("fit", "2016-01-01", "2020-12-31"), ("test", "2021-01-01", "2025-09-30"))


def evaluate(rule, rows, draws, seed, positive=False, segs=SEGS):
    """rows = [(d, tk, flagged, path)]。
    负向规则：剔除 flagged 后的等权组合 vs 全体；正向规则：只持 flagged vs 全体。
    指标 = 12m 组合 Calmar / 收益 / 回撤之差；随机臂 = 每期同样只数随机剔除（或随机选取）。"""
    rng = np.random.default_rng(seed)
    out = {}
    for seg, lo, hi in segs:
        by = collections.defaultdict(list)
        for d, tk, f, path in rows:
            if lo <= d <= hi:
                by[d].append((f, path))
        dates = sorted(d for d, v in by.items() if 0 < sum(x[0] for x in v) < len(v) and len(v) >= 20)
        if len(dates) < 6:
            out[seg] = {"n_dates": len(dates)}
            continue
        dc, dr, dm = [], [], []
        rc = np.zeros(draws); rr = np.zeros(draws); rm = np.zeros(draws)
        nflag = []
        for d in dates:
            fl = np.array([x[0] for x in by[d]])
            P = np.stack([x[1] for x in by[d]])            # n × (H+1)
            sel = fl if positive else ~fl                   # 组合里留下的
            k = int(sel.sum())
            nflag.append(int(fl.sum()))
            c_all, r_all, m_all = _cal(P.mean(axis=0))
            c1, r1, m1 = _cal(P[sel].mean(axis=0))
            dc.append(c1 - c_all); dr.append(r1 - r_all); dm.append(m1 - m_all)
            # 随机臂：draws 次随机留 k 只
            W = np.zeros((draws, len(fl)))
            for i in range(draws):
                W[i, rng.choice(len(fl), k, replace=False)] = 1.0 / k
            with np.errstate(all="ignore"):   # numpy 2.2 + Accelerate 的假告警，结果已核对有限且与 einsum 一致
                WP = W @ P
            cR, rR, mR = _cal(WP)
            rc += cR - c_all; rr += rR - r_all; rm += mR - m_all
        n = len(dates)
        c0, r0, m0 = np.mean(dc), np.mean(dr), np.mean(dm)
        out[seg] = {"n_dates": n, "avg_flagged": float(np.mean(nflag)),
                    "d_cal": float(c0), "d_ret": float(r0), "d_mdd": float(m0),
                    "cal_pct": float((rc / n < c0).mean()), "ret_pct": float((rr / n < r0).mean()),
                    "mdd_pct": float((rm / n < m0).mean()),
                    "months_pos": float(np.mean(np.array(dc) > 0))}
    f, t = out.get("fit", {}), out.get("test", {})
    out["pass"] = bool(f.get("cal_pct", 0) >= 0.9 and t.get("cal_pct", 0) >= 0.9)
    return out


def by_year(rule, rows, draws, seed, positive=False, years=range(2016, 2026)) -> dict:
    """逐年（每年 12 个月末）ΔCalmar 与随机分位 —— 看是不是只靠一两年。"""
    out = {}
    for y in years:
        sub = [r for r in rows if r[0][:4] == str(y)]
        by = collections.defaultdict(list)
        for d, tk, f, path in sub:
            by[d].append((f, path))
        dates = sorted(d for d, v in by.items() if 0 < sum(x[0] for x in v) < len(v) and len(v) >= 20)
        if len(dates) < 3:
            continue
        rng = np.random.default_rng(seed + y)
        dc, rc = [], np.zeros(draws)
        for d in dates:
            fl = np.array([x[0] for x in by[d]])
            P = np.stack([x[1] for x in by[d]])
            sel = fl if positive else ~fl
            k = int(sel.sum())
            c_all = _cal(P.mean(axis=0))[0]
            dc.append(_cal(P[sel].mean(axis=0))[0] - c_all)
            W = np.zeros((draws, len(fl)))
            for i in range(draws):
                W[i, rng.choice(len(fl), k, replace=False)] = 1.0 / k
            with np.errstate(all="ignore"):
                WP = W @ P
            rc += _cal(WP)[0] - c_all
        c0 = float(np.mean(dc))
        out[y] = (c0, float((rc / len(dates) < c0).mean()))
    return out


def month_ends(a: str, b: str) -> list[str]:
    out, y, m = [], int(a[:4]), int(a[5:7])
    while True:
        nm = dt.date(y + (m == 12), m % 12 + 1, 1)
        d = (nm - dt.timedelta(days=1)).isoformat()
        if d > b:
            return out
        out.append(d)
        y, m = nm.year, nm.month


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--draws", type=int, default=300)
    ap.add_argument("--json", default="")
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--years", action="store_true", help="另出逐年稳健性")
    a = ap.parse_args()

    quarters, points, shares = load_raw()
    prices = load_prices()
    hist_dates = month_ends("2012-01-01", "2026-09-30")
    test_dates = [d for d in hist_dates if "2016-01-01" <= d <= "2025-09-30"]
    tks = sorted(t for t in quarters if t in prices)
    print(f"标的 {len(tks)} 只；月末 {len(test_dates)} 个（2016-01 ~ 2025-09）；前看 12 个月")
    panel = build_panel(quarters, points, shares, prices, hist_dates)
    pe_h = pe_history(panel, hist_dates, tks)

    rows = collections.defaultdict(list)
    paths = {}
    for d in test_dates:
        F = flags(panel, d, tks, pe_h)
        for rule, m in F.items():
            if a.only and not rule.startswith(a.only):
                continue
            for tk, f in m.items():
                if f is None:
                    continue
                if (d, tk) not in paths:
                    paths[(d, tk)] = fwd_path(prices, tk, d)
                if paths[(d, tk)] is not None:
                    rows[rule].append((d, tk, bool(f), paths[(d, tk)]))

    res = {}
    print(f"\n{'规则':<22}{'段':<5}{'月数':>5}{'均标':>6}{'ΔCalmar':>9}{'随机分位':>8}{'Δ收益':>8}{'分位':>6}"
          f"{'Δ回撤':>8}{'分位':>6}{'月胜率':>7}  判")
    for rule in sorted(rows):
        ev = evaluate(rule, rows[rule], a.draws, 7, positive=rule.startswith("P"))
        res[rule] = ev
        for seg in ("fit", "test"):
            s_ = ev.get(seg, {})
            if "d_cal" not in s_:
                print(f"{rule:<22}{seg:<5}{s_.get('n_dates', 0):>5}  （样本不足）")
                continue
            tail = f"  {'✅' if ev['pass'] else '❌'}" if seg == "test" else ""
            print(f"{rule:<22}{seg:<5}{s_['n_dates']:>5}{s_['avg_flagged']:>6.1f}{s_['d_cal']:>+9.3f}"
                  f"{s_['cal_pct']:>8.0%}{s_['d_ret']:>+8.2%}{s_['ret_pct']:>6.0%}{s_['d_mdd']:>+8.2%}"
                  f"{s_['mdd_pct']:>6.0%}{s_['months_pos']:>7.0%}{tail}")
    if a.years:
        print(f"\n逐年 ΔCalmar（随机分位）")
        for rule in sorted(rows):
            yy = by_year(rule, rows[rule], a.draws, 7, positive=rule.startswith("P"))
            res.setdefault(rule, {})["by_year"] = yy
            good = sum(p >= 0.9 for _, p in yy.values())
            bad = sum(p <= 0.1 for _, p in yy.values())
            print(f"{rule:<22}" + " ".join(f"{y % 100:02d}:{c:+.2f}({p:.0%})" for y, (c, p) in sorted(yy.items()))
                  + f"   ≥90分位 {good}/{len(yy)} 年，≤10分位 {bad}/{len(yy)} 年")
    if a.json:
        json.dump(res, open(a.json, "w"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
