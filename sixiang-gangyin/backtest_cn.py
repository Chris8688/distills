#!/usr/bin/env python3
"""A股复核：backtest.py 里不依赖股数/估值序列的规则，在 A股 757 只上再测一遍。

PIT 口径：财报按**法定披露截止日**才算可见（一季报 4-30、中报 8-31、三季报 10-31、年报次年 4-30）——
比实际披露偏晚，只会让规则更难过，不会更容易。
价格 2020-02 起 → 前看 12 个月的月末 2020-03 ~ 2025-09；fit 2020-03~2022-12，test 2023-01~2025-09。

  python3 sixiang-gangyin/backtest_cn.py [--years]
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import statistics as st

import numpy as np

import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backtest as B  # noqa: E402
from lib import frame as FR  # noqa: E402

DATA = B.DATA
DEADLINE = {"0331": "0430", "0630": "0831", "0930": "1031"}


def avail(period: str) -> str:
    y, md = period[:4], period[4:]
    if md == "1231":
        return f"{int(y) + 1}-04-30"
    return f"{y}-{DEADLINE[md][:2]}-{DEADLINE[md][2:]}"


def load() -> tuple[dict, dict]:
    fds = {}
    for f in glob.glob(os.path.join(DATA, "cn_*_fd.json")) + [os.path.join(DATA, "cn_fundamentals.json")]:
        try:
            d = json.load(open(f))
        except Exception:      # noqa: BLE001
            continue
        for k, v in d.items():
            if isinstance(v, dict) and "_periods" in v:
                fds[k[:6]] = v
    px = {}
    for f in glob.glob(os.path.join(DATA, "cn_*_px.json")):
        for k, v in json.load(open(f)).items():
            ser = sorted((b["date"], float(b["close"])) for b in v if b.get("close"))
            if ser and (k[:6] not in px or len(ser) > len(px[k[:6]])):
                px[k[:6]] = ser
    return {k: v for k, v in fds.items() if k in px}, px


def ttm_series(fd: dict, field: str, d: str) -> list[tuple[str, float]]:
    """累计口径 → 可见的 TTM 序列 [(period, TTM)]。TTM = 本期累计 + 上年年报 − 上年同期累计。"""
    s = fd.get(field) or {}
    vis = sorted(p for p in s if s[p] is not None and avail(p) <= d)
    out = []
    for p in vis:
        y, md = p[:4], p[4:]
        if md == "1231":
            out.append((p, float(s[p])))
            continue
        a, b = f"{int(y) - 1}1231", f"{int(y) - 1}{md}"
        if a in s and b in s and s[a] is not None and s[b] is not None:
            out.append((p, float(s[p]) + float(s[a]) - float(s[b])))
    return out


def single_q(fd: dict, field: str, d: str) -> list[float]:
    s = fd.get(field) or {}
    vis = sorted(p for p in s if s[p] is not None and avail(p) <= d)
    out, prev = [], {}
    for p in vis:
        y, md = p[:4], p[4:]
        if md == "0331":
            out.append(float(s[p]))
        else:
            pp = {"0630": "0331", "0930": "0630", "1231": "0930"}[md]
            if y + pp in s and s[y + pp] is not None:
                out.append(float(s[p]) - float(s[y + pp]))
    return out


def annual(fd: dict, field: str, d: str) -> list[float]:
    s = fd.get(field) or {}
    return [float(s[p]) for p in sorted(s) if p.endswith("1231") and s[p] is not None and avail(p) <= d]


def flags_cn(fds: dict, px: dict, d: str) -> dict:
    F = collections.defaultdict(dict)
    vol, acc = {}, {}
    for c, fd in fds.items():
        ni = ttm_series(fd, "归母净利润", d)
        if not ni or ni[-1][0] < f"{int(d[:4]) - 2}":
            continue
        ni0 = ni[-1][1]
        ni12 = ni[-13][1] if len(ni) >= 13 else None
        F["C1_ni_3y_down"][c] = None if ni12 is None else (ni0 < 0 or ni0 < ni12)
        roe = annual(fd, "净资产收益率(ROE)", d)
        lev = annual(fd, "资产负债率", d)
        if len(roe) >= 4:
            F["C2_roe_low"][c] = sum(x < 10 for x in roe[-4:]) >= 3
        if roe and lev:
            F["C3_roe_too_high"][c] = FR.e1_high_roe(roe[-1] / 100, debt_ratio=lev[-1] / 100)[0]
            F["C3b_roe_gt25"][c] = roe[-1] > 25 and lev[-1] <= 80
            F["C3c_roe_gt40"][c] = roe[-1] > 40 and lev[-1] <= 80
        ocf = ttm_series(fd, "经营现金流量净额", d)
        if len(ocf) >= 9 and len(ni) >= 9:
            so = sum(v for _, v in ocf[-1::-4][:3])
            sn = sum(v for _, v in ni[-1::-4][:3])
            F["C4_ocf_ni_low"][c] = sn > 0 and so / sn < 0.8
            eq = annual(fd, "股东权益合计(净资产)", d)
            if eq and lev and lev[-1] < 100:
                assets = eq[-1] / (1 - lev[-1] / 100)
                acc[c] = abs(ni0 - ocf[-1][1]) / assets if assets > 0 else None
        q = single_q(fd, "归母净利润", d)
        if len(q) >= 12:
            m = st.mean(q[-12:])
            vol[c] = st.pstdev(q[-12:]) / abs(m) if m else float("inf")
        for name, fld in (("C7_growth_peak", "营业总收入"), ("C7b_ni_growth_peak", "归母净利润")):
            h = ttm_series(fd, fld, d)
            ys = [h[i][1] / h[i - 4][1] - 1 for i in range(4, len(h)) if h[i - 4][1] > 0]
            if len(ys) >= 3:
                F[name][c] = FR.e2_growth_peak(ys)[0]
        ser = px[c]
        p0 = B.px_at(ser, d)
        p2 = B.px_at(ser, B._shift(d, -730))
        ni8 = ni[-9][1] if len(ni) >= 9 else None
        if p0 and p2 and ser[0][0] <= B._shift(d, -730) and ni8 and ni8 > 0:
            F["C9_val_expansion"][c] = (p0 / p2 - 1) - (ni0 / ni8 - 1) > 1.0
    for name, src in (("C6_earn_vol_top20", vol), ("C5_accrual_top20", {k: v for k, v in acc.items() if v is not None})):
        if len(src) >= 20:
            cut = sorted(src.values())[int(len(src) * 0.8)]
            for c in src:
                F[name][c] = src[c] >= cut
    return F


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--draws", type=int, default=300)
    ap.add_argument("--years", action="store_true")
    ap.add_argument("--json", default="")
    a = ap.parse_args()
    fds, px = load()
    dates = B.month_ends("2020-03-01", "2025-09-30")
    print(f"A股 {len(fds)} 只；月末 {len(dates)} 个；前看 12 个月")
    rows = collections.defaultdict(list)
    paths = {}
    for d in dates:
        F = flags_cn(fds, px, d)
        for rule, m in F.items():
            for c, f in m.items():
                if f is None:
                    continue
                if (d, c) not in paths:
                    paths[(d, c)] = B.fwd_path(px, c, d)
                if paths[(d, c)] is not None:
                    rows[rule].append((d, c, bool(f), paths[(d, c)]))

    # 时间段换成 A股 的
    segs = (("fit", "2020-03-01", "2022-12-31"), ("test", "2023-01-01", "2025-09-30"))
    res = {}
    print(f"\n{'规则':<22}{'段':<5}{'月数':>5}{'均标':>7}{'ΔCalmar':>9}{'随机分位':>8}{'Δ收益':>8}{'分位':>6}"
          f"{'Δ回撤':>8}{'分位':>6}{'月胜率':>7}  判")
    for rule in sorted(rows):
        ev = B.evaluate(rule, rows[rule], a.draws, 7, segs=segs)
        res[rule] = ev
        for seg in ("fit", "test"):
            s = ev.get(seg, {})
            if "d_cal" not in s:
                print(f"{rule:<22}{seg:<5}{s.get('n_dates', 0):>5}  （样本不足）")
                continue
            tail = f"  {'✅' if ev['pass'] else '❌'}" if seg == "test" else ""
            print(f"{rule:<22}{seg:<5}{s['n_dates']:>5}{s['avg_flagged']:>7.1f}{s['d_cal']:>+9.3f}"
                  f"{s['cal_pct']:>8.0%}{s['d_ret']:>+8.2%}{s['ret_pct']:>6.0%}{s['d_mdd']:>+8.2%}"
                  f"{s['mdd_pct']:>6.0%}{s['months_pos']:>7.0%}{tail}")
    if a.years:
        print("\n逐年 ΔCalmar（随机分位）")
        for rule in sorted(rows):
            yy = B.by_year(rule, rows[rule], a.draws, 7, years=range(2020, 2026))
            res[rule]["by_year"] = yy
            good = sum(p >= 0.9 for _, p in yy.values())
            bad = sum(p <= 0.1 for _, p in yy.values())
            print(f"{rule:<22}" + " ".join(f"{y % 100:02d}:{c:+.2f}({p:.0%})" for y, (c, p) in sorted(yy.items()))
                  + f"   ≥90分位 {good}/{len(yy)}，≤10分位 {bad}/{len(yy)}")
    if a.json:
        json.dump(res, open(a.json, "w"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
