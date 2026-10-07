#!/usr/bin/env python3
"""（youzi skill 自带副本；源自语料库 tools/trades.py）原始成交 csv/xls/xlsx → 统一成交表 → 持仓段 → 统计画像。

episode：某只股票从空仓到建仓、再回到空仓的一段（多次加减仓合并），这是游资"一笔交易"的自然口径。
  - 窗口开始前就持有的股票，开头的卖出没有对应买入 → 丢弃并计数（期初持仓）
  - 窗口结束仍持有 → 未平仓，不计入胜率（计数）
费用：统一按估算（佣金万 2.5 最低 5 元 双边；印花税卖出千 1，2023-08-28 起万 5；过户费十万分之 1），
      保证不同券商导出之间口径一致；原表费用列只做核对。

"""
import argparse
import io
import json
import re
from pathlib import Path

import numpy as np
import pandas as pd

DISTILL = Path(__file__).resolve().parent.parent   # 只用于交易日历缓存

SYN = {
    "date": ["成交日期", "交易日期", "发生日期", "日期"],
    "time": ["成交时间", "时间"],
    "code": ["证券代码", "代码"],
    "name": ["证券名称", "名称"],
    "op": ["操作", "买卖标志", "业务名称", "摘要"],
    "price": ["成交均价", "成交价格", "价格"],
    "qty": ["成交数量", "数量"],
    "amount": ["成交金额", "金额"],
    "tid": ["成交编号"],
}
BUY = re.compile(r"买入|融资买|买$|^买")
SELL = re.compile(r"卖出|卖券还款|卖$|^卖")
SKIP_OP = re.compile(r"申购|配号|中签|配售|红利|利息|转托管|撤|新股|托管|担保品|划转|ETF申赎")


