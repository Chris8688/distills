"""思想钢印 · 两条「勉强通过」的可量化规则（纯函数）。

  E1 高 ROE 均值回归   ROE > 30% 且不是薄权益造成
  E2 增速见顶           营收 TTM 同比近 8 季峰值 ≥100%，现 ≤30% 且没再加速

效应弱、跨市场不稳，只供回测复现，不作选股工具。返回 (bool|None, 说明)，None = 数据不足。
"""
from __future__ import annotations

from typing import Optional, Sequence

ROE_HIGH = 0.30          # 25%~40% 美股两段都过；50% 失效
EQ_ASSETS_MIN = 0.20     # 美股：权益/资产 ≥20% 才算「不是薄权益」（回购打薄的 ROE 不算）
DEBT_RATIO_MAX = 0.80    # A股：资产负债率 ≤80%（银行/券商天然高杠杆，不进这条）
PEAK_YOY = 1.0
NOW_YOY = 0.30


def e1_high_roe(roe: Optional[float], *, eq_to_assets: Optional[float] = None,
                debt_ratio: Optional[float] = None) -> tuple[Optional[bool], str]:
    """ROE 是小数（0.35 = 35%）。美股传 eq_to_assets，A股传 debt_ratio（小数）。"""
    if roe is None:
        return None, "无 ROE"
    if eq_to_assets is not None:
        thin = eq_to_assets < EQ_ASSETS_MIN
        lev_txt = f"权益/资产 {eq_to_assets:.0%}"
    elif debt_ratio is not None:
        thin = debt_ratio > DEBT_RATIO_MAX
        lev_txt = f"负债率 {debt_ratio:.0%}"
    else:
        return None, f"ROE {roe:.0%}，缺杠杆数据无法排除薄权益"
    if roe > ROE_HIGH and not thin:
        return True, f"ROE {roe:.0%} >{ROE_HIGH:.0%}（{lev_txt}）→ 高 ROE 均值回归，缺省不新建仓"
    if roe > ROE_HIGH and thin:
        return False, f"ROE {roe:.0%} 但 {lev_txt}：薄权益/高杠杆造成，不适用 E1"
    return False, f"ROE {roe:.0%}"


def e2_growth_peak(rev_yoy: Sequence[float], unit: str = "季") -> tuple[Optional[bool], str]:
    """rev_yoy：营收 TTM 同比（小数），按季、最新在最后，至少 3 个点。
    unit='年' 给只有年报的 yfinance 近似口径用（只改文字，判据不变 —— 未单独检验过）。"""
    ys = list(rev_yoy)
    if len(ys) < 3:
        return None, f"营收同比不足 3 {unit}"
    pk = max(ys[-8:])
    hit = pk >= PEAK_YOY and ys[-1] <= NOW_YOY and ys[-1] <= ys[-2]
    txt = f"近 8 {unit}营收同比峰值 {pk:+.0%} → 现 {ys[-1]:+.0%}（上{unit} {ys[-2]:+.0%}）"
    return hit, txt + (" → 增速见顶、未再加速，缺省不新建仓" if hit else "")
