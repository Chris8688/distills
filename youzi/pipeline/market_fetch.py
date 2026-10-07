#!/usr/bin/env python3
"""全市场 A 股不复权日线（含退市股）→ ~/.trade-strategy/distill/youzi/market/bars/<code>.parquet
baostock 多进程（每进程独立登录）。增量：已存在且覆盖到 end 的跳过。
用法: $PY market_fetch.py [--start 2009-06-01] [--end today] [--procs 6]
"""
import argparse
import os
from datetime import date
from multiprocessing import Pool
from pathlib import Path

import pandas as pd

OUT = Path.home() / ".trade-strategy/distill/youzi/market/bars"
FIELDS = "date,open,high,low,close,preclose,volume,amount,turn,tradestatus,pctChg,isST"


def work(args):
    codes, start, end = args
    import baostock as bs
    bs.login()
    n = 0
    for code in codes:
        f = OUT / f"{code[3:]}.parquet"
        if f.exists():
            try:
                if pd.read_parquet(f, columns=["date"])["date"].max() >= pd.Timestamp(end) - pd.Timedelta(days=7):
                    continue
            except Exception:
                pass
        rs = bs.query_history_k_data_plus(code, FIELDS, start_date=start, end_date=end, frequency="d", adjustflag="3")
        rows = []
        while rs.error_code == "0" and rs.next():
            rows.append(rs.get_row_data())
        if not rows:
            continue
        df = pd.DataFrame(rows, columns=rs.fields)
        df["date"] = pd.to_datetime(df["date"])
        for c in ["open", "high", "low", "close", "preclose", "volume", "amount", "turn", "pctChg"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df = df[df["tradestatus"] == "1"].drop(columns="tradestatus")
        df["isST"] = df["isST"] == "1"
        df.to_parquet(f, index=False)
        n += 1
    bs.logout()
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2009-06-01")
    ap.add_argument("--end", default=str(date.today()))
    ap.add_argument("--procs", type=int, default=6)
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    import baostock as bs
    bs.login()
    rs = bs.query_stock_basic()
    rows = []
    while rs.error_code == "0" and rs.next():
        rows.append(rs.get_row_data())
    bs.logout()
    basic = pd.DataFrame(rows, columns=rs.fields)
    basic = basic[basic["type"] == "1"]  # 股票
    basic.to_csv(OUT.parent / "stock_basic.csv", index=False)
    basic = basic[(basic["outDate"] == "") | (basic["outDate"] >= a.start)]
    codes = basic["code"].tolist()
    print(f"{len(codes)} stocks", flush=True)
    chunks = [codes[i::a.procs * 4] for i in range(a.procs * 4)]
    with Pool(a.procs) as p:
        done = 0
        for n in p.imap_unordered(work, [(c, a.start, a.end) for c in chunks]):
            done += n
            print(f"fetched {done}", flush=True)


if __name__ == "__main__":
    main()
