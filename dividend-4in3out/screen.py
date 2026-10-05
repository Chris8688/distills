#!/usr/bin/env python3
"""「股息率模式 · 四进三出」A股筛选 —— 蒸馏自雪球组合 ZH1027925（高股息之家·老杜）。

规则全文与出处见同目录 docs/01-蒸馏笔记.md；本脚本只做三件事：
  ① 买入清单：股息率 ≥ 4%、连续分红 ≥ 4 年、季报/分红可持续性过关，按「可持续股息率」排序 + 行业上限
  ② 持仓体检：股息率 ≤ 3% 或 季报扣非恶化（按规则档）→ 卖出；其余持有
  ③ 换仓建议：「不出则不进」—— 只有卖出腾出的空位才从买入清单顶部补

股息率口径（原作者《如何计算股息率》基础篇 + 进阶篇）：
  静态股息率 = 「最近一个已公布年报的财年」全年现金分红合计（含中报/季报分红）÷ 当前市值
  用「现金总额 ÷ 市值」而不是「每股分红 ÷ 股价」，送转股稀释自动处理（进阶篇的要点）。

数据：东财分红送配（akshare stock_fhps_em，全市场按报告期）· 新浪财务摘要（扣非/现金流/归母净利）
     · TickFlow 股本×收盘价（lib/prices.market_caps）· 东财业绩报表的「所处行业」。
缓存：~/.cache/distill/dividend-4in3out/（环境变量 DISTILL_CACHE）
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)

from lib import CACHE, STATE  # noqa: E402

# ── 规则参数（原作者口径；改这里之前先读 docs/01 的出处与校准） ──────────────────
BUY_YIELD = 0.04          # 四进
SELL_YIELD = 0.03         # 三出
MIN_YEARS = 4             # 连续分红年数：六大原则写 5，问答篇写「四~五年（经验数据）」；实盘 4 年的也买
KF_SELL = -0.10           # 1.0：最新一期累计扣非同比 < -10% → 卖出 / 不买
KF_HARD = -0.30           # observed：扣非 < -30% 直接卖（规则 A）
FWD_KF_MIN = 0.04         # observed：扣非 -10%~-30% 时，扣非口径预期股息率 < 4% 才卖（规则 B）
                          #   A∨B 对作者 2023 起 27 次「扣非<-10%」决策：命中其卖出 12/18，误卖其留着的 1/9
KF_BUY_STRICT = 0.0       # strict（跟随者转述，已被实盘证伪为「2.0」）：扣非同比须 > 0
OCF_NI_STRICT = 0.6       # strict：经营现金流 / 归母净利 > 0.6（金融业不适用）
SPIKE_X = 10.0            # 上年分红 > 前 4 年中位数 × 10 → 「分红怪异」，不买（3× 会误挡作者 14/111 笔真实买入）
FLOOR_PAYOUT_MAX = 1.0    # 保底分红率上限（= 4% × PE_TTM ≤ 100%，即 TTM 市盈率 ≤ 25）
# 买入过滤（2026-10-05 落地，docs/04；三档规则都生效，只挡买入、不影响卖出）：
KNIFE_REL = -0.25         # 不接飞刀（相对版，2026-10-05 替换绝对版）：过去一年前复权涨跌比「全部连续分红 ≥4 年 A 股」
                          #   的中位数多跌 25% 以上不买。只挡「自己出问题」的，不挡「跟着大盘跌」的（2018-12/2020-03 底部反弹）
                          #   17 起点：年化 +0.88pp（绝对版 +0.18）、买错率 22.3%（绝对版 23.2%）、大赢家 25.6；两段窗口都优于绝对版
PROFIT_SURGE = 0.70       # 不买利润暴增：TTM 归母净利同比 > +70% 不买（峰值利润 / 峰值分红）
#   17 个起点回测：买错率 26.4% → 23.2%、陷阱率 30.4% → 27.7%、大赢家笔数 24.9 → 25.5、年化 +0.18pp；两段独立窗口都成立
PAYOUT_OVER_YEARS = 2     # 分红率 >100%（分红高于归母净利）连续 ≥2 年 → 不买（1.0 原文「连续几年分红率超过100%」= 怪异）
                          #   作者实盘单年 >100% 照买（21/111 笔，价格收益中位 -6.6%、胜率 38%，其余 -2.3%、48%）
HIST_YEARS = 8           # 分红历史拉几年（只影响「连续分红」显示上限；历史期永久缓存）
STOCK_CAP = 0.10          # 单只上限
INDUSTRY_CAP = 0.20       # 单行业上限
FIN_INDUSTRIES = ("银行", "保险", "证券", "多元金融", "非银金融")
CYCLICAL_HINT = ("煤", "钢", "航运", "港口", "航空", "海运", "有色", "化工", "化学", "石油", "采掘", "水泥", "建材")


# ══════════════════════════════════════════════════════════════════════════════
# 缓存小工具
# ══════════════════════════════════════════════════════════════════════════════
def _cpath(name: str) -> str:
    os.makedirs(CACHE, exist_ok=True)
    return os.path.join(CACHE, name)


def _fresh(path: str, hours: float) -> bool:
    return os.path.exists(path) and (time.time() - os.path.getmtime(path)) < hours * 3600


def _quiet_ak():
    """akshare 的分页进度条写 stderr，吞掉。"""
    import contextlib
    import io
    return contextlib.redirect_stderr(io.StringIO())


# ══════════════════════════════════════════════════════════════════════════════
# 分红：东财分红送配，按报告期全市场拉
# ══════════════════════════════════════════════════════════════════════════════
BAD_PLAN = ("未通过", "停止", "取消", "终止", "不分配")


def _date(x) -> str:
    s = str(x or "")[:10]
    return s if len(s) == 10 and s[4] == "-" else ""


def _fhps_period(period: str, today: dt.date, offline: bool) -> dict:
    """{code: [[现金总额, 方案进度, 每10股派, 每10股送转, 除权除息日, 股权登记日]]}。
    历史期永久缓存，近 18 个月的期 1 天 TTL（方案会从预案走到实施、补上除息日）。"""
    p = _cpath(f"fhps2_{period}.json")
    pd_ = dt.datetime.strptime(period, "%Y%m%d").date()
    ttl = 24.0 if (today - pd_).days < 550 else 24 * 365 * 10
    if offline or _fresh(p, ttl):
        if os.path.exists(p):
            return json.load(open(p))
        if offline:
            return {}
    import akshare as ak
    try:
        with _quiet_ak():
            d = ak.stock_fhps_em(date=period)
    except Exception as e:      # noqa: BLE001  （该期还没有任何方案时东财返回空 → akshare 抛错）
        print(f"  ⚠️ 分红 {period} 取数失败：{type(e).__name__}", file=sys.stderr)
        d = None
    out: dict = {}
    if d is not None and len(d):
        for _, r in d.iterrows():
            ratio, shares = r.get("现金分红-现金分红比例"), r.get("总股本")
            sz = r.get("送转股份-送转总比例")
            status = str(r.get("方案进度") or "")
            if any(b in status for b in BAD_PLAN):
                continue
            ratio = float(ratio) if ratio == ratio and ratio else 0.0
            sz = float(sz) if sz == sz and sz else 0.0
            if (ratio <= 0 and sz <= 0) or shares != shares:
                continue
            cash = ratio / 10.0 * float(shares)          # 每 10 股派 X 元 × 股本
            out.setdefault(str(r["代码"]).zfill(6), []).append(
                [cash, status, ratio, sz, _date(r.get("除权除息日")), _date(r.get("股权登记日"))])
    # ⚠️ 取数失败 / 返回空 → 不落缓存。历史期缓存是「永久」的，空结果一旦写进去，
    #   该财年就被当成全市场不分红（2026-10-02 查到 FY2018 年报期被缓存成 0 条：
    #   「连续分红」全体少算一年、yield_trap 研究 2021/22 快照样本塌缩）。
    if out or (d is not None and len(d) == 0 and (today - pd_).days < 120):
        json.dump(out, open(p, "w"), ensure_ascii=False)
    elif os.path.exists(p):
        return json.load(open(p))
    return out


def dividend_history(today: dt.date, years: int, offline: bool) -> dict:
    """{code: {财年: 现金分红总额}}，财年 = 报告期所属年份（中报/季报分红计入当年）。"""
    last_fy = today.year - 1
    hist: dict = {}
    for fy in range(last_fy - years + 1, today.year + 1):
        for mmdd in ("1231", "0630", "0930", "0331"):
            period = f"{fy}{mmdd}"
            if dt.datetime.strptime(period, "%Y%m%d").date() > today:
                continue
            # 季报分红稀少，只拉最近 3 年
            if mmdd in ("0930", "0331") and fy < last_fy - 1:
                continue
            for code, rows in _fhps_period(period, today, offline).items():
                hist.setdefault(code, {}).setdefault(fy, 0.0)
                hist[code][fy] += sum(r[0] for r in rows)
    return hist


def corporate_actions(today: dt.date, offline: bool) -> dict:
    """{code: [{ex, record, cash_per_share, bonus_per_share}]}：最近两个财年内已定除息日的方案（纸面账户派息/送转用）。"""
    out: dict = {}
    for fy in (today.year - 1, today.year):
        for mmdd in ("1231", "0630", "0930", "0331"):
            period = f"{fy}{mmdd}"
            if dt.datetime.strptime(period, "%Y%m%d").date() > today:
                continue
            for code, rows in _fhps_period(period, today, offline).items():
                for r in rows:
                    if len(r) >= 6 and r[4]:
                        out.setdefault(code, []).append({"ex": r[4], "record": r[5], "period": period,
                                                         "cash_ps": r[2] / 10.0, "bonus_ps": r[3] / 10.0})
    return out


# ══════════════════════════════════════════════════════════════════════════════
# 市值 / 行业
# ══════════════════════════════════════════════════════════════════════════════
def price_change_1y(codes: list[str], offline: bool) -> dict:
    """{code: 过去一年前复权涨跌}（TickFlow，含分红再投入；不接飞刀用）。20 小时缓存，遇 429 退避。"""
    import urllib.error
    from lib import prices as PX
    p = _cpath("px1y.json")
    have = json.load(open(p)) if os.path.exists(p) and (offline or _fresh(p, 20)) else {}
    need = [c for c in codes if c not in have]
    if need and not offline:
        sym = {PX.tf(c, "CN"): c for c in need}
        keys = list(sym)
        for i in range(0, len(keys), PX.BATCH_MAX):
            ch = keys[i:i + PX.BATCH_MAX]
            for att in range(5):
                try:
                    got = PX._klines(ch, 270, "forward")
                    break
                except urllib.error.HTTPError as e:
                    if e.code != 429 or att == 4:
                        raise
                    time.sleep(10 * (att + 1))
            for k, bars in got.items():
                if len(bars) < 2:
                    continue
                last = dt.date.fromisoformat(bars[-1]["date"])
                old = [b for b in bars if dt.date.fromisoformat(b["date"]) <= last - dt.timedelta(days=365)]
                base = old[-1] if old else bars[0]
                have[sym[k]] = bars[-1]["close"] / base["close"] - 1
            time.sleep(0.5)
        json.dump(have, open(p, "w"))
    return have


def market_caps(offline: bool) -> dict:
    from lib import prices as PX
    p = os.path.join(PX.OWN_CACHE, "cn_mcap.json")
    refresh = (not offline) and not _fresh(p, 20)
    rows = PX.market_caps("CN", refresh=refresh)
    return {r["code"]: r for r in rows}


def industries(today: dt.date, offline: bool) -> dict:
    """东财业绩报表的「所处行业」（全市场一次）。7 天 TTL。"""
    p = _cpath("industry.json")
    if offline or _fresh(p, 24 * 7):
        if os.path.exists(p):
            return json.load(open(p))
    import akshare as ak
    out: dict = {}
    # 从最近的报告期往回找，直到拿到一期
    q_ends = [f"{y}{md}" for y in (today.year, today.year - 1) for md in ("1231", "0930", "0630", "0331")]
    for period in q_ends:
        if dt.datetime.strptime(period, "%Y%m%d").date() > today:
            continue
        try:
            with _quiet_ak():
                d = ak.stock_yjbb_em(date=period)
        except Exception:      # noqa: BLE001
            continue
        for _, r in d.iterrows():
            ind = r.get("所处行业")
            if isinstance(ind, str) and ind:
                out.setdefault(str(r["股票代码"]).zfill(6), ind)
        if len(out) > 3000:
            break
    json.dump(out, open(p, "w"), ensure_ascii=False)
    return out


# ══════════════════════════════════════════════════════════════════════════════
# 财务：新浪财务摘要（扣非 / 归母净利 / 经营现金流）
# ══════════════════════════════════════════════════════════════════════════════
FIN_WANT = ("归母净利润", "扣非净利润", "经营现金流量净额")


def _fin_one(code: str) -> dict | None:
    import akshare as ak
    for att in range(3):
        try:
            with _quiet_ak():
                d = ak.stock_financial_abstract(symbol=code)
            if d is None or not len(d):
                return None
            periods = [c for c in d.columns if str(c).isdigit() and len(str(c)) == 8]
            rec: dict = {}
            for _, row in d.iterrows():
                k = str(row["指标"])
                if k in FIN_WANT and k not in rec:          # 同名指标在多个「选项」下重复，取第一次
                    rec[k] = {p: float(row[p]) for p in periods if row[p] == row[p]}
            return rec
        except Exception:      # noqa: BLE001
            time.sleep(1.5 * (att + 1))
    return None


def financials(codes: list[str], offline: bool, workers: int = 4) -> dict:
    """{code: {指标: {报告期: 值}}}。7 天 TTL（财报按季更新）。"""
    p = _cpath("fin.json")
    cache = json.load(open(p)) if os.path.exists(p) else {}
    stamp = cache.get("_ts", {})
    now = time.time()
    need = [c for c in codes if c not in cache or now - stamp.get(c, 0) > 7 * 86400]
    if need and not offline:
        print(f"  … 拉财务摘要 {len(need)} 只（新浪，{workers} 并发）", file=sys.stderr, flush=True)
        done = 0
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(_fin_one, c): c for c in need}
            for f in as_completed(futs):
                c = futs[f]
                r = f.result()
                if r is not None:
                    cache[c] = r
                    stamp[c] = now
                done += 1
                if done % 50 == 0:
                    print(f"    {done}/{len(need)}", file=sys.stderr, flush=True)
        cache["_ts"] = stamp
        json.dump(cache, open(p, "w"), ensure_ascii=False)
    return cache


# ══════════════════════════════════════════════════════════════════════════════
# 规则
# ══════════════════════════════════════════════════════════════════════════════
def _latest(series: dict) -> str | None:
    ks = sorted(series)
    return ks[-1] if ks else None


def _yoy(series: dict, period: str) -> tuple[float | None, str]:
    """累计值同比。基数 ≤ 0 时不给百分比，只给方向。"""
    prev = f"{int(period[:4]) - 1}{period[4:]}"
    if period not in series or prev not in series:
        return None, "缺同期"
    a, b = series[period], series[prev]
    if b > 0:
        return a / b - 1, ""
    return (math.inf if a > b else -math.inf), ("亏转盈/减亏" if a > b else "亏损扩大")


def _ttm(series: dict, period: str) -> float | None:
    if period.endswith("1231"):
        return series.get(period)
    y = int(period[:4])
    fy, prev = f"{y - 1}1231", f"{y - 1}{period[4:]}"
    if fy in series and prev in series and period in series:
        return series[fy] - series[prev] + series[period]
    return None


def evaluate(code: str, *, divs: dict, mc: dict, fin: dict | None, ind: str,
             rules: str, today: dt.date, p12: float | None = None, p12_med: float | None = None) -> dict:
    """单只：算股息率与各判据，给 action ∈ BUY / HOLD / SELL / NO。
    p12 = 过去一年前复权涨跌，p12_med = 全部长期分红股的中位数（相对飞刀用）。"""
    p12x = (p12 - p12_med) if (p12 is not None and p12_med is not None) else None
    row = {"code": code, "name": mc.get("name", ""), "industry": ind or "?",
           "mcap_yi": mc.get("mcap", 0) / 1e8, "flags": [], "why": [], "p12": p12, "p12x": p12x}
    fin = fin or {}
    ni, kf, ocf = (fin.get(k, {}) for k in FIN_WANT)

    # ── 1. 「上年」= 最近一个已公布年报的财年 ─────────────────────────────────
    fy = today.year - 1
    if f"{fy}1231" not in ni:            # 年报还没出（每年 1~4 月）→ 再往前一年
        fy -= 1
    row["fy"] = fy
    cash = divs.get(fy, 0.0)
    mcap = mc.get("mcap") or 0
    row["yield"] = cash / mcap if mcap else 0.0

    # ── 2. 连续分红年数（含中报分红；截至上年）────────────────────────────────
    n = 0
    y = fy
    while divs.get(y, 0) > 0:
        n += 1
        y -= 1
    row["years"] = n

    # ── 3. 分红怪异：突增 / 分红率 ────────────────────────────────────────────
    prior = [divs.get(fy - k, 0.0) for k in range(1, 5)]
    med = statistics.median(prior) if any(prior) else 0.0
    row["spike"] = bool(med > 0 and cash > SPIKE_X * med)
    ni_fy = ni.get(f"{fy}1231")
    row["payout"] = cash / ni_fy if ni_fy and ni_fy > 0 else None
    p_last = _latest(ni)
    ni_ttm = _ttm(ni, p_last) if p_last else None
    # 保底分红率（《释疑帖》第五条）：股价不变、明年要保住 4% 股息率，需拿出 TTM 归母净利的多少
    #   = 4% × 市值 ÷ TTM 净利 = 4% × PE(TTM)。> 100% ⇔ 靠利润保不住 4%（作者 111 笔买入只有 5 笔 > 100%）
    row["floor_payout"] = BUY_YIELD * mcap / ni_ttm if ni_ttm and ni_ttm > 0 and mcap else None
    # 可持续股息率 = min(股息率, TTM 盈利收益率)：分的比赚的多，就只按利润能撑住的部分算。
    #   买入清单按它排序（2026-10-02 定）—— 按静态股息率排会把「一次性超额分红」排到最前
    #   （当时前 20 只 11 只上年分红率 >100%，股息率中位 7.6%，作者持仓中位 5.2%）
    ey = ni_ttm / mcap if (ni_ttm and mcap) else 0.0
    row["sus_yield"] = min(row["yield"], max(ey, 0.0))
    # 预期股息率：上年分红率不变、按 TTM 利润分 → 明年大概能拿多少（只作参考，不进判定）
    row["fwd_yield"] = (row["payout"] * ni_ttm / mcap) if (row["payout"] and ni_ttm and mcap) else None
    pk_ = _latest(kf)
    kf_ttm = _ttm(kf, pk_) if pk_ else None
    row["fwd_yield_kf"] = (row["payout"] * kf_ttm / mcap) if (row["payout"] and kf_ttm and mcap) else None
    over = 0
    y = fy
    while ni.get(f"{y}1231", 0) > 0 and divs.get(y, 0) / ni[f"{y}1231"] > 1.0:
        over += 1
        y -= 1
    if over:
        row["flags"].append(f"分红>利润{over}年")

    # ── 4. 季报：最新一期累计扣非同比 ─────────────────────────────────────────
    pk = _latest(kf)
    row["kf_period"] = pk
    row["kf_yoy"], kf_note = _yoy(kf, pk) if pk else (None, "无扣非")
    if kf_note:
        row["flags"].append(kf_note)

    # ── 5. 2.0：现金流含金量（最新财年 经营现金流 / 归母净利）──────────────────
    ocf_fy = ocf.get(f"{fy}1231")
    is_fin = any(k in (ind or "") for k in FIN_INDUSTRIES)
    row["ocf_ni"] = (ocf_fy / ni_fy) if (ocf_fy is not None and ni_fy and ni_fy > 0) else None
    if is_fin:
        row["flags"].append("金融业·现金流不适用")
    if any(k in (ind or "") for k in CYCLICAL_HINT):
        row["flags"].append("强周期·看PBR")

    # ── 判定 ─────────────────────────────────────────────────────────────────
    y_ = row["yield"]
    kfy = row["kf_yoy"]
    kf_bad = kfy is not None and kfy < KF_SELL
    sell_why, review = [], []
    if y_ <= SELL_YIELD:
        sell_why.append(f"股息率{y_:.2%}≤3%")
    if kf_bad and rules != "observed":
        sell_why.append(f"扣非累计同比{_pct(kfy)}<-10%")
    elif kf_bad:
        # observed（作者 2.0 实盘拟合）：A 扣非 < -30%，或 B 扣非口径预期股息率 < 4% → 卖；否则只提示复核
        fk = row["fwd_yield_kf"]
        if kfy < KF_HARD:
            sell_why.append(f"扣非累计同比{_pct(kfy)}<-30%")
        elif fk is None or fk < FWD_KF_MIN:
            sell_why.append(f"扣非{_pct(kfy)}且扣非口径预期股息率{'—' if fk is None else f'{fk:.2%}'}<4%")
        else:
            review.append(f"扣非累计同比{_pct(kfy)}<-10%（预期股息率{fk:.2%}≥4%，继续持有）")
    row["sell_why"], row["review"] = sell_why, review

    buy_block = []
    if y_ < BUY_YIELD:
        buy_block.append(f"股息率{y_:.2%}<4%")
    if n < MIN_YEARS:
        buy_block.append(f"连续分红{n}年<{MIN_YEARS}")
    if kf_bad:
        # 三档都挡买入：observed 只是把「卖出」放宽成 A∨B，复核状态可以继续持有、不能新买（四进三出式的不对称）
        buy_block.append("扣非<-10%")
    if kfy is None:
        buy_block.append("缺扣非数据")
    if row["spike"]:
        buy_block.append(f"分红怪异(>{SPIKE_X:.0f}×前4年中位)")
    if over >= PAYOUT_OVER_YEARS:
        buy_block.append(f"分红>利润连续{over}年")
    if not ni_ttm or ni_ttm <= 0:
        buy_block.append("TTM 亏损")
    elif row["floor_payout"] is not None and row["floor_payout"] > FLOOR_PAYOUT_MAX:
        buy_block.append(f"保底分红率{row['floor_payout']:.0%}>100%")
    # 不接飞刀 / 不买利润暴增（docs/04）
    if p12x is not None and p12x < KNIFE_REL:
        buy_block.append(f"飞刀：一年{p12:+.0%}，比分红股中位多跌{-p12x:.0%}")
    if p_last:
        prev = f"{int(p_last[:4]) - 1}{p_last[4:]}"
        ni_prev = _ttm(ni, prev) if prev in ni else None
        row["ni_ttm_yoy"] = (ni_ttm / ni_prev - 1) if (ni_ttm and ni_prev and ni_prev > 0) else None
        if row["ni_ttm_yoy"] is not None and row["ni_ttm_yoy"] > PROFIT_SURGE:
            buy_block.append(f"利润暴增：TTM 净利同比{row['ni_ttm_yoy']:+.0%}")
    if rules == "strict":
        if kfy is not None and kfy <= KF_BUY_STRICT:
            buy_block.append("strict:扣非未增长")
        if not is_fin and row["ocf_ni"] is not None and row["ocf_ni"] < OCF_NI_STRICT:
            buy_block.append(f"strict:现金流/净利{row['ocf_ni']:.2f}<0.6")
    if sell_why and not buy_block:
        buy_block.append("触发卖出条件")         # 防止 observed 下买进即被卖
    row["buy_block"] = buy_block
    row["action"] ="BUY" if not buy_block else ("SELL" if sell_why else "HOLD_ONLY")
    return row


def _pct(x) -> str:
    if x is None:
        return "—"
    if x == math.inf:
        return "转正"
    if x == -math.inf:
        return "恶化"
    return f"{x:+.1%}"


# ══════════════════════════════════════════════════════════════════════════════
# 组合：行业上限 + 不出则不进
# ══════════════════════════════════════════════════════════════════════════════
def pick(candidates: list[dict], slots: int, held_inds: list[str], n_free: int) -> list[dict]:
    per_ind = max(1, int(INDUSTRY_CAP * slots + 1e-9))
    cnt: dict = {}
    for i in held_inds:
        cnt[i] = cnt.get(i, 0) + 1
    out = []
    for r in candidates:
        if len(out) >= n_free:
            break
        if cnt.get(r["industry"], 0) >= per_ind:
            continue
        cnt[r["industry"]] = cnt.get(r["industry"], 0) + 1
        out.append(r)
    return out


# ══════════════════════════════════════════════════════════════════════════════
# 输出
# ══════════════════════════════════════════════════════════════════════════════
def _line(r: dict, extra: str = "") -> str:
    fp, oc, fw = r.get("floor_payout"), r.get("ocf_ni"), r.get("fwd_yield")
    note = " ".join(x for x in (extra, "·".join(r["flags"])) if x)
    return (f"| {r['code']} {r['name']} | {r['industry']} | {r['yield']:.2%} | {r['sus_yield']:.2%} | "
            f"{'—' if fw is None else f'{fw:.2%}'} | {r['years']} | "
            f"{_pct(r['kf_yoy'])} ({(r['kf_period'] or '')[2:]}) | "
            f"{'—' if fp is None else f'{fp:.0%}'} | {'—' if oc is None else f'{oc:.2f}'} | "
            f"{r['mcap_yi']:.0f} | {note} |")


BLOCK_LABELS = (("strict:扣非", "strict 扣非未增长"), ("strict:现金流", "strict 现金流/净利<0.6"),
                ("连续分红", "连续分红不足"), ("扣非<", "扣非<-10%"), ("缺扣非", "缺扣非数据"),
                ("保底分红率", "保底分红率>100%"), ("TTM", "TTM 亏损"), ("分红怪异", "分红怪异"),
                ("分红>利润连续", "分红>利润连续≥2年"), ("触发卖出", "触发卖出条件"),
                ("飞刀", "飞刀：比分红股中位多跌超25%"), ("利润暴增", "利润暴增：净利同比>+70%"))

HDR = ("| 标的 | 行业 | 股息率 | 可持续股息率 | 预期股息率 | 连续分红 | 扣非累计同比(期) | 保底分红率 | 现金流/净利 | 市值亿 | 备注 |\n"
       "|---|---|---|---|---|---|---|---|---|---|---|")


def tradable(code: str, name: str) -> bool:
    return (code[:1] in "036" and not code.startswith(("200", "900"))
            and "ST" not in name.upper() and "退" not in name)


def scan(rules: str, held=(), only=(), *, offline: bool = False, today: dt.date | None = None):
    """取数 + 逐只判定。返回 (rows{code: row}, 粗筛池, 市值表)。纸面账户 paper.py 复用。"""
    today = today or dt.date.today()
    print("… 分红送配（东财，按报告期）", file=sys.stderr, flush=True)
    divs = dividend_history(today, HIST_YEARS, offline)
    print("… 市值（TickFlow 股本 × 收盘价）", file=sys.stderr, flush=True)
    mcs = market_caps(offline)
    print("… 行业（东财业绩报表）", file=sys.stderr, flush=True)
    inds = industries(today, offline)

    # ── 粗筛：可交易 A 股 + 上年（按日历）股息率 ≈≥3% 且足够年数有分红记录 ──────────
    pool = []
    for code, mc in mcs.items():
        if not tradable(code, mc.get("name", "")) or not mc.get("mcap"):
            continue
        d = divs.get(code, {})
        best = max(d.get(today.year - 1, 0), d.get(today.year - 2, 0))
        if best / mc["mcap"] >= SELL_YIELD * 0.9 and sum(1 for v in d.values() if v > 0) >= MIN_YEARS:
            pool.append(code)
    want = sorted(set(only) | set(held)) if only else sorted(set(pool) | set(held))
    fin = financials(want, offline)
    # 相对飞刀的基准：全部「连续分红 ≥4 年」的可交易 A 股（与回测口径一致，不只是高股息的粗筛池）
    def streak(d):
        y = max(today.year - 1, max(d) if d else 0)
        y = y if d.get(y, 0) > 0 else y - 1
        n = 0
        while d.get(y - n, 0) > 0:
            n += 1
        return n
    bench = [c for c, mc in mcs.items() if tradable(c, mc.get("name", "")) and streak(divs.get(c, {})) >= MIN_YEARS]
    print(f"… 过去一年涨跌（相对飞刀；基准 {len(bench)} 只长期分红股）", file=sys.stderr, flush=True)
    p1y = price_change_1y(sorted(set(want) | set(bench)), offline)
    bv = [p1y[c] for c in bench if c in p1y]
    p12_med = statistics.median(bv) if len(bv) >= 100 else None
    if p12_med is not None:
        print(f"… 长期分红股一年涨跌中位 {p12_med:+.1%}（{len(bv)} 只）", file=sys.stderr, flush=True)

    rows = {}
    for code in want:
        if code not in mcs:
            rows[code] = {"code": code, "name": "?", "missing": True}
            continue
        rows[code] = evaluate(code, divs=divs.get(code, {}), mc=mcs[code], fin=fin.get(code),
                              ind=inds.get(code, ""), rules=rules, today=today, p12=p1y.get(code), p12_med=p12_med)
    return rows, pool, mcs


def latest_cube_snapshot() -> dict:
    """data/xueqiu/holdings/ 下最新一份作者持仓快照（sync_xueqiu.py 写入）。"""
    d = os.path.join(HERE, "data", "xueqiu", "holdings")
    f = sorted(x for x in os.listdir(d) if x.endswith(".csv"))[-1]
    import csv
    return {"date": f[:-4], "rows": list(csv.DictReader(open(os.path.join(d, f))))}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rules", choices=("1.0", "observed", "strict"), default="1.0",
                    help="1.0 = 原作者公开的机械规则（缺省）；observed = 作者 2.0 实盘口径（扣非不挡买入，"
                         "扣非大跌只提示复核）；strict = 1.0 + 跟随者转述的 扣非>0、现金流/净利>0.6")
    ap.add_argument("--slots", type=int, default=30, help="组合只数（等权，单只 = 1/slots，不超过 10%%）")
    ap.add_argument("--top", type=int, default=40, help="买入清单显示前 N 只")
    ap.add_argument("--hold", default="", help="持仓代码，逗号分隔：做体检并给换仓建议")
    ap.add_argument("--hold-file", default="", help="持仓文件（每行一个代码，# 注释）")
    ap.add_argument("--cube", action="store_true", help="用 data/xueqiu/ 里作者最新持仓快照当持仓（校验用）")
    ap.add_argument("--paper", action="store_true", help="用纸面账户 paper/account.json 的持仓当持仓（体检 + 换仓建议）")
    ap.add_argument("--codes", default="", help="只看这几只的明细（不给组合建议）")
    ap.add_argument("--min-mcap", type=float, default=0.0, help="买入清单的市值下限（亿；原作者无此限，缺省 0）")
    ap.add_argument("--offline", action="store_true", help="只读缓存")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    slots = max(a.slots, int(1 / STOCK_CAP))
    today = dt.date.today()

    held: list[str] = []
    if a.hold:
        held += [c.strip().split(".")[0][-6:] for c in a.hold.split(",") if c.strip()]
    if a.hold_file:
        for ln in open(a.hold_file):
            ln = ln.split("#")[0].strip()
            if ln:
                held.append(ln.split(",")[0].split(".")[0][-6:])
    if a.cube:
        snap = latest_cube_snapshot()
        held += [r["symbol"][-6:] for r in snap["rows"]]
        print(f"… 作者持仓快照 {snap['date']}", file=sys.stderr)
    if a.paper:
        acc = json.load(open(os.path.join(STATE, "paper", "account.json"), encoding="utf-8"))
        held += list(acc["positions"])
        a.slots = acc["slots"]
        slots = max(a.slots, int(1 / STOCK_CAP))
        print(f"… 纸面账户持仓 {len(acc['positions'])} 只（规则 {acc['rules']}）", file=sys.stderr)
    only = [c.strip()[-6:] for c in a.codes.split(",") if c.strip()]

    rows, pool, _ = scan(a.rules, held, only, offline=a.offline, today=today)

    if a.json:
        json.dump(rows, sys.stdout, ensure_ascii=False, indent=1, default=str)
        return

    w = 1 / slots
    print(f"# 股息率模式·四进三出（规则 {a.rules}）— {today}")
    print(f"组合 {slots} 只 × {w:.1%}，单行业 ≤ {int(INDUSTRY_CAP * slots)} 只（{INDUSTRY_CAP:.0%}）"
          f" · 粗筛 {len(pool)} 只（股息率≈≥3% 且 ≥{MIN_YEARS} 年有分红记录）")

    if only:
        print("\n## 明细\n" + HDR)
        for c in only:
            r = rows.get(c)
            if not r or r.get("missing"):
                print(f"| {c} | 无市值数据 | | | | | | | |")
                continue
            print(_line(r, f"**{r['action']}** " + "；".join(r["buy_block"] + r["sell_why"] + r["review"])))
        return

    buys = sorted((r for r in rows.values() if not r.get("missing") and r["action"] == "BUY"
                   and r["mcap_yi"] >= a.min_mcap), key=lambda r: (-r["sus_yield"], -r["yield"]))

    if held:
        print(f"\n## 持仓体检（{len(held)} 只）\n" + HDR)
        keep, sell = [], []
        for c in held:
            r = rows.get(c)
            if not r or r.get("missing"):
                print(f"| {c} | 无市值数据（停牌/退市/代码错？）| | | | | | | |")
                continue
            if r["sell_why"]:
                sell.append(r)
                print(_line(r, "**SELL** " + "；".join(r["sell_why"])))
            else:
                keep.append(r)
                tag = "持有" + ("（仍可买）" if r["action"] == "BUY" else "（持有不加：" + "；".join(r["buy_block"]) + "）")
                if r["review"]:
                    tag = "**复核** " + "；".join(r["review"]) + " · " + tag
                print(_line(r, tag))
        n_free = max(0, slots - len(keep))
        fills = pick([b for b in buys if b["code"] not in held], slots, [r["industry"] for r in keep], n_free)
        print(f"\n## 换仓建议（不出则不进）：卖 {len(sell)} 只，空位 {n_free} 个")
        if not sell and n_free == 0:
            print("无动作。")
        for r in sell:
            print(f"- 卖出 {r['code']} {r['name']}：{'；'.join(r['sell_why'])}")
        for r in fills:
            print(f"- 买入 {r['code']} {r['name']}（{r['industry']}，股息率 {r['yield']:.2%}）→ 目标 {w:.1%}")
        if n_free > len(fills):
            print(f"- 还有 {n_free - len(fills)} 个空位买不满（清单不足或行业已满）→ 按原作者做法留现金/红利ETF，不降标准")
        # 升级换股：作者实盘有「卖掉仍 ≥4% 的、换股息率更高的」操作（交行→平安银行、立霸→联明），阈值未公开 → 只列不动
        cand = [b for b in buys if b["code"] not in held and b not in fills]
        low = sorted(keep, key=lambda r: r["yield"])[:3]
        if cand and low:
            print("\n可选·升级换股（作者有此类操作、无公开阈值，仅供参考）：持仓股息率最低 "
                  + "、".join(f"{r['name']} {r['yield']:.2%}" for r in low)
                  + f" vs 清单未持有头部 {cand[0]['name']} {cand[0]['yield']:.2%}")

    print(f"\n## 买入清单（满足全部买入条件，按股息率降序；共 {len(buys)} 只，显示前 {a.top}）\n" + HDR)
    for r in buys[:a.top]:
        print(_line(r))
    blocked = {}
    for r in rows.values():
        if r.get("missing") or r["action"] == "BUY" or r["yield"] < BUY_YIELD:
            continue
        for b in r["buy_block"]:
            k = next((lab for pre, lab in BLOCK_LABELS if pre in b), b)
            blocked[k] = blocked.get(k, 0) + 1
    if blocked:
        print("\n股息率 ≥4% 但被挡下的原因计数：" + "，".join(f"{k} {v}" for k, v in sorted(blocked.items(), key=lambda x: -x[1])))
    print("\n> 只是规则清单，不是投资建议。周期股（强周期·看PBR）的高股息常是峰值利润，见 docs/01 §4。")


if __name__ == "__main__":
    main()
