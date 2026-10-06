"""给「红利低波均线」修下跌出口 —— 同一套参数在 5 只红利 ETF 上检验（不按标的调参）。

原规则的 bug：仓位只有 0/100%，卖出只有「涨过均线 7%」一条 → 单边下跌里满仓到底
（510880 2008 年 −68%，159905 2021~2022 年 −41%）。

检验过、**没用**的修补（详见 docs/02-修补检验.md）：
  · 绝对 PE ≤ 20（原文）       红利低波 PE 常年 5~9 从不触发；只在 2007~08 年的上证红利上把买点推迟了 2 个月
                                （保留：无害，且 v2 里把 510880 回撤 −29% → −24%）
  · 相对 PE（自身 3 年分位）    低波三只年化掉 5~8pp，510880 回撤仍 −62%~−70%
  · 均线方向 / 长均线（250/500）过滤、跌破均线 x% 止损、回撤止损、超时止损
                                同起点比较下 510880 回撤多数仍 −47%~−76%；能压到 −26% 的组合（MA 方向 + PE）
                                让 515080/515180 年化掉 5pp；159905 来回打脸，阈值一挪结论就翻
  · 涨过 7% 后改回撤止盈       159905 变好，但 512890/515080/515180 大幅变差 —— 是换资产类型，不是修 bug

采用的修补 v2：**信号不变，仓位按波动率缩放**
  持有时仓位 = min(1, 目标波动 / 近 60 日年化波动)，目标波动默认 12%；
  仓位偏离 >10 个百分点或每 5 个交易日（偏离 >2pp）再平衡；空仓/建仓/清仓照原信号次日开盘。
  —— 没有开关阈值，参数单调（目标波动越低越保守），在 5 只上同向改善回撤。

用法：
  python3 improve.py signal         # 今日：6 只（含 563020）原规则状态 + v2 目标仓位
  python3 improve.py compare        # 5 只 × {买持, 原规则, 原规则+PE, v2(8/10/12/15/18%)}，含费用
  python3 improve.py oos            # 2018 年底前（510880/159905）看 → 2019 起全部 5 只验
  python3 improve.py year 510880    # v2 与原规则逐年对比
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import backtest as B  # noqa: E402

ETFS = {   # 代码: (名称, 蛋卷指数代码)
    "512890": ("红利低波 华泰柏瑞", "CSIH30269"),
    "515080": ("中证红利 招商", "SH000922"),
    "515180": ("红利 易方达", "SH000922"),
    "510880": ("上证红利 华泰柏瑞", "SH000015"),
    "159905": ("深证红利 工银", "SZ399324"),
}
FEE = 0.0003          # 单边佣金+冲击（ETF 免印花税），按换手计
TV = 0.12             # v2 缺省目标波动


def load_pe(index: str) -> dict[str, float]:
    """日期 → PE(TTM)。蛋卷 2016 起周频；上证红利再并上乐咕乐股 2005 起月度（akshare stock_index_pe_lg）。"""
    out: dict[str, float] = {}
    p = os.path.join(B.CACHE, f"pe_{index}.json")
    if not os.path.exists(p):
        import urllib.request
        req = urllib.request.Request(
            f"https://danjuanfunds.com/djapi/index_eva/pe_history/{index}?day=all",
            headers={"User-Agent": "Mozilla/5.0"})
        open(p, "wb").write(urllib.request.urlopen(req, timeout=30).read())
    for r in json.load(open(p))["data"]["index_eva_pe_growths"]:
        out[dt.datetime.fromtimestamp(r["ts"] / 1000 + 8 * 3600, dt.UTC).strftime("%Y-%m-%d")] = r["pe"]
    lg = os.path.join(B.CACHE, "pe_lg_上证红利.csv")
    if index == "SH000015":
        if not os.path.exists(lg):
            import akshare as ak
            ak.stock_index_pe_lg(symbol="上证红利").to_csv(lg, index=False)
        first = min(out) if out else "9999"
        for row in csv.DictReader(open(lg)):
            if row["日期"] < first:
                out[row["日期"]] = float(row["滚动市盈率"])
    return out


def sma(x: list[float], n: int) -> list[float | None]:
    out, s = [], 0.0
    for i, c in enumerate(x):
        s += c
        if i >= n:
            s -= x[i - n]
        out.append(s / n if i >= n - 1 else None)
    return out


def realized_vol(c: list[float], n: int = 60) -> list[float | None]:
    r = [0.0] + [math.log(c[i] / c[i - 1]) for i in range(1, len(c))]
    out: list[float | None] = [None] * len(c)
    for i in range(n, len(c)):
        w = r[i - n + 1:i + 1]
        mu = sum(w) / n
        out[i] = math.sqrt(sum((x - mu) ** 2 for x in w) / (n - 1) * 252)
    return out


def run(bars, *, ma=182, buy=1.0, sell=1.07, pe=None, pe_max=20.0,
        tv=None, vol_n=60, band=0.10, rebal=5, fee=FEE, start=None, end=None):
    """返回 (nav, 年均换手, 最新状态)。tv=None → 原规则（满仓/空仓）；tv=x → v2 波动率缩放。
    信号按当日收盘算，次日开盘调到目标仓位。"""
    c = [b["close"] for b in bars]
    m = sma(c, ma)
    v = realized_vol(c, vol_n) if tv else None
    pe_now, pe_ok = None, []
    for b in bars:
        if pe and b["date"] in pe:
            pe_now = pe[b["date"]]
        pe_ok.append(pe is None or pe_now is None or pe_now <= pe_max)
    start = start or bars[ma]["date"]
    end = end or bars[-1]["date"]
    eq, w, sig, pend, nav, turn = 1.0, 0.0, 0, None, [], 0.0
    state = {}
    for i, b in enumerate(bars):
        if b["date"] < start or b["date"] > end or m[i] is None:
            continue
        if pend is not None and i > 0:
            eq *= 1 + w * (b["open"] / c[i - 1] - 1)
            eq *= 1 - fee * abs(pend - w)
            eq *= 1 + pend * (c[i] / b["open"] - 1)
            turn += abs(pend - w)
            w, pend = pend, None
        elif nav:
            eq *= 1 + w * (c[i] / c[i - 1] - 1)
        nav.append((b["date"], eq))
        r = c[i] / m[i]
        if sig and r > sell:
            sig = 0
        elif not sig and r <= buy and pe_ok[i]:
            sig = 1
        tgt = 0.0 if not sig else (min(1.0, tv / v[i]) if (tv and v[i]) else 1.0)
        if (tgt == 0) != (w == 0) or abs(tgt - w) > band or (i % rebal == 0 and abs(tgt - w) > 0.02):
            pend = tgt
        state = {"date": b["date"], "close": c[i], "ma": m[i], "dev": r, "signal": sig,
                 "vol": v[i] if v else None, "target_weight": tgt, "weight": w}
    yrs = max(1e-9, len(nav) / 244)
    return nav, turn / yrs, state


def _row(nav):
    st = B.stats(nav)
    return st["cagr"], st["mdd"], st["cagr"] / abs(st["mdd"]) if st["mdd"] else float("nan")


def _data():
    return {k: (B.load_prices_code(k), load_pe(ix)) for k, (_, ix) in ETFS.items()}


CONFIGS = [("买持", None), ("原规则", {}), ("原规则+PE≤20", {"pe": True}),
           ("v2 目标波动8%", {"tv": 0.08}), ("v2 10%", {"tv": 0.10}), ("v2 12%（缺省）", {"tv": 0.12}),
           ("v2 15%", {"tv": 0.15}), ("v2 18%", {"tv": 0.18}), ("v2 12% + PE≤20", {"tv": 0.12, "pe": True})]


def _eval(data, cfg, start=None, end=None):
    out = {}
    for k, (bars, pe) in data.items():
        s = max(start or "", bars[182]["date"])
        if cfg is None:
            nav = [(b["date"], b["close"]) for b in bars if s <= b["date"] <= (end or "9999")]
            t = 0.0
        else:
            kw = dict(cfg)
            if kw.pop("pe", False):
                kw["pe"] = pe
            nav, t, _ = run(bars, start=s, end=end, **kw)
        out[k] = (*_row(nav), t)
    return out


def _print(title, data, start=None, end=None, codes=None):
    codes = codes or list(ETFS)
    print(f"\n{title}  —— 每格：年化 / 最大回撤 / Calmar；末两列：5 只最差 Calmar、Calmar 不低于买持的只数")
    print(f"{'':<16}" + "".join(f"{k:>22}" for k in codes))
    sub = {k: data[k] for k in codes}
    bh = _eval(sub, None, start, end)
    for name, cfg in CONFIGS:
        res = bh if cfg is None else _eval(sub, cfg, start, end)
        cells = [f"{res[k][0]:+.1%}/{res[k][1]:.0%}/{res[k][2]:.2f}" for k in codes]
        beat = sum(res[k][2] >= bh[k][2] - 1e-9 for k in codes)
        print(f"{name:<16}" + "".join(f"{x:>22}" for x in cells)
              + f"{min(res[k][2] for k in codes):>8.2f}{beat:>4}/{len(codes)}")


def cmd_compare():
    data = _data()
    _print(f"全样本（各自上市满 182 日起，费用 {FEE:.2%}/单边换手）", data)


def cmd_oos():
    data = _data()
    _print("训练段：2018-12-31 以前（只有 510880 / 159905 有数据）", data, end="2018-12-31", codes=["510880", "159905"])
    _print("检验段：2019-01-01 起（全部 5 只）", data, start="2019-01-01")


def cmd_year(code="510880"):
    bars, pe = B.load_prices_code(code), load_pe(ETFS[code][1])
    navs = {"买持": [(b["date"], b["close"]) for b in bars[182:]],
            "原规则": run(bars)[0], f"v2 {TV:.0%}": run(bars, tv=TV)[0]}
    years = sorted({d[:4] for d, _ in navs["买持"]})
    print(f"{code} {ETFS[code][0]} 逐年收益 / 年内最大回撤")
    print(f"{'年':<6}" + "".join(f"{k:>20}" for k in navs))
    for y in years:
        cells = []
        for k, nav in navs.items():
            seg = [x for x in nav if x[0][:4] == y]
            prev = [x for x in nav if x[0][:4] < y]
            base = prev[-1][1] if prev else seg[0][1]
            peak, mdd = base, 0.0
            for _, val in seg:
                peak = max(peak, val)
                mdd = min(mdd, val / peak - 1)
            cells.append(f"{seg[-1][1] / base - 1:+.1%} / {mdd:.0%}")
        print(f"{y:<6}" + "".join(f"{x:>20}" for x in cells))


def cmd_signal():
    """今日：原规则状态 + v2 目标仓位（重抓行情；563020 用自身 K 线、PE 用红利低波指数）。"""
    rows = dict(ETFS)
    rows["563020"] = ("红利低波 易方达（作者建议实盘）", "CSIH30269")
    print(f"{'代码':<8}{'名称':<18}{'日期':<12}{'乖离':>7}{'60日波动':>9}{'原规则':>8}{'v2 目标仓位':>12}")
    for code, (name, ix) in rows.items():
        bars = B.P.klines(code, 600, "forward")
        if len(bars) < 250:
            print(f"{code:<8}{name:<18}K 线不足（{len(bars)}）")
            continue
        _, _, st = run(bars, tv=TV, pe=load_pe(ix))
        print(f"{code:<8}{name:<18}{st['date']:<12}{st['dev']:>7.3f}{(st['vol'] or 0):>9.1%}"
              f"{('持有' if st['signal'] else '空仓'):>8}{st['target_weight']:>12.0%}")
    print(f"\n规则：乖离=收盘/MA182，≤1.000 买、>1.070 卖（买入另需指数 PE≤20）；"
          f"v2 持有时仓位 = min(1, {TV:.0%}/60日波动)。信号按收盘算，次日开盘执行。")


if __name__ == "__main__":
    a = sys.argv[1:] or ["compare"]
    {"compare": cmd_compare, "oos": cmd_oos, "signal": cmd_signal,
     "year": lambda: cmd_year(*(a[1:2] or ["510880"]))}[a[0]]()
