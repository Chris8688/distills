#!/usr/bin/env python3
"""A 股情绪周期面板：最近 N 个交易日的读数 + 阶段 + 仓位上限 + 各状态的历史次日表现。只读，不下单。

用法:
  python3 panel.py                 # 先增量更新行情（TickFlow，约 1 分钟），再出最近 10 日
  python3 panel.py --days 20 --no-update
  python3 panel.py --date 2024-02-05 --no-update    # 回看某天
  python3 panel.py --json
依赖：先跑过一次全量 `python3 -c "import sys;sys.path.insert(0,'lib');import market;market.update(full=True)"`（约 30 分钟）
"""
import argparse
import json
import sys
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "lib"))
import market  # noqa: E402
import sentiment as S  # noqa: E402

READ = market.CACHE / "sentiment_daily.parquet"
HIST = {  # validate.py T1 全样本（2010-01 ~ 2026-09）：各状态当日打首板的次日收盘均值；重跑 validate.py 后同步
    "S1": -0.0166, "S1c": 0.0031, "S2": -0.0062, "S3": -0.0042, "S4": -0.0030, "S5": -0.0071, "S6": -0.0080, "ALL": -0.0064}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=10)
    ap.add_argument("--date")
    ap.add_argument("--no-update", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    if not a.no_update:
        market.update(log=lambda s: None)
    if READ.exists() and READ.stat().st_mtime > market.DAILY.stat().st_mtime:
        x = pd.read_parquet(READ)
    else:
        x = S.daily(S.annotate(market.load()))
        x.to_parquet(READ)
    x = S.phases(x)
    if a.date:
        x = x.loc[:a.date]
    w = x.tail(a.days)
    if a.json:
        cols = ["state", "state_cn", "cap", "WEAK_MKT", "ZT", "DT", "ZBR", "H", "LB", "P1", "P2", "HIr", "HId", "BR", "AMT", "pZT", "pDT"]
        print(json.dumps({str(k.date()): {c: (None if pd.isna(v) else (v.item() if hasattr(v, "item") else v)) for c, v in r[cols].items()}
                          for k, r in w.iterrows()}, ensure_ascii=False, indent=1))
        return
    print(f"情绪周期面板 · 数据截至 {x.index[-1].date()}（全市场 {int(x['N'].iloc[-1])} 只，剔除上市 5 日内新股）\n")
    print("| 日期 | 状态 | 上限 | 弱市 | 涨停 | 跌停 | 炸板率 | 最高板 | 连板 | 昨涨停今日 P1 | 昨连板今日 P2 | 高位≥3板今日 | 涨跌比 | 成交(亿) |")
    print("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for k, r in w.iterrows():
        print(f"| {k.date()} | {r.state} {r.state_cn} | {r.cap * 100:.0f}% | {'是' if r.WEAK_MKT else ''} | {int(r.ZT)} | {int(r.DT)} | "
              f"{r.ZBR * 100:.0f}% | {int(r.H)} | {int(r.LB)} | {r.P1 * 100:+.2f}% | {r.P2 * 100:+.2f}% | {r.HIr * 100:+.2f}%（跌停{r.HId * 100:.0f}%）| "
              f"{r.BR:.2f} | {r.AMT:,.0f} |")
    last = w.iloc[-1]
    print(f"\n今日状态 **{last.state} {last.state_cn}**：历史上该状态当天打首板（全部首板尝试、涨停价买、次日收盘卖）"
          f"次日平均 {HIST.get(last.state, float('nan')) * 100:+.2f}%（全样本 {HIST['ALL'] * 100:+.2f}%）。")
    print("仓位上限是状态机给的**上限**（只做负向约束），不是加仓信号；阈值多为待校准，见 docs/01 §3、docs/02。")


if __name__ == "__main__":
    main()