def read_any(p: Path) -> dict:
    raw = p.read_bytes()
    if raw[:4] == b"PK\x03\x04":
        return pd.read_excel(p, sheet_name=None, header=None, dtype=str)
    if raw[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return pd.read_excel(p, sheet_name=None, header=None, dtype=str, engine="xlrd")
    for enc in ("utf-8", "gbk", "utf-16"):
        try:
            t = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    sep = "\t" if "\t" in t[:2000] else ","
    return {"txt": pd.read_csv(io.StringIO(t), sep=sep, header=None, dtype=str, engine="python")}


def strip_eq(x):
    if isinstance(x, str):
        x = x.strip()
        m = re.fullmatch(r'="?(.*?)"?', x)
        return m.group(1).strip() if m else x
    return x


def normalize(df: pd.DataFrame, src: str) -> pd.DataFrame:
    df = df.map(strip_eq)
    hdr_row = None
    for i in range(min(len(df), 15)):
        row = [str(v) for v in df.iloc[i].tolist()]
        if any(v in SYN["code"] for v in row) and any(v in SYN["op"] + ["买卖标志"] for v in row):
            hdr_row = i
            break
    if hdr_row is None:
        return pd.DataFrame()
    cols = [str(v) for v in df.iloc[hdr_row].tolist()]
    body = df.iloc[hdr_row + 1:].reset_index(drop=True)
    body.columns = cols
    out = {}
    for k, names in SYN.items():
        for n in names:
            if n in cols:
                c = body[n]
                out[k] = c.iloc[:, 0] if isinstance(c, pd.DataFrame) else c
                break
    if not {"date", "code", "op", "price", "qty"} <= out.keys():
        return pd.DataFrame()
    t = pd.DataFrame(out)
    # 分块表（林疯狂：每笔一个小表，日期只写在块首行）→ 重复表头行去掉、日期前向填充
    t = t[t["code"].astype(str).str.contains(r"\d", na=False) | t["code"].isna()]
    t = t[~t["op"].astype(str).isin(SYN["op"])]
    t["date"] = t["date"].ffill()
    t["src"] = src
    return t


def clean_trades(t: pd.DataFrame) -> pd.DataFrame:
    t = t.dropna(subset=["date", "code", "op"]).copy()
    ds = t["date"].astype(str).str.strip().str.replace(r"^(20)0(\d{2})\.", r"\1\2.", regex=True)  # 源表笔误 20016.8.11
    dotted = ds.str.contains(r"[./-]")
    d1 = pd.to_datetime(ds.where(~dotted).str.replace(r"\D", "", regex=True).str[:8], format="%Y%m%d", errors="coerce")
    d2 = pd.to_datetime(ds.where(dotted).str.replace(".", "-", regex=False).str.replace("/", "-").str[:10], errors="coerce")
    t["date"] = d1.fillna(d2)
    t = t.dropna(subset=["date"])
    t["code"] = t["code"].astype(str).str.replace(r"\.0$", "", regex=True).str.extract(r"(\d+)")[0].str.zfill(6)
    t["op"] = t["op"].astype(str)
    t = t[~t["op"].str.contains(SKIP_OP)]
    t["side"] = np.where(t["op"].str.contains(BUY), "B", np.where(t["op"].str.contains(SELL), "S", ""))
    t = t[t["side"] != ""]
    # 只要 A 股个股（沪 60/68、深 00/30、北 8/4/92）；去逆回购、债券、基金
    t = t[t["code"].str.match(r"^(60|68|00|30|8|4|92)")]
    for c in ("price", "qty", "amount"):
        if c in t:
            t[c] = pd.to_numeric(t[c].astype(str).str.replace(",", ""), errors="coerce")
    t["qty"] = t["qty"].abs()
    if "amount" not in t or t["amount"].isna().all():
        t["amount"] = t["price"] * t["qty"]
    t["amount"] = t["amount"].abs().fillna(t["price"] * t["qty"])
    t = t[(t["qty"] > 0) & (t["price"] > 0) & (t["amount"] > 0)]  # 金额 0 = 新股入账/送转等记账行
    if "time" not in t:
        t["time"] = ""
    t["time"] = t["time"].fillna("").astype(str).str.strip()
    t["time"] = t["time"].where(t["time"].str.match(r"^\d{1,2}:\d{2}"), "")
    t["time"] = t["time"].str.replace(r"^(\d):", r"0\1:", regex=True)
    off = (t["time"] != "") & ((t["time"] < "09:15") | (t["time"] > "15:00:59"))
    t = t[~off]  # 盘后时间戳 = 清算记账行
    if "name" not in t:
        t["name"] = ""
    keep = ["date", "time", "code", "name", "side", "price", "qty", "amount", "src"] + (["tid"] if "tid" in t else [])
    t = t[keep]
    # 跨文件重叠去重
    key = ["date", "time", "code", "side", "price", "qty"] + (["tid"] if "tid" in t else [])
    t = t.drop_duplicates(subset=key)
    return t.sort_values(["date", "time"], kind="stable").reset_index(drop=True)


def est_fee(row) -> float:
    comm = max(row["amount"] * 0.00025, 5.0)
    trans = row["amount"] * 0.00001
    stamp = 0.0
    if row["side"] == "S":
        stamp = row["amount"] * (0.0005 if row["date"] >= pd.Timestamp("2023-08-28") else 0.001)
    return comm + trans + stamp


def episodes(t: pd.DataFrame, cal: pd.DatetimeIndex) -> tuple[pd.DataFrame, dict]:
    t = t.copy()
    t["fee"] = t.apply(est_fee, axis=1)
    eps, pre_sells, open_pos = [], 0, 0
    for code, g in t.groupby("code", sort=False):
        pos, cur = 0, None
        for _, r in g.iterrows():
            if r["side"] == "B":
                if pos == 0:
                    cur = {"code": code, "name": r["name"], "entry": r["date"], "entry_time": r["time"],
                           "buy_amt": 0.0, "sell_amt": 0.0, "fee": 0.0, "buy_qty": 0, "n_buy": 0, "n_sell": 0,
                           "first_buy_px": r["price"]}
                pos += r["qty"]
                cur["buy_amt"] += r["amount"]
                cur["buy_qty"] += r["qty"]
                cur["fee"] += r["fee"]
                cur["n_buy"] += 1
            else:
                if pos <= 0 or cur is None:
                    pre_sells += 1
                    continue
                q = min(r["qty"], pos)
                frac = q / r["qty"]
                if r["qty"] > pos * 1.05:
                    # 卖出量 > 持仓：要么是窗口前就有的底仓，要么是送转后股数变多。
                    # 送转判据：卖价 × (卖量/持仓) ≈ 均买价（±35%）→ 按整笔卖出金额平仓（轮回666 德艺文创/农尚环境）
                    implied = r["qty"] / pos
                    avg_px = cur["buy_amt"] / max(1, cur["buy_qty"])
                    if abs(r["price"] * implied / avg_px - 1) < 0.35:
                        frac = 1.0
                        cur["split_adj"] = True
                pos -= q
                cur["sell_amt"] += r["amount"] * frac
                cur["fee"] += r["fee"] * frac
                cur["n_sell"] += 1
                cur["exit"], cur["exit_time"] = r["date"], r["time"]
                if pos <= 0:
                    eps.append(cur)
                    pos, cur = 0, None
        if pos > 0:
            open_pos += 1
    e = pd.DataFrame(eps)
    if e.empty:
        return e, {"pre_window_sells": pre_sells, "open_positions": open_pos}
    e["pnl"] = e["sell_amt"] - e["buy_amt"] - e["fee"]
    e["ret"] = e["pnl"] / e["buy_amt"]
    ci = cal.searchsorted
    e["hold_td"] = [int(ci(x) - ci(y)) for x, y in zip(e["exit"], e["entry"])]
    return e, {"pre_window_sells": pre_sells, "open_positions": open_pos}


def bucket_time(s: str) -> str:
    if not s:
        return "无时间"
    hm = s[:5]
    if hm < "09:30":
        return "集合竞价"
    if hm < "09:35":
        return "09:30-09:35"
    if hm < "10:00":
        return "09:35-10:00"
    if hm < "11:31":
        return "10:00-11:30"
    if hm < "14:30":
        return "13:00-14:30"
    return "14:30-15:00"


def board(code: str) -> str:
    if code.startswith("60"):
        return "沪主板"
    if code.startswith("00"):
        return "深主板"
    if code.startswith("30"):
        return "创业板"
    if code.startswith("68"):
        return "科创板"
    return "北交所"


def trading_calendar() -> pd.DatetimeIndex:
    cache = Path.home() / ".trade-strategy/distill/youzi/trade_cal.csv"
    if cache.exists():
        return pd.DatetimeIndex(pd.to_datetime(pd.read_csv(cache)["d"]))
    try:
        import akshare as ak
        d = pd.to_datetime(ak.tool_trade_date_hist_sina()["trade_date"])
        cache.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame({"d": d.dt.strftime("%Y-%m-%d")}).to_csv(cache, index=False)
        return pd.DatetimeIndex(d)
    except Exception:
        return pd.bdate_range("2005-01-01", "2030-12-31")


def pct(x):
    return f"{x * 100:.1f}%"


def profile(person: str, t: pd.DataFrame, e: pd.DataFrame, meta: dict) -> tuple[str, dict]:
    win = e[e["pnl"] > 0]
    loss = e[e["pnl"] <= 0]
    gp, gl = win["pnl"].sum(), -loss["pnl"].sum()
    net = e["pnl"].sum()
    top10 = e.nlargest(10, "pnl")["pnl"].sum()
    hold = e["hold_td"]
    hb = pd.cut(hold, [-1, 0, 1, 2, 5, 10, 10_000], labels=["当日(T+0 回转)", "1 天", "2 天", "3-5 天", "6-10 天", ">10 天"])
    buys, sells = t[t["side"] == "B"], t[t["side"] == "S"]
    first_buy_tb = e["entry_time"].map(bucket_time).value_counts(normalize=True)
    sell_tb = sells["time"].map(bucket_time).value_counts(normalize=True)
    bd = e["code"].map(board).value_counts(normalize=True)
    # 同时持仓数（按日末）
    days = pd.DatetimeIndex(sorted(t["date"].unique()))
    conc = []
    for d in days:
        conc.append(int(((e["entry"] <= d) & (e["exit"] > d)).sum()))
    conc = pd.Series(conc)
    gross_peak = 0.0
    for d in days:
        live = e[(e["entry"] <= d) & (e["exit"] >= d)]
        gross_peak = max(gross_peak, live["buy_amt"].sum())
    # 连亏
    streak = mx = 0
    for p in e.sort_values("exit")["pnl"]:
        streak = streak + 1 if p <= 0 else 0
        mx = max(mx, streak)
    monthly = e.groupby(e["exit"].dt.to_period("M"))["pnl"].sum()
    js = {
        "person": person,
        "period": [str(t["date"].min().date()), str(t["date"].max().date())],
        "n_fills": int(len(t)), "n_buys": int(len(buys)), "n_sells": int(len(sells)),
        "n_episodes": int(len(e)), "n_stocks": int(e["code"].nunique()),
        "win_rate": float(len(win) / len(e)) if len(e) else None,
        "avg_win": float(win["ret"].mean()) if len(win) else None,
        "avg_loss": float(loss["ret"].mean()) if len(loss) else None,
        "median_ret": float(e["ret"].median()),
        "profit_factor": float(gp / gl) if gl > 0 else None,
        "net_pnl": float(net), "gross_profit": float(gp), "gross_loss": float(gl),
        "top10_share_of_net": float(top10 / net) if net > 0 else None,
        "worst_episode_ret": float(e["ret"].min()),
        "max_loss_streak": int(mx),
        "hold_median_td": float(hold.median()),
        "hold_win_median_td": float(win["hold_td"].median()) if len(win) else None,
        "hold_loss_median_td": float(loss["hold_td"].median()) if len(loss) else None,
        "hold_dist": {str(k): float(v) for k, v in hb.value_counts(normalize=True).sort_index().items()},
        "entry_time_dist": {k: float(v) for k, v in first_buy_tb.items()},
        "sell_time_dist": {k: float(v) for k, v in sell_tb.items()},
        "board_dist": {k: float(v) for k, v in bd.items()},
        "avg_episode_buy_amt": float(e["buy_amt"].mean()),
        "gross_exposure_peak": float(gross_peak),
        "concurrent_median": float(conc.median()), "concurrent_max": int(conc.max()),
        "new_eps_per_active_day": float(len(e) / max(1, e["entry"].nunique())),
        "monthly_pnl": {str(k): float(v) for k, v in monthly.items()},
        **meta,
    }
    js["sell_match_ratio"] = 1 - meta["pre_window_sells"] / max(1, len(sells))
    js["ocr_share"] = float(t["src"].astype(str).str.startswith("ocr:").mean())
    L = [f"# {person} · 成交统计画像（机械统计，未接行情）", "",
         f"- 区间 **{js['period'][0]} ~ {js['period'][1]}**；成交 {js['n_fills']} 笔（买 {js['n_buys']} / 卖 {js['n_sells']}），"
         f"持仓段 **{js['n_episodes']}** 段、{js['n_stocks']} 只股票",
         f"- 期初持仓导致的无主卖出 {meta['pre_window_sells']} 笔（丢弃）；期末未平仓 {meta['open_positions']} 只（不计胜负）",
         f"- **数据完整度**：卖出配上买入的比例 {js['sell_match_ratio'] * 100:.0f}%"
         + ("（OCR 来源，有缺页/漏行，统计只作方向参考）" if js['ocr_share'] > 0.5 else ""),
         f"- 来源：{', '.join(sorted(t['src'].unique()))}", "",
         "## 胜负与盈亏比", "",
         "| 指标 | 值 |", "|---|---|",
         f"| 胜率（段） | {pct(js['win_rate'])} |",
         f"| 平均盈利段收益 | {pct(js['avg_win'] or 0)} |",
         f"| 平均亏损段收益 | {pct(js['avg_loss'] or 0)} |",
         f"| 中位段收益 | {pct(js['median_ret'])} |",
         f"| PF（总盈 ÷ 总亏） | {js['profit_factor']:.2f} |" if js["profit_factor"] else "| PF | n/a |",
         f"| 净盈亏（估算费后） | {net / 1e4:,.1f} 万 |",
         f"| 前 10 大盈利段占净利 | {pct(js['top10_share_of_net']) if js['top10_share_of_net'] else 'n/a'} |",
         f"| 最差一段 | {pct(js['worst_episode_ret'])} |",
         f"| 最长连亏段数 | {mx} |", "",
         "## 持有周期（交易日）", "",
         f"中位 **{js['hold_median_td']:.0f}** 天；盈利段中位 {js['hold_win_median_td']} 天 vs 亏损段中位 {js['hold_loss_median_td']} 天", "",
         "| 持有 | 占比 |", "|---|---|"] + [f"| {k} | {pct(v)} |" for k, v in js["hold_dist"].items()] + [
         "", "## 下单时间", "",
         "| 时段 | 首笔建仓占比 | 卖出成交占比 |", "|---|---|---|"] + [
         f"| {k} | {pct(js['entry_time_dist'].get(k, 0))} | {pct(js['sell_time_dist'].get(k, 0))} |"
         for k in ["集合竞价", "09:30-09:35", "09:35-10:00", "10:00-11:30", "13:00-14:30", "14:30-15:00", "无时间"]
         if js['entry_time_dist'].get(k) or js['sell_time_dist'].get(k)] + [
         "", "## 板块 / 仓位", "",
         "板块：" + "，".join(f"{k} {pct(v)}" for k, v in js["board_dist"].items()), "",
         f"- 平均每段买入金额 {js['avg_episode_buy_amt'] / 1e4:,.1f} 万；同时持仓中位 {js['concurrent_median']:.0f} 只、最多 {js['concurrent_max']} 只；"
         f"峰值总敞口约 {gross_peak / 1e4:,.0f} 万",
         f"- 有建仓的交易日平均新开 {js['new_eps_per_active_day']:.2f} 段", "",
         "## 月度净盈亏（万，按平仓月）", "",
         "| 月 | 净盈亏 |", "|---|---|"] + [f"| {k} | {v / 1e4:,.1f} |" for k, v in js["monthly_pnl"].items()] + [
         "", "## 盈利前 10 段", "", "| 代码 | 名称 | 建仓 | 平仓 | 持有 | 收益 | 盈亏(万) |", "|---|---|---|---|---|---|---|"] + [
         f"| {r.code} | {r.name} | {r.entry.date()} | {r.exit.date()} | {r.hold_td} | {pct(r.ret)} | {r.pnl / 1e4:,.1f} |"
         for r in e.nlargest(10, "pnl").itertuples()] + [
         "", "## 亏损前 10 段", "", "| 代码 | 名称 | 建仓 | 平仓 | 持有 | 收益 | 盈亏(万) |", "|---|---|---|---|---|---|---|"] + [
         f"| {r.code} | {r.name} | {r.entry.date()} | {r.exit.date()} | {r.hold_td} | {pct(r.ret)} | {r.pnl / 1e4:,.1f} |"
         for r in e.nsmallest(10, "pnl").itertuples()] + [
         "", "> 口径：费用统一估算；episode = 空仓→建仓→空仓；持有天数按交易日历。未接行情，打板/低吸/首板等分类见 enrich。"]
    return "\n".join(L) + "\n", js


