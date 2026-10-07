#!/usr/bin/env python3
"""从 OCR / pdftotext 文本里抽成交行 → 双重校验 → 人物/<name>/trades/ocr_fills.csv（trades.py 自动并入）。

一行算成交，需要：6 位 A 股代码 + 买/卖方向词 + 能配平的数字。
  校验 1（自洽）：在数字里找 (价, 量, 额) 使 |价×量 − 额| ≤ 2%·额；找不到额时退化为 (价, 量)，标 quality=pair
  校验 2（行情）：价格必须落在该日 baostock 不复权 [最低, 最高] 内（±0.5%），否则丢弃
日期：行内有就用行内（YYYYMMDD / YYYY-MM-DD / YYYY.M.D）；没有就沿用上文最近出现的日期（quality 加 ctx）
有原始 xls 的人物：只收原始成交覆盖区间之外的 OCR 行（避免重复）
用法: $PY ocr_trades.py [--person 赵老哥] [--report]
"""
import argparse
import json
import re
import sys
import os
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from enrich import fetch  # noqa: E402

HERE = Path(__file__).resolve().parent
DISTILL = Path(os.environ.get("YOUZI_CORPUS", Path.home() / "Baidu-Pan/游资心法+讲座全集1/distill"))  # 语料库（本机，不进 git）
ROOT = DISTILL.parent
UNVERIFIED = []  # 行情拉取失败、无法校验的代码（不是 OCR 错，是数据源问题）

DATE = re.compile(r"(?<!\d)(20[012]\d)\s?[-/.年]?\s?(0?[1-9]|1[0-2])\s?[-/.月]?\s?(0?[1-9]|[12]\d|3[01])(?!\d)")
DATE8 = re.compile(r"(?<!\d)(20[012]\d)(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])(?!\d)")
TIME = re.compile(r"(?<!\d)([01]?\d|2[0-3])[:：]([0-5]\d)(?:[:：]([0-5]\d))?(?!\d)")
CODE = re.compile(r"(?<![\d.])((?:00|30|60|68)\d{4})(?![\d]|\.\d)")
NUM = re.compile(r"-?\d+(?:\.\d+)?")
BUY = re.compile(r"买入|买人|融资买|证券买|^买|\s买\s|买$")
SELL = re.compile(r"卖出|卖券|证券卖|^卖|\s卖\s|卖$")


def norm_line(s: str) -> str:
    s = s.translate(str.maketrans("０１２３４５６７８９．，：－", "0123456789.,:-"))
    s = re.sub(r"(?<=\d),(?=\d{3})", "", s)  # 千分位
    return s


def find_date(s: str):
    m = DATE8.search(s)
    if m:
        y, mo, d = m.groups()
    else:
        m = DATE.search(s)
        if not m:
            return None, None
        y, mo, d = m.groups()
    try:
        return pd.Timestamp(int(y), int(mo), int(d)), m.span()
    except ValueError:
        return None, None


def parse_line(s: str, ctx_date):
    if not CODE.search(s):
        return None
    b, sl = bool(BUY.search(s)), bool(SELL.search(s))
    if b == sl:
        return None
    side = "B" if b else "S"
    d, dspan = find_date(s)
    q = ""
    if d is None:
        if ctx_date is None:
            return None
        d, q = ctx_date, "ctx"
    s2 = s
    if dspan:
        s2 = s[:dspan[0]] + " " * (dspan[1] - dspan[0]) + s[dspan[1]:]
    tm = TIME.search(s2)
    t = ""
    if tm:
        t = f"{int(tm.group(1)):02d}:{tm.group(2)}:{tm.group(3) or '00'}"
        s2 = s2[:tm.start()] + " " * (tm.end() - tm.start()) + s2[tm.end():]
    cm = CODE.search(s2)
    code = cm.group(1)
    after = s2[cm.end():]
    name = re.split(r"\s|证券|买|卖|融资", after.strip(), maxsplit=1)[0][:6]
    nums = [float(x) for x in NUM.findall(after)]
    nums = [abs(x) for x in nums]
    best = None
    for i, p in enumerate(nums):
        if not (0.5 <= p <= 1000):
            continue
        for j, qty in enumerate(nums):
            if j == i or qty < 100 or qty != int(qty):
                continue
            for k, a in enumerate(nums):
                if k in (i, j) or a <= 0:
                    continue
                if abs(p * qty - a) <= 0.02 * a:
                    best = (p, qty, a, "triple")
                    break
            if best:
                break
        if best:
            break
    if not best:
        # 退化：价（有小数）+ 量（100 的整数倍）
        ps = [x for x in nums if 0.5 <= x <= 1000 and x != int(x)]
        qs = [x for x in nums if x >= 100 and x == int(x) and int(x) % 100 == 0]
        if ps and qs:
            best = (ps[0], qs[0], ps[0] * qs[0], "pair")
    if not best:
        return None
    p, qty, a, qual = best
    return {"date": d, "time": t, "code": code, "name": name, "op": "证券买入" if side == "B" else "证券卖出",
            "price": p, "qty": qty, "amount": a, "quality": qual + ("+" + q if q else "")}


