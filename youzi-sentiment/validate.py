#!/usr/bin/env python3
"""情绪阶段状态机的证伪检验 → docs/02-检验.md（自动生成，可重复跑）。

问题：游资们说「按情绪阶段控仓」，那这套阶段在全市场数据上有没有预测力？
  T1 各状态下，次日「打首板」（首板尝试按涨停价买、含炸板）的收益与胜率 —— 状态之间有没有单调差异
  T2 负向闸门：S6/S1/WEAK_MKT 停手日 vs 在场日的打首板次日平均收益之差，对照随机停手（同天数、20 日块随机）+ 反面臂
  T3 锚点：涅盘重升记录的已知退潮段（2019-05）与公认冰点（2018-10、2020-02、2024-02 初）当时状态是什么
  T4 样本外：2009-2017 拟合期 vs 2018-至今 检验期，T2 的结论是否同号
  T5 游资真实持仓段按入场日状态分组（读语料库 trades/episodes_enriched.csv）：他们在 S6 入场是不是更差
  T6 选股力：游资首板段的次日开盘溢价 − 同日全市场首板尝试平均（t 值）
用法: python3 validate.py [--refresh]    （--refresh 先增量更新行情）
"""
import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "lib"))
import market  # noqa: E402
import sentiment as S  # noqa: E402

CORPUS = Path.home() / "Baidu-Pan/游资心法+讲座全集1/distill/人物"
OUT = HERE / "docs" / "02-检验.md"
READ = market.CACHE / "sentiment_daily.parquet"


def readings(refresh=False) -> pd.DataFrame:
    if refresh:
        market.update()
    if READ.exists() and not refresh and READ.stat().st_mtime > market.DAILY.stat().st_mtime:
        return pd.read_parquet(READ)
    d = S.annotate(market.load())
    x = S.daily(d)
    x.to_parquet(READ)
    return x


def fmt(v, pct=True, nd=2):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return ""
    return f"{v * 100:.{nd}f}%" if pct else f"{v:.{nd}f}"


def t1(x):
    g = x.groupby("state")
    t = pd.DataFrame({"天数": g.size(), "占比": g.size() / len(x), "次日开盘(打首板)": g["FAo"].mean(),
                      "次日收盘(打首板)": g["FAc"].mean(), "首板封板率": g["FAseal"].mean(),
                      "首板尝试家数": g["FA"].mean(), "涨停家数": g["ZT"].mean()})
    t.loc["全部"] = [len(x), 1.0, x["FAo"].mean(), x["FAc"].mean(), x["FAseal"].mean(), x["FA"].mean(), x["ZT"].mean()]
    order = ["S1", "S1c", "S2", "S3", "S4", "S5", "S6", "全部"]
    return t.reindex([o for o in order if o in t.index])


def gate_stats(x, off):
    """打首板（每天等权买全部首板尝试、次日收盘卖）在「在场日」与「停手日」的平均次日收益。
    不做复利：基线本身是负期望（见 T1），复利年化没有意义。"""
    r = x["FAc"]
    on, of = r[~off].mean(), r[off].mean() if off.any() else np.nan
    return on, of, 1 - off.mean()


def block_random(off, rng, block=20):
    n = len(off)
    k = int(off.sum())
    starts = np.arange(0, n, block)
    rng.shuffle(starts)
    m = np.zeros(n, bool)
    for b in starts:
        if m.sum() >= k:
            break
        m[b:b + block] = True
    m[np.where(m)[0][k:]] = False
    return pd.Series(m, index=off.index)


def t2(x, rng, n=1000):
    rows = []
    gates = {
        "S6 停手": x["state"].eq("S6"),
        "S1 停手": x["state"].eq("S1"),
        "S6+S1 停手": x["state"].isin(["S6", "S1"]),
        "仅 WEAK_MKT 停手": x["WEAK_MKT"],
        "S6+S1+WEAK_MKT 停手": x["state"].isin(["S6", "S1"]) | x["WEAK_MKT"],
        "（反面臂）S3+S4 停手": x["state"].isin(["S3", "S4"]),
    }
    for name, off in gates.items():
        on, of, inn = gate_stats(x, off)
        diff = on - of
        rd = []
        for _ in range(n):
            ro = block_random(off, rng)
            a, b, _ = gate_stats(x, ro)
            rd.append(a - b)
        rows.append([name, inn, on, of, diff, float(np.median(rd)), (np.array(rd) < diff).mean() * 100])
    return pd.DataFrame(rows, columns=["闸门", "在场比例", "在场日均", "停手日均", "差", "随机停手差中位", "落在随机分位"]), None


