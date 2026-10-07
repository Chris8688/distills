#!/usr/bin/env python3
"""给 episodes 接不复权日线 → 判定买点类型 / 连板位置 / 封板与次日溢价，按类型拆胜率与盈亏。

买点类型（看每段首笔买入，相对 T-1 收盘 pc 与当日涨停价 lu）:
  竞价    : 首笔时间 < 09:30
  打板    : 买价 >= lu - 0.01（涨停价或封板瞬间）
  半路    : 涨幅 >= 3% 且未到涨停
  平盘    : 0 ~ 3%
  低吸    : 涨幅 < 0
连板位置（T-1 为止连续涨停收盘的天数 n）：n=0 → 首板（若打板），n=1 → 二板（1进2），n>=2 → 高位接力
打板结果：当日收盘仍 = lu 为「封住」，否则「炸板」
次日溢价：T+1 开盘 / 首笔买价 - 1

涨跌幅制度：主板 10%；创业板 2020-08-24 起 20%（之前 10%）；科创板 20%；北交所 30%；名称含 ST 5%。
日线：baostock 不复权（adjustflag=3，preclose 已按除权调整，isST 判 5%），缓存在 data/bars_bs/<code>.csv。
用 distill/.venv 跑（系统 Python 无 baostock）：.venv/bin/python tools/enrich.py
用法: python3 tools/enrich.py [--person 北京炒家]
"""
import argparse
import json
import time
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import pandas as pd

BARS = Path.home() / ".trade-strategy/distill/youzi/bars_bs"


_BS = None


def _bs():
    global _BS
    if _BS is None:
        import baostock as bs
        bs.login()
        _BS = bs
    return _BS


def bs_code(code):
    return ("sh." if code.startswith(("6", "9")) else "bj." if code.startswith(("8", "4")) else "sz.") + code


_MKT = None
ALLOW_BS = False  # baostock 已黑名单；缓存区间外的数据要用时手动打开
MKT_FILE = Path.home() / ".trade-strategy/distill/youzi/market/daily.parquet"


def fetch(code: str, start: str, end: str) -> pd.DataFrame:
    """优先读全市场缓存（TickFlow 不复权 + 复权因子推出的 preclose，youzi-sentiment/lib/market.py 生成）；
    没有再走 baostock（2026-10-07 起 baostock 把本机列入黑名单，基本只剩缓存可用）。"""
    global _MKT
    if MKT_FILE.exists():
        if _MKT is None:
            _MKT = {c: g.reset_index(drop=True) for c, g in pd.read_parquet(MKT_FILE).groupby("code")}
        g = _MKT.get(code)
        if g is not None and len(g):
            g = g[(g["date"] >= pd.Timestamp(start)) & (g["date"] <= pd.Timestamp(end))].reset_index(drop=True)
            if len(g):
                g = g.copy()
                g["isST"] = "0"
                for c in ["open", "high", "low", "close", "preclose"]:
                    g[c] = g[c].astype("float64")
                return g
        if g is None:
            return pd.DataFrame()  # 全市场缓存（含退市股）里没有 = 不存在的代码（多为 OCR 误读），不再去 baostock 重试
        if not ALLOW_BS:
            return pd.DataFrame()
    return fetch_bs(code, start, end)