def scan_text(path: Path):
    rows, ctx = [], None
    for line in path.read_text(errors="ignore").splitlines():
        s = norm_line(line)
        r = parse_line(s, ctx)
        if r:
            r["src"] = "ocr:" + path.name
            rows.append(r)
        d, _ = find_date(s)
        if d is not None and pd.Timestamp("2005-01-01") <= d <= pd.Timestamp("2026-12-31"):
            ctx = d
    return rows


def fuzzy_dedupe(df: pd.DataFrame) -> pd.DataFrame:
    """同一笔成交在多份文档里被 OCR 多次、读数略有出入 → 按 (日期, 代码, 数量, 时间) 合并，方向/价格投票。
    无时间的行若与同 (日期, 代码, 数量) 的有时间行价格相差 <1%，视为同一笔丢弃。"""
    df = df.copy()
    df["qrank"] = df["quality"].str.startswith("triple").map({True: 0, False: 1})
    out = []
    for _, g in df.groupby(["date", "code", "qty"], sort=False):
        timed, untimed = g[g["time"] != ""], g[g["time"] == ""]
        for _, h in timed.groupby("time"):
            side = h["op"].mode().iloc[0]
            h2 = h[h["op"] == side].sort_values("qrank")
            r = h2.iloc[0].copy()
            r["price"] = h2["price"].mode().iloc[0]
            r["amount"] = r["price"] * r["qty"]
            out.append(r)
        tp = timed["price"].tolist()
        for _, h in untimed.groupby("op"):
            for _, r in h.sort_values("qrank").drop_duplicates("price").iterrows():
                if any(abs(r["price"] - x) <= 0.01 * x for x in tp):
                    continue
                tp.append(r["price"])
                out.append(r)
    return pd.DataFrame(out).drop(columns="qrank").reset_index(drop=True)


def validate(df: pd.DataFrame) -> pd.DataFrame:
    keep = []
    for code, g in df.groupby("code"):
        s = (g["date"].min() - pd.Timedelta(days=5)).date()
        e = (g["date"].max() + pd.Timedelta(days=5)).date()
        try:
            b = fetch(code, str(s), str(e))
        except RuntimeError:
            UNVERIFIED.append(code)
            continue
        if b is None or b.empty:
            continue
        bi = b.set_index("date")
        for r in g.itertuples():
            if r.date not in bi.index:
                continue
            lo, hi = bi.at[r.date, "low"], bi.at[r.date, "high"]
            if lo * 0.995 <= r.price <= hi * 1.005:
                keep.append(r.Index)
    return df.loc[keep]


def raw_range(person: str):
    f = DISTILL / "人物" / person / "trades" / "trades.csv"
    if not f.exists():
        return None
    t = pd.read_csv(f, parse_dates=["date"])
    t = t[~t["src"].astype(str).str.startswith("ocr:")]
    if t.empty:
        return None
    return t["date"].min(), t["date"].max()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--person")
    ap.add_argument("--min", type=int, default=20, help="候选行少于该数的人物跳过")
    a = ap.parse_args()
    people = [a.person] if a.person else sorted(p.name for p in (DISTILL / "人物").iterdir() if (p / "text").exists())
    report = {}
    for person in people:
        rows = []
        for f in sorted((DISTILL / "人物" / person / "text").glob("*.md")):
            rows += scan_text(f)
        if len(rows) < a.min:
            continue
        df = pd.DataFrame(rows)
        df = df[(df["date"] >= "2005-01-01") & (df["date"] <= "2026-12-31")]
        n0 = len(df)
        df = fuzzy_dedupe(df)
        n1 = len(df)
        df = validate(df)
        rr = raw_range(person)
        if rr:
            df = df[(df["date"] < rr[0]) | (df["date"] > rr[1])]
        out = DISTILL / "人物" / person / "trades"
        out.mkdir(parents=True, exist_ok=True)
        if df.empty:
            (out / "ocr_fills.csv").unlink(missing_ok=True)
            report[person] = {"candidates": n0, "dedup": n1, "valid": 0}
            print(f"{person}: 候选 {n0} → 去重 {n1} → 校验通过 0")
            continue
        df = df.sort_values(["date", "time"])
        df["date"] = df["date"].dt.strftime("%Y%m%d")
        df.to_csv(out / "ocr_fills.csv", index=False)
        q = df["quality"].value_counts().to_dict()
        report[person] = {"candidates": n0, "dedup": n1, "valid": int(len(df)), "quality": q,
                          "period": [df["date"].min(), df["date"].max()]}
        print(f"{person}: 候选 {n0} → 去重 {n1} → 校验通过 {len(df)} {q} {df['date'].min()}~{df['date'].max()}"
              + (f" ⚠️ 行情拉取失败 {len(UNVERIFIED)} 只" if UNVERIFIED else ""))
        report[person]["unverified_codes"] = list(UNVERIFIED)
        UNVERIFIED.clear()
    (DISTILL / "data").mkdir(exist_ok=True)
    (DISTILL / "data" / "ocr_trades_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
