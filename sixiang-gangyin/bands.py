#!/usr/bin/env python3
"""思想钢印 · 机械化规则计算器（只读、只打印，不下单）。

三个子命令，规则出处见 docs/01-蒸馏笔记.md §3 / §4：

  wave   长线波段四原则（2023-07）：20/60 日均线乖离率定高低位
  grid   ETF 均线网格（2022-07）：成长策略（60 日线）/ 超跌反弹策略（年线）
  odds   极限赔率建仓（2023-02/03）+ 预期收益率（2022-03）

用法：
  python3 sixiang-gangyin/bands.py wave --market CN --codes 510300,512480
  python3 sixiang-gangyin/bands.py grid --market CN --codes 159863 --max-pos 30
  python3 sixiang-gangyin/bands.py odds --price 12 --up 18 --down 8 --win 0.5
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _ma(xs: list[float], n: int) -> float | None:
    return sum(xs[-n:]) / n if len(xs) >= n else None


def _load(codes: list[str], market: str, offline: bool) -> dict[str, list[float]]:
    from lib import prices
    px = prices.fetch(codes, market, count=400, need_open=False, quiet=True, offline=offline)
    out = {}
    for c in codes:
        bars = px.get(prices.tf(c, market)) or []
        out[c] = [float(b["close"]) for b in bars if b.get("close") is not None]
    return out


# ---------------------------------------------------------------- wave
def wave_zone(closes: list[float], bull: bool) -> dict:
    """乖离率 = 现价 / 均线 − 1。高位 +10%（熊）~+15%（牛），低位 −10%（牛）~−15%（熊）。"""
    p = closes[-1]
    m20, m60 = _ma(closes, 20), _ma(closes, 60)
    if m20 is None or m60 is None:
        return {"err": "K 线不足 60 根"}
    d20, d60 = p / m20 - 1, p / m60 - 1
    hi = 0.15 if bull else 0.10
    lo = -0.10 if bull else -0.15
    dev = max(d20, d60) if d60 > 0 else min(d20, d60)
    if dev >= hi:
        zone, act = "高位", "原则②：优先减仓（80%）；不减须有基本面强理由（≤20%）"
    elif dev <= lo:
        zone, act = "低位", "原则①：优先买入/加仓（90%）；不加须有基本面强理由（≤10%）"
    else:
        zone, act = "区间内", "持有，不操作（均值回归区）"
    slope60 = None
    if len(closes) >= 80:
        prev = sum(closes[-80:-20]) / 60
        slope60 = m60 / prev - 1
    return {"price": p, "ma20": m20, "ma60": m60, "dev20": d20, "dev60": d60,
            "hi": hi, "lo": lo, "zone": zone, "action": act, "ma60_20d_slope": slope60}


# ---------------------------------------------------------------- grid
def grid_levels(closes: list[float], mode: str, max_pos: float, step: float, n_up: int, n_dn: int) -> dict:
    """成长策略：60 日线 = 最大仓位一半，向上每档减、向下每档加。
    超跌反弹：年线 = 0 仓，年线下每档加，上穿年线清仓。"""
    p = closes[-1]
    base_n = 60 if mode == "growth" else 250
    base = _ma(closes, base_n)
    if base is None:
        return {"err": f"K 线不足 {base_n} 根"}
    levels = []
    if mode == "growth":
        half = max_pos / 2
        per_up, per_dn = half / n_up, half / n_dn
        for k in range(n_up, 0, -1):
            levels.append((base * (1 + step) ** k, half - per_up * k))
        levels.append((base, half))
        for k in range(1, n_dn + 1):
            levels.append((base * (1 - step) ** k, half + per_dn * k))
        # 现价落在哪两档之间 → 持有较保守一档
        target = half
        if p >= base:
            k = 0
            while k < n_up and p >= base * (1 + step) ** (k + 1):
                k += 1
            target = half - per_up * k
        else:
            k = 0
            while k < n_dn and p <= base * (1 - step) ** (k + 1):
                k += 1
            target = half + per_dn * k
    else:
        per = max_pos / n_dn
        levels.append((base, 0.0))
        for k in range(1, n_dn + 1):
            levels.append((base * (1 - step) ** k, per * k))
        k = 0
        while k < n_dn and p <= base * (1 - step) ** (k + 1):
            k += 1
        target = 0.0 if p >= base else per * k
    # 唯一需要止损的情形：均线由多转空（60 日线下穿 120 日线 / 年线拐头）
    m120 = _ma(closes, 120)
    warn = None
    if mode == "growth" and m120 is not None and _ma(closes, 60) < m120:
        warn = "60 日线已在 120 日线下方：均线转空头 = 成长网格唯一需要止损的情形"
    return {"price": p, "base": base, "base_n": base_n, "dev": p / base - 1,
            "levels": levels, "target_pos": target, "warn": warn}


# ---------------------------------------------------------------- odds
def odds(price: float, up: float, down: float, win: float) -> dict:
    """赔率 = (上限−现价)/(现价−下限)。2:1 先买一半，3:1 加满，≤1:1 不买。
    预期收益率 = 上行空间×胜率 + 下行空间×(1−胜率)，两端取极限价
    （作者校验例：高 30 低 15、2:1 买点 20 → 12.5%；高 45 低 15 → 20%，2023-03-18）。"""
    r = (up - price) / (price - down) if price > down else float("inf")
    px_2 = (up + 2 * down) / 3      # (up−x)/(x−down)=2
    px_3 = (up + 3 * down) / 4      # =3
    px_1 = (up + down) / 2
    er = (up / price - 1) * win + (down / price - 1) * (1 - win)
    if r <= 1:
        act = "赔率 ≤1:1：不能买（持有的考虑减仓）"
    elif r < 2:
        act = "1:1~2:1：放自选、设 2:1 价位提醒"
    elif r < 3:
        act = "2:1~3:1：先买一半，3:1 再加"
    else:
        act = "≥3:1：最佳成本，可加满（并可把高于 2:1 的同等确定性品种换过来）"
    return {"odds": r, "price_1to1": px_1, "price_2to1": px_2, "price_3to1": px_3,
            "exp_return": er, "action": act}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("wave", "grid"):
        s = sub.add_parser(name)
        s.add_argument("--market", required=True, help="CN / HK / US")
        s.add_argument("--codes", required=True)
        s.add_argument("--offline", action="store_true")
        s.add_argument("--json", action="store_true")
    sub.choices["wave"].add_argument("--bull", action="store_true", help="牛市口径（高位 +15%，低位 −10%）")
    g = sub.choices["grid"]
    g.add_argument("--mode", choices=["growth", "rebound"], default="growth")
    g.add_argument("--max-pos", type=float, default=30.0, help="该 ETF 最大仓位（%%），缺省 30")
    g.add_argument("--step", type=float, default=0.05, help="每档幅度，缺省 5%%")
    g.add_argument("--n-up", type=int, default=5)
    g.add_argument("--n-dn", type=int, default=5, help="rebound 模式作者用 7 档（−30%% 封底）")
    o = sub.add_parser("odds")
    o.add_argument("--price", type=float, required=True)
    o.add_argument("--up", type=float, required=True, help="向上极限价：乐观 EPS × 近三年持续 2-3 个月的最高估值")
    o.add_argument("--down", type=float, required=True, help="向下极限价：利好全不兑现 EPS × 历史最差估值")
    o.add_argument("--win", type=float, default=0.5, help="胜率，基准 0.5（新手 .45 / 高手 .6；黑马 .4）")
    a = ap.parse_args()

    if a.cmd == "odds":
        r = odds(a.price, a.up, a.down, a.win)
        print(f"赔率 {r['odds']:.2f}:1  → {r['action']}\n"
              f"  1:1 价 {r['price_1to1']:.3f} | 2:1 价 {r['price_2to1']:.3f} | 3:1 价 {r['price_3to1']:.3f}\n"
              f"  预期收益率 {r['exp_return']:.1%}（胜率 {a.win:.0%}，两端取极限价）")
        return

    codes = [c.strip() for c in a.codes.split(",") if c.strip()]
    data = _load(codes, a.market, a.offline)
    res = {}
    for c in codes:
        cl = data.get(c) or []
        if not cl:
            res[c] = {"err": "无行情"}
            continue
        res[c] = wave_zone(cl, a.bull) if a.cmd == "wave" else \
            grid_levels(cl, a.mode, a.max_pos, a.step, a.n_up, a.n_dn)
    if a.json:
        print(json.dumps(res, ensure_ascii=False, indent=1))
        return
    for c, r in res.items():
        if "err" in r:
            print(f"{c}: {r['err']}")
            continue
        if a.cmd == "wave":
            sl = f"，60 日线 20 日斜率 {r['ma60_20d_slope']:+.1%}" if r["ma60_20d_slope"] is not None else ""
            print(f"{c}  现价 {r['price']:.3f}  乖离 20d {r['dev20']:+.1%} / 60d {r['dev60']:+.1%}"
                  f"（阈值 {r['lo']:+.0%} / {r['hi']:+.0%}{sl}）\n  → {r['zone']}：{r['action']}")
        else:
            print(f"{c}  现价 {r['price']:.3f}  {r['base_n']} 日线 {r['base']:.3f}（乖离 {r['dev']:+.1%}）"
                  f"  → 目标仓位 {r['target_pos']:.1f}%")
            print("   " + "  ".join(f"{lv:.3f}:{pos:.0f}%" for lv, pos in r["levels"]))
            if r["warn"]:
                print(f"  ⚠️ {r['warn']}")


if __name__ == "__main__":
    main()