def fetch_bs(code: str, start: str, end: str) -> pd.DataFrame:
    """baostock 不复权日线（adjustflag=3），带 preclose（已按除权调整）与 isST。单线程（baostock 会话非线程安全）。"""
    BARS.mkdir(parents=True, exist_ok=True)
    f = BARS / f"{code}.csv"
    if f.exists():
        df = pd.read_csv(f, parse_dates=["date"])
        if len(df) and df["date"].min() <= pd.Timestamp(start) + pd.Timedelta(days=10) \
                and df["date"].max() >= pd.Timestamp(end) - pd.Timedelta(days=10):
            return df
    global _BS
    rows, fields = [], None
    for attempt in range(6):  # 连接失败（会话过多/断线）重登重试；失败不写缓存，免得把「拉不到」当成「没行情」
        try:
            rs = _bs().query_history_k_data_plus(bs_code(code), "date,open,high,low,close,preclose,volume,tradestatus,isST",
                                                 start_date=start, end_date=end, frequency="d", adjustflag="3")
            if rs.error_code != "0":
                raise RuntimeError(rs.error_msg)
            rows, fields = [], rs.fields
            while rs.next():  # 不用 rs.get_data()：多页时内部调用 DataFrame.append，pandas 3 已删除
                rows.append(rs.get_row_data())
            if rs.error_code != "0":
                raise RuntimeError(rs.error_msg)
            break
        except Exception:
            time.sleep(3 * (attempt + 1))
            try:
                _BS.logout()
            except Exception:
                pass
            _BS = None
    else:
        raise RuntimeError(f"baostock 拉取失败: {code}")
    df = pd.DataFrame(rows, columns=fields) if rows else pd.DataFrame()
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"])
    for c in ["open", "high", "low", "close", "preclose", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df[df["tradestatus"] == "1"].reset_index(drop=True)  # 去停牌日
    if f.exists():
        old = pd.read_csv(f, parse_dates=["date"])
        df = pd.concat([old, df]).drop_duplicates("date").sort_values("date").reset_index(drop=True)
    df.to_csv(f, index=False)
    return df


def limit_pct(code: str, name: str, d: pd.Timestamp, st: bool = False) -> float:
    if st or (isinstance(name, str) and "ST" in name.upper()):
        return 0.05
    if code.startswith("68"):
        return 0.20
    if code.startswith("30"):
        return 0.20 if d >= pd.Timestamp("2020-08-24") else 0.10
    if code.startswith(("8", "4", "92")):
        return 0.30
    return 0.10


def r2(x: float) -> float:
    return float(Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def classify(e: pd.DataFrame, bars: dict) -> pd.DataFrame:
    out = []
    for r in e.itertuples():
        b = bars.get(r.code)
        rec = {"etype": "无行情", "board_n": None, "sealed": None, "pc": None, "lu": None, "r_buy": None,
               "next_open_prem": None, "next_close_ret": None, "day_close_ret": None}
        if b is not None and len(b):
            idx = b.index[b["date"] == r.entry]
            if len(idx):
                i = idx[0]
                pc = b.at[i, "preclose"]
                lim = limit_pct(r.code, r.name, r.entry, str(b.at[i, "isST"]) == "1")
                lu = r2(pc * (1 + lim))
                px = r.first_buy_px
                rb = px / pc - 1
                t = str(r.entry_time) if isinstance(r.entry_time, str) else ""
                if t and t[:5] < "09:30":
                    et = "竞价"
                elif px >= lu - 0.011:
                    et = "打板"
                elif rb >= 0.03:
                    et = "半路"
                elif rb >= 0:
                    et = "平盘"
                else:
                    et = "低吸"
                # T-1 为止的连板数
                n, j = 0, i - 1
                while j >= 0:
                    pcj = b.at[j, "preclose"]
                    if abs(b.at[j, "close"] - r2(pcj * (1 + limit_pct(r.code, r.name, b.at[j, "date"], str(b.at[j, "isST"]) == "1")))) < 0.011:
                        n += 1
                        j -= 1
                    else:
                        break
                rec.update({"etype": et, "board_n": n, "pc": pc, "lu": lu, "r_buy": rb,
                            "sealed": bool(abs(b.at[i, "close"] - lu) < 0.011) if et == "打板" else None,
                            "day_close_ret": b.at[i, "close"] / px - 1})
                if i + 1 < len(b):
                    rec["next_open_prem"] = b.at[i + 1, "open"] / px - 1
                    rec["next_close_ret"] = b.at[i + 1, "close"] / px - 1
        out.append(rec)
    return pd.concat([e.reset_index(drop=True), pd.DataFrame(out)], axis=1)


def pos_label(row):
    if row["etype"] != "打板" or pd.isna(row["board_n"]):
        return None
    n = int(row["board_n"])
    return "首板" if n == 0 else "二板(1进2)" if n == 1 else "三板(2进3)" if n == 2 else "四板+"


def summarize(person: str, x: pd.DataFrame) -> tuple[str, dict]:
    x = x.copy()
    x["pos"] = x.apply(pos_label, axis=1)
    tot = x["pnl"].sum()

    def grp(col, order=None):
        g = x.groupby(col, dropna=True)
        df = pd.DataFrame({
            "段数": g.size(),
            "占比": g.size() / len(x),
            "胜率": g["pnl"].apply(lambda s: (s > 0).mean()),
            "平均收益": g["ret"].mean(),
            "中位收益": g["ret"].median(),
            "PF": g["pnl"].apply(lambda s: s[s > 0].sum() / max(1e-9, -s[s <= 0].sum())),
            "净利(万)": g["pnl"].sum() / 1e4,
            "占净利": g["pnl"].sum() / tot if tot else float("nan"),
            "次日开盘溢价": g["next_open_prem"].mean(),
        })
        if order:
            df = df.reindex([o for o in order if o in df.index])
        return df

    et = grp("etype", ["打板", "半路", "平盘", "低吸", "竞价", "无行情"])
    ps = grp("pos", ["首板", "二板(1进2)", "三板(2进3)", "四板+"])
    db = x[x["etype"] == "打板"]
    seal = db["sealed"].mean() if len(db) else None
    js = {"person": person, "n": int(len(x)), "coverage": float((x["etype"] != "无行情").mean()),
          "etype": json.loads(et.to_json(orient="index", force_ascii=False)),
          "board_pos": json.loads(ps.to_json(orient="index", force_ascii=False)),
          "seal_rate": float(seal) if seal is not None else None,
          "seal_win": float((db[db["sealed"] == True]["pnl"] > 0).mean()) if len(db) else None,  # noqa
          "broken_win": float((db[db["sealed"] == False]["pnl"] > 0).mean()) if len(db[db["sealed"] == False]) else None}  # noqa

    def fmt(df):
        if df.empty:
            return ["（无）"]
        cols = list(df.columns)
        L = ["| 类型 | " + " | ".join(cols) + " |", "|---" * (len(cols) + 1) + "|"]
        for k, r in df.iterrows():
            cells = []
            for c in cols:
                v = r[c]
                if c in ("段数",):
                    cells.append(f"{int(v)}")
                elif c in ("PF",):
                    cells.append("∞" if v > 1e6 else f"{v:.2f}")
                elif c == "净利(万)":
                    cells.append(f"{v:,.1f}")
                else:
                    cells.append("" if pd.isna(v) else f"{v * 100:.1f}%")
            L.append(f"| {k} | " + " | ".join(cells) + " |")
        return L

    L = [f"# {person} · 买点类型画像（接不复权日线）", "",
         f"- 持仓段 {len(x)}，行情覆盖 {js['coverage'] * 100:.0f}%", "",
         "## 按买点类型（每段首笔买入）", ""] + fmt(et) + [
         "", "## 打板段按连板位置（T-1 为止连板数）", ""] + fmt(ps) + [
         "", f"- 打板当日封住率 **{(seal or 0) * 100:.0f}%**；封住段胜率 {(js['seal_win'] or 0) * 100:.0f}%，"
         f"炸板段胜率 {(js['broken_win'] or 0) * 100:.0f}%" if len(db) else "- 无打板段",
         "", "> 口径：类型按首笔买价相对 T-1 收盘与涨停价判定；次日溢价 = T+1 开盘 / 首笔买价 - 1。"]
    return "\n".join(L) + "\n", js


