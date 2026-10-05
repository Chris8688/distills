#!/usr/bin/env python3
"""思想钢印的两套机械交易规则 —— 回测（对照买持、同平均仓位的恒定仓位）。

规则（出处见 docs/01 §4，参数取作者原文）：
  WAVE  长线波段（2023-07）：60 日均线乖离率 ≤ 低位阈值 → 加一档（+25%），≥ 高位阈值 → 减一档；
        起始半仓，同方向两次操作至少隔 5 个交易日。阈值：熊市口径 +10%/−15%，牛市口径 +15%/−10%
  GRID  ETF 均线网格·成长策略（2022-07）：60 日线 = 半仓，每偏离一档 5% 加/减 10%（上下各 5 档）
  GRID+ 同上，60 日线跌破 120 日线（均线转空）时清仓 —— 作者说「唯一需要止损的情形」
  REB   超跌反弹网格（2022-07）：年线以上空仓，年线下每跌一档 5% 加 1/7，上穿年线清仓

对照：
  BH     买入持有（满仓）
  CONST  恒定仓位 = 该规则在同一只、同一时段的平均仓位（不择时，只是「少买一点」）——
         **择时有没有用就看能不能赢它**；赢不了 BH 只是因为仓位低，不说明规则坏

样本：16 只常见宽基与行业 ETF（见 ETFS），申万二级 124 个行业指数（2000 起，ETF 长历史代理）。
成本：A股 买 3bps / 卖 8bps，按 |Δw| 计提。

  python3 sixiang-gangyin/backtest_trading.py [--sw-only|--etf-only]
"""
from __future__ import annotations

import argparse
import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lib import prices as PX                                              # noqa: E402

ETFS = {                                   # 常见宽基与行业 ETF，2020 年前上市
    "510050.SH": "上证50ETF", "510300.SH": "沪深300ETF", "510500.SH": "中证500ETF",
    "159915.SZ": "创业板ETF", "510880.SH": "红利ETF", "512880.SH": "证券ETF",
    "512800.SH": "银行ETF", "512010.SH": "医药ETF", "159928.SZ": "消费ETF",
    "512480.SH": "半导体ETF", "512660.SH": "军工ETF", "515030.SH": "新能源车ETF",
    "518880.SH": "黄金ETF", "513100.SH": "纳指ETF", "159920.SZ": "恒生ETF",
    "512400.SH": "有色金属ETF",
}

BUY, SELL, TD = 3.0, 8.0, 244.0
WARM = 250


def ma(c: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(c), np.nan)
    cs = np.cumsum(np.insert(c, 0, 0.0))
    out[n - 1:] = (cs[n:] - cs[:-n]) / n
    return out


def wave(c: np.ndarray, hi: float, lo: float, n: int = 60, step: float = 0.25, cool: int = 5) -> np.ndarray:
    m = ma(c, n)
    w = np.zeros(len(c))
    p, last_add, last_cut = 0.5, -99, -99
    for i in range(len(c)):
        if i < WARM:
            continue
        d = c[i] / m[i] - 1
        if d <= lo and p < 1 and i - last_add >= cool:
            p, last_add = min(1.0, p + step), i
        elif d >= hi and p > 0 and i - last_cut >= cool:
            p, last_cut = max(0.0, p - step), i
        w[i] = p
    return w


def grid(c: np.ndarray, stop: bool = False, n: int = 60, step: float = 0.05, k: int = 5) -> np.ndarray:
    m, m120 = ma(c, n), ma(c, 120)
    w = np.zeros(len(c))
    lg = math.log(1 + step)
    for i in range(WARM, len(c)):
        x = math.log(c[i] / m[i]) / lg                      # 偏离了几档（带符号）
        lvl = int(math.floor(x)) if x > 0 else -int(math.floor(-x))   # 0 档 = 均线 ±1 档内
        lvl = max(-k, min(k, lvl))
        w[i] = 0.5 - 0.5 * lvl / k
        if stop and m[i] < m120[i]:
            w[i] = 0.0
    return w


def rebound(c: np.ndarray, step: float = 0.05, k: int = 7) -> np.ndarray:
    m = ma(c, 250)
    w = np.zeros(len(c))
    lg = math.log(1 / (1 - step))
    for i in range(WARM, len(c)):
        if c[i] >= m[i]:
            continue
        lvl = min(k, int(math.floor(math.log(m[i] / c[i]) / lg)))
        w[i] = lvl / k
    return w


def daily_ret(c: np.ndarray, w: np.ndarray) -> np.ndarray:
    r = np.zeros(len(c))
    r[1:] = w[:-1] * (c[1:] / c[:-1] - 1)
    dw = np.diff(w, prepend=w[0])
    r -= np.where(dw > 0, dw * BUY, -dw * SELL) / 1e4
    return r


