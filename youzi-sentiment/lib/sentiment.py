"""情绪周期读数（日频）+ 阶段状态机。定义见 docs/01-蒸馏笔记.md §3（出处：涅盘重升 / 炒股养家 / 不颜不语 / Asking / 独股一箭）。

全部用日线可算（不需要 L2）：
  ZT 收盘涨停家数 · DT 收盘跌停家数 · ZB 炸板（high 触及涨停、收盘未封）· ZBR = ZB/(ZT+ZB)
  H 最高连板 · LB 连板(≥2)家数 · FB 首板家数
  P1 昨日涨停(非一字)今日等权涨幅 · P2 昨日连板今日涨幅 · HIr 昨日 ≥3 板今日涨幅 · HId 其中跌停占比
  BR 上涨/(上涨+下跌) · AMT 成交额 · EW 等权指数（全市场中位涨跌幅累乘，作大盘代理）
  前视（只用于检验，不进状态判定）：FBo 今日首板次日开盘溢价 · FBc 次日收盘涨幅 · FBw 次日收盘 > 今日收盘比例
    FA 首板尝试家数（今日首次触及涨停、非一字）· FAseal 其封板率 · FAo/FAc 按涨停价买入的次日开盘/收盘收益（含炸板）
涨跌幅制度：主板 10%；创业板 2020-08-24 起 20%；科创 20%；北交所 30%；ST 5%（无历史 ST 名单 → 用
  「近 60 日内有过恰好 ±5% 的封板、且近 20 日 |涨跌幅| 从未超过 5.3%」推断，误差见 SKILL.md）；
  上市前 5 个交易日（新股无涨跌幅/首日 44%）剔除。
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def _r2(x):
    return np.floor(x * 100 + 0.5 + 1e-6) / 100


def limit_pct(code: pd.Series, date: pd.Series) -> np.ndarray:
    c = code.str
    lim = np.full(len(code), 0.10)
    lim[c.startswith("68").to_numpy()] = 0.20
    gem = c.startswith("30").to_numpy()
    lim[gem & (date >= pd.Timestamp("2020-08-24")).to_numpy()] = 0.20
    bj = (c.startswith("8") | c.startswith("4") | c.startswith("92")).to_numpy()
    lim[bj] = 0.30
    return lim


def annotate(df: pd.DataFrame) -> pd.DataFrame:
    """逐行加涨停/跌停/炸板/连板等标记。df: code,date,open,high,low,close,preclose,amount（按 code,date 排序）"""
    d = df.copy()
    d = d[d["preclose"] > 0]
    d["n"] = d.groupby("code").cumcount()
    d = d[d["n"] >= 5]  # 新股前 5 日
    lim = limit_pct(d["code"], d["date"])
    pc = d["preclose"].to_numpy(dtype="float64")
    cl = d["close"].to_numpy(dtype="float64")
    pct = cl / pc - 1
    d["pct"] = pct
    # ST 推断（只对 10% 主板）
    lu5, ld5 = _r2(pc * 1.05), _r2(pc * 0.95)
    hit5 = ((np.abs(cl - lu5) < 0.005) | (np.abs(cl - ld5) < 0.005)) & (lim == 0.10)
    d["hit5"] = hit5
    d["absp"] = np.abs(pct)
    g = d.groupby("code", sort=False)
    hit60 = g["hit5"].transform(lambda s: s.rolling(60, min_periods=1).sum())
    max20 = g["absp"].transform(lambda s: s.rolling(20, min_periods=1).max())
    st = (hit60 >= 1) & (max20 <= 0.053) & (lim == 0.10)
    lim = np.where(st, 0.05, lim)
    d["st"] = st
    lu, ld = _r2(pc * (1 + lim)), _r2(pc * (1 - lim))
    hi = d["high"].to_numpy(dtype="float64")
    lo = d["low"].to_numpy(dtype="float64")
    d["lu"] = lu
    d["zt"] = cl >= lu - 0.005
    d["dt"] = cl <= ld + 0.005
    d["touch"] = hi >= lu - 0.005
    d["zb"] = d["touch"] & ~d["zt"]
    d["yizi"] = d["zt"] & (lo >= lu - 0.005)
    # 连板数：连续 zt 的长度
    z = d["zt"].astype(int)
    grp = (z != z.groupby(d["code"]).shift()).cumsum()
    d["streak"] = z.groupby([d["code"], grp]).cumsum() * z
    # 次日
    g = d.groupby("code", sort=False)
    d["next_open"] = g["open"].shift(-1)
    d["next_close"] = g["close"].shift(-1)
    d["next_date"] = g["date"].shift(-1)
    d["prev_zt"] = g["zt"].shift(1).fillna(False).astype(bool)
    d["prev_yizi"] = g["yizi"].shift(1).fillna(False).astype(bool)
    d["prev_streak"] = g["streak"].shift(1).fillna(0)
    return d.drop(columns=["hit5", "absp"])


def daily(d: pd.DataFrame) -> pd.DataFrame:
    """按日聚合读数。"""
    g = d.groupby("date")
    out = pd.DataFrame({
        "N": g.size(),
        "ZT": g["zt"].sum(), "DT": g["dt"].sum(), "ZB": g["zb"].sum(),
        "H": g["streak"].max(), "LB": g["streak"].apply(lambda s: (s >= 2).sum()),
        "FB": g.apply(lambda x: (x["zt"] & (x["streak"] == 1)).sum(), include_groups=False),
        "UP": g["pct"].apply(lambda s: (s > 0).sum()), "DOWN": g["pct"].apply(lambda s: (s < 0).sum()),
        "AMT": g["amount"].sum() / 1e8,
        "MED": g["pct"].median(),
    })
    out["ZBR"] = out["ZB"] / (out["ZT"] + out["ZB"]).replace(0, np.nan)
    out["BR"] = out["UP"] / (out["UP"] + out["DOWN"]).replace(0, np.nan)
    y = d[d["prev_zt"] & ~d["prev_yizi"]]
    out["P1"] = y.groupby("date")["pct"].mean()
    out["P2"] = d[d["prev_streak"] >= 2].groupby("date")["pct"].mean()
    hi = d[d["prev_streak"] >= 3]
    out["HIr"] = hi.groupby("date")["pct"].mean()
    out["HId"] = hi.groupby("date")["dt"].mean()
    out["HIn"] = hi.groupby("date").size()
    fb = d[d["zt"] & (d["streak"] == 1) & ~d["yizi"]]
    out["FBo"] = (fb["next_open"] / fb["close"] - 1).groupby(fb["date"]).mean()
    out["FBc"] = (fb["next_close"] / fb["close"] - 1).groupby(fb["date"]).mean()
    out["FBw"] = (fb["next_close"] > fb["close"]).groupby(fb["date"]).mean()
    # 首板尝试（打板现实口径）：今日首次触及涨停（昨日未涨停、非一字），按涨停价买入，含炸板 → 无幸存者偏差
    fa = d[d["touch"] & (d["prev_streak"] == 0) & ~d["yizi"]]
    out["FA"] = fa.groupby("date").size()
    out["FAseal"] = fa.groupby("date")["zt"].mean()
    out["FAo"] = (fa["next_open"] / fa["lu"] - 1).groupby(fa["date"]).mean()
    out["FAc"] = (fa["next_close"] / fa["lu"] - 1).groupby(fa["date"]).mean()
    out["EW"] = (1 + out["MED"].fillna(0)).cumprod()
    return out.fillna({"P2": 0, "HIr": 0, "HId": 0, "HIn": 0})


def pctile(s: pd.Series, w: int = 250) -> pd.Series:
    return s.rolling(w, min_periods=60).apply(lambda x: (x[:-1] < x[-1]).mean() * 100 + (x[:-1] == x[-1]).mean() * 50, raw=True)


STATES = {"S1": "冰点候选", "S1c": "冰点确认", "S2": "启动/修复", "S3": "主升", "S4": "高潮", "S5": "分歧", "S6": "退潮/补跌"}
CAP = {"S1": 0.10, "S1c": 0.30, "S2": 0.30, "S3": 1.00, "S4": 0.50, "S5": 0.50, "S6": 0.10}


def phases(x: pd.DataFrame, p=None) -> pd.DataFrame:
    """状态机（优先级 崩溃→S1 > S6 > S1 > S1c > S4 > S5 > S3 > S2 > 延续）。阈值 p 可覆盖；默认值见 DEFAULT。"""
    p = {**DEFAULT, **(p or {})}
    x = x.copy()
    x["pZT"], x["pDT"], x["pP1"], x["pAMT5"] = pctile(x["ZT"]), pctile(x["DT"]), pctile(x["P1"]), pctile(x["AMT"].rolling(5).mean())
    x["H10max"] = x["H"].rolling(10).max().shift(1)
    x["H20max"] = x["H"].rolling(20).max()
    x["P1ma3"] = x["P1"].rolling(3).mean()
    ma20 = x["EW"].rolling(20).mean()
    x["WEAK_MKT"] = ((x["EW"] < ma20) & (ma20.diff(5) < 0)) | (x["pAMT5"] < p["amt_weak_pct"])
    x["BIG_UP"] = (x["EW"] > ma20) & (x["H20max"] >= p["big_h"])
    st, prev = [], "S2"
    for i, r in enumerate(x.itertuples()):
        s = None
        p2neg2 = i >= 1 and r.P2 < 0 and x["P2"].iat[i - 1] < 0
        if r.DT / max(1, r.N) >= p["crash_dt_ratio"]:
            s = "S1"  # 崩溃日（养家 YJ-05：跌停 ≥50 家@2012 年约 2400 只 ≈ 2%）→ 恐慌 = 冰点候选
        elif p2neg2 and (r.HIr < p["s6_hir"] or r.HId >= p["s6_hid"]) and r.H < (r.H10max if r.H10max == r.H10max else 99):
            s = "S6"
        elif r.P1 < 0 and r.pZT <= p["s1_zt_pct"] and (r.HIr <= p["s6_hir"] or r.pDT >= p["s1_dt_pct"]):
            s = "S1"
        elif prev == "S1" and r.P1 > 0 and r.HIr > 0:
            s = "S1c"
        elif r.pZT >= p["s4_zt_pct"] and r.BR >= p["s4_br"] and r.pP1 >= p["s4_p1_pct"]:
            s = "S4"
        elif i >= 1 and x["H"].iat[i - 1] >= 3 and r.H < x["H"].iat[i - 1] and (r.ZBR >= p["s5_zbr"] or r.P2 < 0):
            s = "S5"
        elif r.P1ma3 > 0 and r.P1 > 0 and r.pDT < p["s1_dt_pct"] and r.ZBR < p["s3_zbr"] and r.BIG_UP:
            s = "S3"
        elif prev in ("S1c", "S2", "S6", "S1") and r.P1ma3 > 0 and r.pZT >= 40:
            s = "S2"
        else:
            s = prev if prev not in ("S1c",) else "S2"
        st.append(s)
        prev = s
    x["state"] = st
    x["state_cn"] = x["state"].map(STATES)
    x["cap"] = x["state"].map(CAP) * np.where(x["WEAK_MKT"], 0.3, 1.0)
    return x


DEFAULT = {
    "s6_hir": -0.05,   # 高位股平均跌幅（待校准，NP-07）
    "s6_hid": 0.30,    # 高位股跌停占比（待校准）
    "s1_zt_pct": 20,   # 涨停家数分位（NP-09）
    "s1_dt_pct": 80,   # 跌停家数分位
    "s4_zt_pct": 90, "s4_br": 0.70, "s4_p1_pct": 80,   # 高潮（NP-17，BR 参考 2017 日志 p90）
    "s5_zbr": 0.40,    # 分歧炸板率（2017 日志中位 0.30 / p90 0.59）
    "s3_zbr": 0.30,
    "big_h": 5,        # 大周期：20 日最高连板 ≥5（NP-05）
    "amt_weak_pct": 20,  # 成交额 5 日均值分位 <20 → 弱市（AK-03、DG-11）
    "crash_dt_ratio": 0.02,  # 跌停家数 / 全市场 ≥2% → 崩溃日（YJ-05）
}