ANCHORS = [("2015-06-15", "2015-07-09", "2015 股灾"), ("2016-01-04", "2016-01-29", "2016 熔断"),
           ("2018-10-08", "2018-10-19", "2018-10 冰点"), ("2019-05-06", "2019-05-31", "2019-05 退潮（涅盘回撤 500 万）"),
           ("2020-02-03", "2020-02-07", "2020-02 疫情冰点"), ("2024-01-22", "2024-02-08", "2024-02 小微盘踩踏"),
           ("2024-09-24", "2024-10-10", "2024-09 政策急涨")]


def t3(x):
    out = []
    for a, b, name in ANCHORS:
        w = x.loc[a:b]
        if w.empty:
            continue
        vc = w["state"].value_counts()
        out.append([name, f"{a}~{b}", " ".join(f"{k}×{v}" for k, v in vc.items()),
                    int(w["WEAK_MKT"].sum()), len(w), w["ZT"].min(), w["DT"].max()])
    return pd.DataFrame(out, columns=["锚点", "区间", "状态分布", "WEAK_MKT 天数", "交易日", "最少涨停", "最多跌停"])


def t5(x):
    rows = []
    for f in sorted(CORPUS.glob("*/trades/episodes_enriched.csv")):
        e = pd.read_csv(f, parse_dates=["entry"])
        e = e[e["entry"].isin(x.index)]
        if len(e) < 20:
            continue
        e["state"] = x.loc[e["entry"], "state"].to_numpy()
        e["person"] = f.parent.parent.name
        rows.append(e[["person", "state", "pnl", "ret", "buy_amt"]])
    if not rows:
        return None, None
    a = pd.concat(rows)
    g = a.groupby("state")
    t = pd.DataFrame({"段数": g.size(), "人数": g["person"].nunique(), "胜率": g["pnl"].apply(lambda s: (s > 0).mean()),
                      "平均收益": g["ret"].mean(), "平均买入额(万)": g["buy_amt"].mean() / 1e4,
                      "PF": g["pnl"].apply(lambda s: s[s > 0].sum() / max(1e-9, -s[s <= 0].sum()))})
    order = ["S1", "S1c", "S2", "S3", "S4", "S5", "S6"]
    return t.reindex([o for o in order if o in t.index]), a


def t6(x):
    """选股力：游资的首板打板段（etype=打板 且 T-1 未涨停）次日开盘溢价 vs 同日全市场首板尝试平均（FAo）。"""
    rows = []
    for f in sorted(CORPUS.glob("*/trades/episodes_enriched.csv")):
        e = pd.read_csv(f, parse_dates=["entry"])
        e = e[(e["etype"] == "打板") & (e["board_n"] == 0) & e["entry"].isin(x.index)].dropna(subset=["next_open_prem"])
        if len(e) < 15:
            continue
        mkt = x.loc[e["entry"], "FAo"].to_numpy()
        ex = e["next_open_prem"].to_numpy() - mkt
        se = ex.std(ddof=1) / np.sqrt(len(ex))
        rows.append([f.parent.parent.name, len(e), e["next_open_prem"].mean(), np.nanmean(mkt), ex.mean(), ex.mean() / se if se > 0 else np.nan,
                     (ex > 0).mean()])
    if not rows:
        return None
    return pd.DataFrame(rows, columns=["人物", "首板段", "他的次日开盘溢价", "同日全市场首板尝试", "超额", "t 值", "超额为正比例"]).set_index("人物")


