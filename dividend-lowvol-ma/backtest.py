"""红利低波均线（稳我才）—— 复现作者雪球回测，再做稳健性检验。

规则（原文 + 图3 雪球「择时策略」配置）：
  买入：收盘价 / MA(182) <= 1  且  PE <= 20      （全部匹配）
  卖出：收盘价 / MA(182) >  1.07
  标的：512890 红利低波ETF华泰柏瑞；空仓无替代资产；无止损

用法：
  python3 backtest.py signal             # 今天该持有还是空仓（512890 + 作者建议的 563020）
  python3 backtest.py replicate          # 与作者 25 笔调仓逐笔对照
  python3 backtest.py grid               # 参数网格（MA 长度 × 卖出乖离）看 182/7% 是否孤峰
  python3 backtest.py cross              # 同规则搬到 510880(2007 起)/159905 等，看熊市里不止损的代价
  python3 backtest.py split              # 2019-10 ~ 2023-03 定参 / 之后检验；发布日 2026-03-18 后实盘段
数据缓存在 ~/.cache/distill/dividend-lowvol-ma/（环境变量 DISTILL_CACHE；price: TickFlow；PE: 蛋卷 CSIH30269 周频）。
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from lib import CACHE          # noqa: E402
from lib import prices as P    # noqa: E402
START = "2019-10-22"          # 作者回测区间起点（图1）
END = "2026-09-29"            # 作者回测区间终点（图1）
PUBLISHED = "2026-03-18"      # 策略在雪球的发布日（图1）


def load_prices(adj: str = "forward") -> list[dict]:
    p = os.path.join(CACHE, f"512890_{adj}.json")
    if not os.path.exists(p):
        json.dump(P.klines("512890", 6000, adj), open(p, "w"))
    return json.load(open(p))


def load_pe() -> dict[str, float]:
    """蛋卷指数估值（周频）→ 前值填充到每个交易日。"""
    p = os.path.join(CACHE, "pe_CSIH30269.json")
    if not os.path.exists(p):
        import urllib.request
        req = urllib.request.Request(
            "https://danjuanfunds.com/djapi/index_eva/pe_history/CSIH30269?day=all",
            headers={"User-Agent": "Mozilla/5.0"})
        open(p, "wb").write(urllib.request.urlopen(req, timeout=30).read())
    rows = json.load(open(p))["data"]["index_eva_pe_growths"]
    return {dt.datetime.fromtimestamp(r["ts"] / 1000 + 8 * 3600, dt.UTC).strftime("%Y-%m-%d"): r["pe"]
            for r in rows}


def run(bars, *, ma=182, buy_dev=1.0, sell_dev=1.07, pe_max=20.0, pe=None,
        start=START, end=END, fill="close", fee=0.0):
    """返回 (交易列表, 日净值序列)。fill='close' 当日收盘成交；'next_open' 次日开盘。"""
    closes = [b["close"] for b in bars]
    pe_last, pe_by_day = None, {}
    for b in bars:
        if pe and b["date"] in pe:
            pe_last = pe[b["date"]]
        pe_by_day[b["date"]] = pe_last
    trades, nav, eq, pos, entry = [], [], 1.0, 0, None
    s = 0.0
    for i, b in enumerate(bars):
        s += closes[i]
        if i >= ma:
            s -= closes[i - ma]
        if b["date"] < start or b["date"] > end:
            continue
        if pos and i > 0:
            eq *= closes[i] / closes[i - 1]
        nav.append((b["date"], eq))
        if i < ma - 1:
            continue
        r = closes[i] / (s / ma)
        px_i = i + 1 if fill == "next_open" and i + 1 < len(bars) else i
        px = bars[px_i]["open"] if fill == "next_open" and px_i != i else closes[i]
        if not pos and r <= buy_dev and (pe is None or (pe_by_day[b["date"]] or 0) <= pe_max):
            pos, entry = 1, px
            eq *= (1 - fee) * (closes[i] / px if fill == "next_open" else 1)
            trades.append((bars[px_i]["date"], "BUY", px, r))
        elif pos and r > sell_dev:
            pos = 0
            eq *= (1 - fee) * (px / closes[i] if fill == "next_open" else 1)
            trades.append((bars[px_i]["date"], "SELL", px, r))
    return trades, nav


def stats(nav):
    if len(nav) < 2:
        return {}
    d0, d1 = dt.date.fromisoformat(nav[0][0]), dt.date.fromisoformat(nav[-1][0])
    yrs = (d1 - d0).days / 365.25
    tot = nav[-1][1] / nav[0][1]
    peak, mdd = 0, 0
    for _, v in nav:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    return {"total": tot - 1, "cagr": tot ** (1 / yrs) - 1, "mdd": mdd}


def author_trades():
    return list(csv.DictReader(open(os.path.join(HERE, "data", "trades.csv"))))


def cmd_replicate():
    fwd, raw = load_prices("forward"), load_prices("none")
    raw_px = {b["date"]: b["close"] for b in raw}
    pe = load_pe()
    for fill in ("close", "next_open"):
        tr, nav = run(fwd, pe=pe, fill=fill)
        st = stats(nav)
        print(f"\n== 成交口径 {fill}：{len(tr)} 笔，总收益 {st['total']:+.1%} 年化 {st['cagr']:+.2%} 最大回撤 {st['mdd']:.2%}")
        au = author_trades()
        match = 0
        print(f"{'作者日期':<11}{'方向':<5}{'作者价':>7} | {'复现日期':<11}{'未复权收盘':>9}{'乖离':>7}  差(交易日)")
        dates = [b["date"] for b in fwd]
        for k in range(max(len(au), len(tr))):
            a = au[k] if k < len(au) else None
            t = tr[k] if k < len(tr) else None
            lag = ""
            if a and t and a["side"] == t[1]:
                lag = dates.index(t[0]) - dates.index(a["date"]) if a["date"] in dates else "?"
                match += lag == 0
            print(f"{(a or {}).get('date',''):<11}{(a or {}).get('side',''):<5}{(a or {}).get('price',''):>7} | "
                  f"{(t[0] if t else ''):<11}{(raw_px.get(t[0], 0) if t else 0):>9.3f}{(t[3] if t else 0):>7.3f}  {lag}")
        print(f"日期完全一致 {match}/{len(au)}")
    tr0, nav0 = run(fwd, pe=None)
    print(f"\n去掉 PE 条件：{len(tr0)} 笔，与带 PE 的成交{'完全相同' if tr0 == run(fwd, pe=pe)[0] else '不同'}"
          f"（2019 年后 PE 区间 {min(v for k, v in pe.items() if k >= '2019'):.2f}~{max(v for k, v in pe.items() if k >= '2019'):.2f}）")
    bh = stats([(b["date"], b["close"]) for b in fwd if START <= b["date"] <= END])
    print(f"同期买入持有（前复权）：总收益 {bh['total']:+.1%} 年化 {bh['cagr']:+.2%} 最大回撤 {bh['mdd']:.2%}")


def cmd_grid():
    fwd = load_prices("forward")
    mas = [60, 90, 120, 150, 170, 182, 200, 250]
    devs = [1.03, 1.05, 1.07, 1.09, 1.11, 1.15]
    print("年化（行：MA 长度，列：卖出乖离）；括号为最大回撤")
    print("MA\\卖 " + "".join(f"{d:>14}" for d in devs))
    for m in mas:
        row = []
        for d in devs:
            st = stats(run(fwd, ma=m, sell_dev=d)[1])
            row.append(f"{st['cagr']:+.1%}({st['mdd']:.0%})")
        print(f"{m:<6}" + "".join(f"{x:>14}" for x in row))
    bh = stats([(b["date"], b["close"]) for b in fwd if START <= b["date"] <= END])
    print(f"买入持有：年化 {bh['cagr']:+.1%} 回撤 {bh['mdd']:.0%}")


def _slice(nav, s, e):
    return [x for x in nav if s <= x[0] <= e]


def cmd_split():
    """策略按全段连续运行（带着持仓状态进入各段），再切片统计。"""
    fwd = load_prices("forward")
    _, full = run(fwd)
    segs = [("全段", START, END), ("前半 2019-10~2023-03", START, "2023-03-31"),
            ("后半 2023-04~2026-09", "2023-04-01", END), ("发布后 2026-03-18~", PUBLISHED, END)]
    for name, s, e in segs:
        st = stats(_slice(full, s, e))
        bh = stats([(b["date"], b["close"]) for b in fwd if s <= b["date"] <= e])
        print(f"{name:<22} 策略 年化 {st['cagr']:+.1%} 回撤 {st['mdd']:.1%} | 买持 年化 {bh['cagr']:+.1%} 回撤 {bh['mdd']:.1%}")


def trade_detail(bars, trades):
    """逐笔：持有天数、收益、持有期内最大浮亏（按收盘）。"""
    idx = {b["date"]: i for i, b in enumerate(bars)}
    out = []
    for k in range(0, len(trades), 2):
        b = trades[k]
        sl = trades[k + 1] if k + 1 < len(trades) else (bars[-1]["date"], "OPEN", bars[-1]["close"], 0)
        i0, i1 = idx[b[0]], idx[sl[0]]
        worst = min(x["close"] for x in bars[i0:i1 + 1]) / b[2] - 1
        days = (dt.date.fromisoformat(sl[0]) - dt.date.fromisoformat(b[0])).days
        out.append((b[0], sl[0], days, sl[2] / b[2] - 1, worst, sl[1] == "OPEN"))
    return out


def cmd_cross():
    """同一规则（182 / 7%，无 PE）搬到更长历史的红利类 ETF —— 样本外资产 + 熊市检验。"""
    names = {"510880": "红利ETF 华泰柏瑞（2007 起）", "159905": "深红利 ETF（2011 起）",
             "515080": "中证红利 ETF 招商", "515180": "红利 ETF 易方达", "512890": "红利低波 ETF 华泰柏瑞"}
    for code, name in names.items():
        bars = load_prices_code(code)
        start = bars[min(len(bars) - 1, 182)]["date"]
        tr, nav = run(bars, fill="next_open", start=start, end=bars[-1]["date"])
        st = stats(nav)
        bh = stats([(b["date"], b["close"]) for b in bars if b["date"] >= start])
        det = trade_detail(bars, tr)
        closed = [d for d in det if not d[5]]
        wins = sum(d[3] > 0 for d in closed)
        print(f"\n== {code} {name}  {start} ~ {bars[-1]['date']}")
        print(f"   策略 年化 {st['cagr']:+.1%} 最大回撤 {st['mdd']:.1%} | 买持 年化 {bh['cagr']:+.1%} 最大回撤 {bh['mdd']:.1%}")
        print(f"   完成 {len(closed)} 笔，盈利 {wins} 笔；平均持有 {sum(d[2] for d in closed)/max(1,len(closed)):.0f} 天，"
              f"最长 {max((d[2] for d in det), default=0)} 天；单笔最大浮亏 {min((d[4] for d in det), default=0):.1%}")
        for d in sorted(det, key=lambda x: x[4])[:3]:
            print(f"     浮亏最深：{d[0]} → {d[1]}{'（未平）' if d[5] else ''} 持有 {d[2]} 天，期间最低 {d[4]:.1%}，结果 {d[3]:+.1%}")


def load_prices_code(code: str) -> list[dict]:
    p = os.path.join(CACHE, f"{code}_forward.json")
    if not os.path.exists(p):
        json.dump(P.klines(code, 6000, "forward"), open(p, "w"))
    return json.load(open(p))


def cmd_signal():
    """今天的状态：按 182 日均线乖离判断该持有还是空仓（重抓最新行情）。"""
    for code, name in (("512890", "红利低波ETF华泰柏瑞（作者回测标的）"), ("563020", "红利低波ETF易方达（作者建议实盘用，费率低）")):
        bars = P.klines(code, 400, "forward")
        if len(bars) < 183:
            print(f"{code} {name}：K 线不足 183 根（{len(bars)}），无法判断")
            continue
        tr, _ = run(bars, start=bars[182]["date"], end=bars[-1]["date"])
        closes = [b["close"] for b in bars]
        ma = sum(closes[-182:]) / 182
        r = closes[-1] / ma
        state = "持有" if tr and tr[-1][1] == "BUY" else "空仓"
        last = f"上次信号 {tr[-1][0]} {tr[-1][1]}" if tr else "近 400 根内无信号"
        hint = ("乖离 >1.07 → 下一交易日卖出" if state == "持有" and r > 1.07 else
                "乖离 ≤1 → 下一交易日买入" if state == "空仓" and r <= 1.0 else "无操作")
        print(f"{code} {name}\n  {bars[-1]['date']} 收盘 {closes[-1]:.3f} · MA182 {ma:.3f} · 乖离 {r:.3f}"
              f"（买 ≤1.000 / 卖 >1.070）\n  状态：{state}（{last}）→ {hint}")


if __name__ == "__main__":
    {"signal": cmd_signal, "replicate": cmd_replicate, "grid": cmd_grid, "split": cmd_split, "cross": cmd_cross}[(sys.argv[1:] or ["replicate"])[0]]()
