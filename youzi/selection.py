#!/usr/bin/env python3
"""首板选股规则检验 → docs/02-选股检验.md（自动生成）。

问题：名家的首板有显著选股力（youzi-sentiment T6：次日开盘溢价超同日全市场 1.3~1.8pp，t 3.5~4.6）。
      蒸馏出来的「日线可算」选股规则，(a) 自己有没有预测力？(b) 能解释名家超额的多少？(c) 机械执行能不能赚钱？
宇宙：2010-01 ~ 今，每天所有「首板尝试」（今日首次触及涨停、昨日未涨停、非一字），按涨停价买入（假设成交），
      目标 yo = 次日开盘/涨停价 −1，yc = 次日收盘/涨停价 −1。
特征：只用买入时已知的信息（T-1 及以前的日线 + 今日开盘 + 板别）。「今日全天量」类条件会偷看，单独标 [前视]，不进组合。
检验：
  T1 逐条规则：日内超额 = 命中组均值 − 当日全体均值，按日聚类 t；拟合期 2010-2017 / 检验期 2018-；50 个随机特征作对照臂
  T2 名家归因：北京炒家/善行天助/赵老哥 的首板 vs 同日落选者 —— 规则命中率提升；拟合期截面回归预测他们的超额，算「可解释比例」
  T3 机械组合：只用拟合期显著且同号的规则打分，检验期每天取分数最高的 N 只（同分随机），对照同日随机 N 只；扣成本
用法: python3 selection.py
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "youzi-sentiment" / "lib"))
import market  # noqa: E402
import sentiment as S  # noqa: E402

CORPUS = Path.home() / "Baidu-Pan/游资心法+讲座全集1/distill/人物"
OUT = HERE / "docs" / "02-选股检验.md"
FEAT = market.CACHE / "firstboard_features.parquet"
SPLIT = pd.Timestamp("2018-01-01")
COST = 0.0020  # 往返：佣金万 2.5×2 + 印花税千 1（2023-08 起万 5）+ 滑点，粗估 20bp


def build() -> pd.DataFrame:
    if FEAT.exists() and FEAT.stat().st_mtime > market.DAILY.stat().st_mtime:
        return pd.read_parquet(FEAT)
    d = S.annotate(market.load())
    d = d.sort_values(["code", "date"]).reset_index(drop=True)
    g = d.groupby("code", sort=False)
    sh = lambda col: g[col].shift(1)  # noqa: E731  T-1 值
    c1 = sh("close")
    zt = d["zt"].astype(float)
    yz = d["yizi"].astype(float)
    for w in (10, 20, 60):
        d[f"zt{w}"] = g["zt"].transform(lambda s: s.astype(float).shift(1).rolling(w, min_periods=1).sum())
    d["yz10"] = yz.groupby(d["code"]).transform(lambda s: s.shift(1).rolling(10, min_periods=1).sum())
    for w in (5, 10, 20, 60, 250):
        d[f"ma{w}"] = g["close"].transform(lambda s: s.shift(1).rolling(w, min_periods=w).mean())
    d["ma10_prev"] = g["ma10"].shift(1)
    d["ma20_prev"] = g["ma20"].shift(1)
    d["ma60_prev5"] = g["ma60"].shift(5)
    for w in (5, 10, 20):
        d[f"ret{w}"] = c1 / g["close"].shift(w + 1) - 1
    for w in (20, 60, 120, 250):
        d[f"hi{w}"] = g["high"].transform(lambda s: s.shift(1).rolling(w, min_periods=w // 2).max())
        d[f"lo{w}"] = g["low"].transform(lambda s: s.shift(1).rolling(w, min_periods=w // 2).min())
    d["amt1"] = sh("amount")
    d["amt20"] = g["amount"].transform(lambda s: s.shift(1).rolling(20, min_periods=10).mean())
    d["amtcv20"] = g["amount"].transform(lambda s: s.shift(1).rolling(20, min_periods=10).std()) / d["amt20"]
    d["vol20"] = g["volume"].transform(lambda s: s.shift(1).rolling(20, min_periods=10).mean())
    d["o1"], d["h1"], d["l1"], d["pc1"] = sh("open"), sh("high"), sh("low"), sh("preclose")
    u = d[d["touch"] & (d["prev_streak"] == 0) & ~d["yizi"] & (d["n"] >= 25) & d["next_open"].notna()].copy()
    u["yo"] = u["next_open"] / u["lu"] - 1
    u["yc"] = u["next_close"] / u["lu"] - 1
    u["c1"] = c1.loc[u.index]
    # 当日全市场按昨日成交额排名
    u["amt_rank"] = d.groupby("date")["amt1"].rank(pct=True).loc[u.index]
    u.to_parquet(FEAT)
    return u


def rules(u: pd.DataFrame) -> dict:
    c1 = u["c1"]
    lu = u["lu"]
    R = {
        # 人气 / 近期涨停
        "近10日有涨停（DG-02/LFK-02/EC-01/GY-02）": u["zt10"] >= 1,
        "近10日涨停≥3（RXY-01）": u["zt10"] >= 3,
        "近20日涨停≥3 人气股（LHC-05）": u["zt20"] >= 3,
        "60日内涨停≥2（DG-03）": u["zt60"] >= 2,
        "近10日无涨停（LH-02 反向）": u["zt10"] == 0,
        "近10日一字≥2 → 剔除（RXY-02）": u["yz10"] >= 2,
        # 动量
        "10日涨幅>10%（LFK-02）": u["ret10"] > 0.10,
        "5日涨幅>0 且 收盘>MA20（GY-02）": (u["ret5"] > 0) & (c1 > u["ma20"]),
        "20日涨幅>60% → 剔除（YJ-21）": u["ret20"] > 0.60,
        # 均线
        "收盘>MA5>MA10（LFK-02）": (c1 > u["ma5"]) & (u["ma5"] > u["ma10"]),
        "收盘>MA10 且 MA10 上行（NP-13）": (c1 > u["ma10"]) & (u["ma10"] > u["ma10_prev"]),
        "MA20 下行 → 剔除（DTLB-01）": u["ma20"] < u["ma20_prev"],
        "MA20>MA60 且 MA60 上行（KHBD-01）": (u["ma20"] > u["ma60"]) & (u["ma60"] > u["ma60_prev5"]),
        "收盘>MA250（CZ-01）": c1 > u["ma250"],
        "涨停价一阳穿 MA5/10/20（58位 首板要点②）": (u["open"] < u[["ma5", "ma10", "ma20"]].min(axis=1)) & (lu > u[["ma5", "ma10", "ma20"]].max(axis=1)),
        # 位置
        "接近 20 日高点 ≥0.95（DG-02）": c1 >= 0.95 * u["hi20"],
        "涨停价突破 60 日高点（箱体突破首板，58位 要点①）": lu > u["hi60"],
        "涨停价突破 250 日高点（前方无套牢，58位 要点③）": lu > u["hi250"],
        "低位盘整：250 日振幅<40% 且 价≤250日高×0.7（WZXD-10）": ((u["hi250"] - u["lo250"]) / u["lo250"] < 0.40) & (c1 <= 0.7 * u["hi250"]),
        "处于 60 日区间下 40%（ZYCS-09 低位起涨）": (c1 - u["lo60"]) / (u["hi60"] - u["lo60"]).replace(0, np.nan) <= 0.40,
        "处于 120 日区间下 1/3（AK-07）": (c1 - u["lo120"]) / (u["hi120"] - u["lo120"]).replace(0, np.nan) <= 1 / 3,
        # 量能 / 规模
        "昨日成交额全市场前 5%（ZSXY-14）": u["amt_rank"] >= 0.95,
        "昨日成交额全市场前 50 名量级（ZMCK-06，前 1%）": u["amt_rank"] >= 0.99,
        "昨日成交额后 50% → 剔除（RHX-02）": u["amt_rank"] < 0.50,
        "20 日成交额变异系数<0.5（DG-03 量能稳定）": u["amtcv20"] < 0.5,
        # 板别 / 次新 / 价格
        "20cm（创业/科创）（XRR-05/EC-01）": u["code"].str[:2].isin(["30", "68"]) & (u["date"] >= "2020-08-24"),
        "深市（CXQ-14/LH-08）": u["code"].str[0].isin(["0", "3"]),
        "次新：上市 ≤60 交易日（JG-03）": u["n"] <= 60,
        "低价：昨收 < 当日首板中位（KB-06 股价低）": c1 < c1.groupby(u["date"]).transform("median"),
        # 昨日形态 / 今日开盘
        "昨日大阳>6% 或长下影 → 剔除（ZM-11）": ((u["c1"] - u["o1"]) / u["pc1"] > 0.06) | ((np.minimum(u["o1"], u["c1"]) - u["l1"]) / (u["h1"] - u["l1"]).replace(0, np.nan) > 0.5),
        "今日高开 ≥3%": u["open"] / u["preclose"] - 1 >= 0.03,
        "今日低开（<0）": u["open"] / u["preclose"] - 1 < 0,
        # 前视（用到今日全天成交量，买点当时不完全可知）
        "[前视] 今日放量 ≥20 日均量×2（ZLG-09/AK-07/ZYCS-09）": u["volume"] >= 2 * u["vol20"],
        "[前视] 当日封住（收盘仍涨停）": u["zt"],
    }
    return {k: v.fillna(False).astype(bool) for k, v in R.items()}


def day_excess(u, mask, y):
    """按日：命中组均值 − 当日全体均值；只用当天既有命中又有未命中的日子。返回日序列。"""
    df = pd.DataFrame({"date": u["date"], "m": mask, "y": u[y]})
    g = df.groupby("date")
    allm = g["y"].mean()
    hit = df[df["m"]].groupby("date")["y"].mean()
    n_hit = df.groupby("date")["m"].sum()
    n = g.size()
    ok = (n_hit > 0) & (n_hit < n)
    return (hit - allm)[ok[ok].index.intersection(hit.index)]


def tstat(s):
    s = s.dropna()
    return s.mean() / (s.std(ddof=1) / np.sqrt(len(s))) if len(s) > 10 else np.nan


def t1(u, R, rng):
    rows = []
    fit, test = u["date"] < SPLIT, u["date"] >= SPLIT
    for name, m in R.items():
        rec = [name, m.mean()]
        for part in (fit, test):
            for y in ("yo", "yc"):
                ex = day_excess(u[part], m[part], y)
                rec += [ex.mean(), tstat(ex)]
        rows.append(rec)
    cols = ["规则", "命中率", "拟合 yo 超额", "拟合 yo t", "拟合 yc 超额", "拟合 yc t", "检验 yo 超额", "检验 yo t", "检验 yc 超额", "检验 yc t"]
    T = pd.DataFrame(rows, columns=cols).set_index("规则")
    # 随机臂：50 个随机布尔特征（命中率 30%）
    rt = []
    for _ in range(50):
        m = pd.Series(rng.random(len(u)) < 0.3, index=u.index)
        rt.append([tstat(day_excess(u[fit], m[fit], "yc")), tstat(day_excess(u[test], m[test], "yc"))])
    return T, np.array(rt)


def persona(u, R):
    out, picks_all = [], {}
    for p in ("北京炒家", "善行天助", "赵老哥"):
        f = CORPUS / p / "trades" / "episodes_enriched.csv"
        if not f.exists():
            continue
        e = pd.read_csv(f, dtype={"code": str}, parse_dates=["entry"])
        e["code"] = e["code"].str.zfill(6)
        e = e[(e["etype"] == "打板") & (e["board_n"] == 0)]
        key = set(zip(e["code"], e["entry"]))
        pick = pd.Series([(c, d) in key for c, d in zip(u["code"], u["date"])], index=u.index)
        days = u.loc[pick, "date"].unique()
        sub = u["date"].isin(days)
        picks_all[p] = (pick, sub)
        rec = {"人物": p, "匹配首板": int(pick.sum())}
        for name, m in R.items():
            a = m[pick].mean()
            b = m[sub & ~pick].mean()
            rec[name] = (a, b)
        out.append(rec)
    return out, picks_all


def ols_explain(u, R, picks_all):
    """拟合期截面回归（日内去均值）：yo ~ 非前视规则；预测名家首板的超额，与实际超额对比。"""
    names = [k for k in R if not k.startswith("[前视]")]
    X = pd.DataFrame({k: R[k].astype(float) for k in names})
    Xd = X - X.groupby(u["date"]).transform("mean")
    res = {}
    for y in ("yo", "yc"):
        yd = u[y] - u[y].groupby(u["date"]).transform("mean")
        fit = (u["date"] < SPLIT).to_numpy()
        beta, *_ = np.linalg.lstsq(Xd[fit].to_numpy(), yd[fit].to_numpy(), rcond=None)
        pred = Xd.to_numpy() @ beta
        for p, (pick, sub) in picks_all.items():
            act = yd[pick].mean()
            prd = pred[pick.to_numpy()].mean()
            res.setdefault(p, {})[y] = (act, prd)
        res.setdefault("_beta", {})[y] = dict(zip(names, beta))
    return res


def t3(u, R, T, rng, N=3):
    names = [k for k in R if not k.startswith("[前视]")]
    sel = [(k, np.sign(T.loc[k, "拟合 yc t"])) for k in names if abs(T.loc[k, "拟合 yc t"]) >= 3]
    score = sum(R[k].astype(float) * s for k, s in sel) if sel else pd.Series(0.0, index=u.index)
    te = u[u["date"] >= SPLIT].copy()
    te["score"] = score.loc[te.index] + rng.random(len(te)) * 1e-6
    te["rnd"] = rng.random(len(te))
    top = te.sort_values("score", ascending=False).groupby("date").head(N)
    rnd = te.sort_values("rnd").groupby("date").head(N)
    allday = te.groupby("date")["yc"].mean()

    def perf(x):
        dly = x.groupby("date")["yc"].mean()
        return {"每日平均 yc": dly.mean(), "扣成本": dly.mean() - COST, "日胜率": (dly > COST).mean(),
                "超同日全体": (dly - allday.loc[dly.index]).mean(), "t(超额)": tstat(dly - allday.loc[dly.index]), "天数": len(dly)}
    # 随机 N 只重复 200 次的分布
    rd = []
    for _ in range(200):
        te["r"] = rng.random(len(te))
        x = te.sort_values("r").groupby("date").head(N)
        rd.append(x.groupby("date")["yc"].mean().mean())
    return sel, perf(top), perf(rnd), np.array(rd), allday.mean()


def fmt(v, pct=True):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return ""
    return f"{v * 100:+.2f}%" if pct else f"{v:.2f}"


def main():
    rng = np.random.default_rng(11)
    u = build()
    u = u[u["date"] >= "2010-01-01"]
    R = rules(u)
    T, rt = t1(u, R, rng)
    pers, picks = persona(u, R)
    ex = ols_explain(u, R, picks)
    sel, ptop, prnd, rd, base = t3(u, R, T, rng)
    L = ["# 首板选股规则检验（自动生成）", "",
         f"> `python3 selection.py`；宇宙 = {u['date'].min().date()} ~ {u['date'].max().date()} 每日首板尝试 {len(u):,} 个（日均 {len(u) / u['date'].nunique():.0f}），按涨停价买入（**假设都能成交**，见 §注意）。",
         "> yo = 次日开盘 / 涨停价 −1，yc = 次日收盘 / 涨停价 −1。超额 = 命中组 − 当日全体，按日聚类 t。拟合 2010-2017 / 检验 2018-。", "",
         f"基线：全体首板尝试 yo {fmt(u['yo'].mean())}，yc {fmt(u['yc'].mean())}。", "",
         "## T1 逐条规则", "",
         "| 规则 | 命中率 | 拟合 yo 超额 (t) | 拟合 yc 超额 (t) | 检验 yo 超额 (t) | 检验 yc 超额 (t) | 判定 |", "|---|---|---|---|---|---|---|"]
    for k, r in T.iterrows():
        ok = (abs(r["拟合 yc t"]) >= 3 and abs(r["检验 yc t"]) >= 2 and np.sign(r["拟合 yc t"]) == np.sign(r["检验 yc t"]))
        judge = ("✅ 样本外同号" if ok else "—") if not k.startswith("[前视]") else "（前视，不可用）"
        L.append(f"| {k} | {r['命中率'] * 100:.0f}% | {fmt(r['拟合 yo 超额'])} ({r['拟合 yo t']:.1f}) | {fmt(r['拟合 yc 超额'])} ({r['拟合 yc t']:.1f}) | "
                 f"{fmt(r['检验 yo 超额'])} ({r['检验 yo t']:.1f}) | {fmt(r['检验 yc 超额'])} ({r['检验 yc t']:.1f}) | {judge} |")
    L += ["", f"随机臂（50 个随机特征，命中率 30%）：yc t 的 |t| 95 分位 = 拟合 {np.nanpercentile(abs(rt[:, 0]), 95):.1f} / 检验 {np.nanpercentile(abs(rt[:, 1]), 95):.1f}；"
          f"同时 |t|≥3 且检验同号 |t|≥2 的随机特征 {int(((abs(rt[:, 0]) >= 3) & (abs(rt[:, 1]) >= 2) & (np.sign(rt[:, 0]) == np.sign(rt[:, 1]))).sum())}/50。", ""]
    L += ["## T2 名家归因：他们的首板 vs 同日落选者（规则命中率）", ""]
    if pers:
        names = [k for k in R]
        L += ["| 规则 | " + " | ".join(f"{p['人物']}（{p['匹配首板']}）选中 / 落选" for p in pers) + " |", "|---" * (len(pers) + 1) + "|"]
        for k in names:
            L.append(f"| {k} | " + " | ".join(f"{p[k][0] * 100:.0f}% / {p[k][1] * 100:.0f}%" for p in pers) + " |")
        L += ["", "**可解释比例**（拟合期截面回归，非前视规则；日内去均值后的超额）：", "",
              "| 人物 | yo 实际超额 | 规则预测 | 可解释 | yc 实际超额 | 规则预测 | 可解释 |", "|---|---|---|---|---|---|---|"]
        for p in picks:
            ao, po = ex[p]["yo"]
            ac, pc = ex[p]["yc"]
            L.append(f"| {p} | {fmt(ao)} | {fmt(po)} | {po / ao * 100:.0f}% | {fmt(ac)} | {fmt(pc)} | {pc / ac * 100:.0f}% |" if ao and ac else f"| {p} | | | | | | |")
    L += ["", f"## T3 机械组合（检验期 2018-，每天取规则分最高的 3 只，规则只用拟合期 |t|≥3 的 {len(sel)} 条，按拟合期符号等权）", "",
          "选入的规则：" + ("；".join(f"{k}（{'+' if s > 0 else '−'}）" for k, s in sel) or "无"), "",
          "| 组合 | 每日平均 yc | 扣 20bp 成本 | 日胜率（>成本） | 超同日全体 | t | 天数 |", "|---|---|---|---|---|---|---|"]
    for nm, p in (("规则分前 3", ptop), ("同日随机 3 只", prnd)):
        L.append(f"| {nm} | {fmt(p['每日平均 yc'])} | {fmt(p['扣成本'])} | {p['日胜率'] * 100:.0f}% | {fmt(p['超同日全体'])} | {p['t(超额)']:.1f} | {p['天数']} |")
    L += ["", f"随机 3 只重复 200 次：每日平均 yc 中位 {fmt(np.median(rd))}，95 分位 {fmt(np.percentile(rd, 95))}；规则组合落在第 {(rd < ptop['每日平均 yc']).mean() * 100:.0f} 分位。检验期全体均值 {fmt(base)}。", "",
          "## 注意", "",
          "- **成交假设偏乐观**：真实打板时，封得越死的板越难排到；能成交的往往是要炸的板。这里假设每个首板尝试都按涨停价成交，所以「封住」类条件的收益被高估，真实执行只会更差。",
          "- 只检验了日线能算、买入时已知的条件。题材地位、板块效应、封单、回封、换手横盘、首次封板时间等盘中信息**没有数据**，不在本检验内 —— 名家超额里规则解释不了的部分，大概率就在这里。",
          "- 40 条规则同时检验，|t|≥2 的随机误报约 2 条；判定要求拟合 |t|≥3 且检验期同号 |t|≥2，对照随机臂。"]
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text("\n".join(L) + "\n")
    print("\n".join(L))


if __name__ == "__main__":
    main()