def md_table(df, pct_cols=(), num_cols=(), int_cols=()):
    cols = list(df.columns)
    L = ["| " + " | ".join([""] + cols) + " |", "|" + "---|" * (len(cols) + 1)]
    for k, r in df.iterrows():
        cells = []
        for c in cols:
            v = r[c]
            if c in pct_cols:
                cells.append(fmt(v))
            elif c in int_cols:
                cells.append("" if pd.isna(v) else f"{int(v)}")
            elif c in num_cols:
                cells.append(fmt(v, pct=False))
            else:
                cells.append(str(v))
        L.append(f"| {k} | " + " | ".join(cells) + " |")
    return L


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true")
    a = ap.parse_args()
    rng = np.random.default_rng(7)
    x = S.phases(readings(a.refresh))
    x = x[x.index >= "2010-01-01"]
    tab1 = t1(x)
    tab2, _ = t2(x, rng)
    fit, test = x[x.index < "2018-01-01"], x[x.index >= "2018-01-01"]
    tab2f, _ = t2(fit, rng, 300)
    tab2t, _ = t2(test, rng, 300)
    tab3 = t3(x)
    tab5, _ = t5(x)
    last = x.iloc[-1]
    L = ["# 情绪阶段状态机 · 证伪检验（自动生成）", "",
         f"> `python3 validate.py` 生成于数据截至 **{x.index[-1].date()}**；样本 {x.index[0].date()} ~ {x.index[-1].date()}，{len(x)} 个交易日。",
         "> 「打首板」= 当天首次触及涨停（昨日未涨停、非一字）的全部个股，按涨停价等权买入、次日收盘卖，含炸板 —— 不存在幸存者偏差。", "",
         "## T1 各状态下次日打首板表现", ""]
    L += md_table(tab1, pct_cols=("占比", "次日开盘(打首板)", "次日收盘(打首板)", "首板封板率"), num_cols=("首板尝试家数", "涨停家数"), int_cols=("天数",))
    L += ["", "## T2 负向闸门 vs 随机停手（同停手天数、20 日块随机 1000 次）", "",
          "全样本：", ""]
    L += md_table(tab2.set_index("闸门"), pct_cols=("在场比例", "在场日均", "停手日均", "差", "随机停手差中位"), num_cols=("落在随机分位",))
    L += ["", "> 读法：「差」= 在场日均 − 停手日均，越大说明停掉的确实是坏日子；「落在随机分位」≥95 才算闸门有信息量（随机停手同样天数、20 日块）。反面臂应落在低分位。", "", "## T4 样本外（拟合期 2010-2017 / 检验期 2018-至今）", "", "拟合期：", ""]
    L += md_table(tab2f.set_index("闸门"), pct_cols=("在场比例", "在场日均", "停手日均", "差", "随机停手差中位"), num_cols=("落在随机分位",))
    L += ["", "检验期：", ""]
    L += md_table(tab2t.set_index("闸门"), pct_cols=("在场比例", "在场日均", "停手日均", "差", "随机停手差中位"), num_cols=("落在随机分位",))
    L += ["", "## T3 锚点回放", ""] + md_table(tab3.set_index("锚点"))
    if tab5 is not None:
        L += ["", "## T5 游资真实持仓段按入场日状态分组（语料库 A/B 级人物，合并）", ""]
        L += md_table(tab5, pct_cols=("胜率", "平均收益"), num_cols=("平均买入额(万)", "PF"), int_cols=("段数", "人数"))
    tab6 = t6(x)
    if tab6 is not None:
        L += ["", "## T6 选股力：游资首板 vs 同日全市场首板尝试（次日开盘溢价）", ""]
        L += md_table(tab6, pct_cols=("他的次日开盘溢价", "同日全市场首板尝试", "超额", "超额为正比例"), num_cols=("t 值",), int_cols=("首板段",))
        L += ["", "> t 值 ≥2 才算选股力显著；样本是幸存者（出了名才有交割单），显著也只说明「这段时间他挑的首板比随手打的好」。"]
    L += ["", f"## 最新读数（{x.index[-1].date()}）", "",
          f"- 状态 **{last['state']} {last['state_cn']}**；仓位上限 {last['cap'] * 100:.0f}%；WEAK_MKT={bool(last['WEAK_MKT'])}",
          f"- ZT {int(last['ZT'])} · DT {int(last['DT'])} · 炸板率 {fmt(last['ZBR'])} · 最高板 {int(last['H'])} · 连板家数 {int(last['LB'])}",
          f"- P1 {fmt(last['P1'])} · P2 {fmt(last['P2'])} · 高位(≥3板)次日 {fmt(last['HIr'])}（跌停占比 {fmt(last['HId'])}）· 涨跌比 {fmt(last['BR'], False)}",
          "", "> 判读规则见 SKILL.md「怎么回答」。阈值全部为「待校准」（docs/01 §3），本文件只回答「这套定义有没有预测力」。"]
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text("\n".join(L) + "\n")
    x.to_parquet(market.CACHE / "sentiment_states.parquet")
    print("\n".join(L))


if __name__ == "__main__":
    main()