def stats(r: np.ndarray) -> dict:
    if len(r) < 120:
        return {}
    eq = np.cumprod(1 + r)
    y = len(r) / TD
    cagr = eq[-1] ** (1 / y) - 1 if eq[-1] > 0 else -1.0
    e = np.concatenate([[1.0], eq])
    mdd = float((1 - e / np.maximum.accumulate(e)).max())
    return {"cagr": cagr, "mdd": mdd, "cal": cagr / mdd if mdd > 1e-9 else float("nan")}


RULES = {
    "WAVE熊": lambda c: wave(c, 0.10, -0.15),
    "WAVE牛": lambda c: wave(c, 0.15, -0.10),
    "GRID": lambda c: grid(c),
    "GRID+止损": lambda c: grid(c, stop=True),
    "REB": lambda c: rebound(c),
}


def run(universe: dict[str, list[dict]], eras: list[tuple[str, str]], title: str) -> None:
    sleeves = {}                           # name → {strategy: (dates, r, w)}
    for name, bars in universe.items():
        v = sorted([b for b in bars if b.get("close")], key=lambda b: str(b["date"]))
        if len(v) < WARM + 250:
            continue
        d = [str(b["date"])[:10] for b in v]
        c = np.asarray([float(b["close"]) for b in v], float)
        s = {"BH": np.where(np.arange(len(c)) >= WARM, 1.0, 0.0)}
        for k, f in RULES.items():
            s[k] = f(c)
        sleeves[name] = (d, c, s)
    print(f"\n════ {title}：{len(sleeves)} 只 ════")
    cols = ["BH"] + list(RULES)
    for lo, hi in eras:
        print(f"\n— {lo} ~ {hi}")
        # 组合层：每天对当天有数的 sleeve 等权（每只 1/N 资金，现金 0 收益）
        agg = {k: {} for k in cols}
        agg_c = {k: {} for k in RULES}
        avg_w = {k: [] for k in RULES}
        win_const = {k: [0, 0] for k in RULES}
        for name, (d, c, s) in sleeves.items():
            idx = [i for i, x in enumerate(d) if lo <= x <= hi and i >= WARM]
            if len(idx) < 120:
                continue
            a, b = idx[0], idx[-1] + 1
            cc = c[a - 1:b]
            rs = {}
            for k in cols:
                ww = s[k][a - 1:b]
                r = daily_ret(cc, ww)[1:]
                rs[k] = r
                for dd, x in zip(d[a:b], r):
                    agg[k].setdefault(dd, []).append(x)
            for k in RULES:
                mw = float(s[k][a:b].mean())
                avg_w[k].append(mw)
                rc = daily_ret(cc, np.full(len(cc), mw))[1:]          # 恒定仓位，无交易
                for dd, x in zip(d[a:b], rc):
                    agg_c[k].setdefault(dd, []).append(x)
                st_r, st_c = stats(rs[k]), stats(rc)
                if st_r and st_c:
                    win_const[k][0] += st_r["cal"] > st_c["cal"]
                    win_const[k][1] += 1
        def pool(dct):
            ds = sorted(dct)
            return stats(np.array([np.mean(dct[x]) for x in ds]))
        print(f"  {'策略':<10}{'年化':>8}{'最大回撤':>9}{'Calmar':>8}{'均仓':>6} │ {'恒定同仓位':>8}{'回撤':>7}{'Calmar':>8} │ 胜恒定")
        for k in cols:
            p = pool(agg[k])
            if not p:
                continue
            line = f"  {k:<10}{p['cagr']:>+8.1%}{p['mdd']:>9.1%}{p['cal']:>8.2f}"
            if k in RULES:
                q = pool(agg_c[k])
                line += (f"{np.mean(avg_w[k]):>6.0%} │ {q['cagr']:>+8.1%}{q['mdd']:>7.1%}{q['cal']:>8.2f} │"
                         f" {win_const[k][0]:>3}/{win_const[k][1]}")
            else:
                line += f"{'100%' if k == 'BH' else '':>6} │"
            print(line)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sw-only", action="store_true")
    ap.add_argument("--etf-only", action="store_true")
    a = ap.parse_args()
    if not a.sw_only:
        px = PX.fetch(ETFS, "CN", count=1600, quiet=True)
        etf = {f"{c} {n}": px.get(PX.tf(c, "CN")) or [] for c, n in ETFS.items()}
        run(etf, [("2021-01-01", "2023-06-30"), ("2023-07-01", "2026-09-30"), ("2021-01-01", "2026-09-30")],
            "16 只宽基与行业 ETF")
    if not a.etf_only:
        from lib import sw_index
        sw = sw_index.fetch_sw2()
        run(sw, [("2001-01-01", "2008-12-31"), ("2009-01-01", "2016-12-31"), ("2017-01-01", "2026-09-30"),
                 ("2001-01-01", "2026-09-30")], "申万二级行业指数（ETF 长历史代理）")


if __name__ == "__main__":
    main()
