"""backtest.py / improve.py 的规则状态机（不连网，合成 K 线）。运行：python3 -m pytest tests/"""
from __future__ import annotations

import datetime as dt
import importlib.util
import os

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))   # 本蒸馏目录
spec = importlib.util.spec_from_file_location(
    "dlm_backtest", os.path.join(ROOT, "backtest.py"))
bt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bt)


def _bars(closes):
    d0 = dt.date(2020, 1, 1)
    return [{"date": (d0 + dt.timedelta(days=i)).isoformat(), "open": c, "close": c,
             "high": c, "low": c} for i, c in enumerate(closes)]


def _path(ma=10):
    # 缓涨（乖离 >1，不买）→ 跌破均线（买）→ 拉升超过均线 7%（卖）→ 再跌破（买）
    return [1.0 + 0.01 * i for i in range(ma)] + [0.95] * 3 + [1.2] * 3 + [1.0] * 15 + [0.8] * 3


def test_buy_below_ma_sell_above_7pct():
    bars = _bars(_path())
    tr, nav = bt.run(bars, ma=10, start=bars[0]["date"], end=bars[-1]["date"])
    assert [t[1] for t in tr] == ["BUY", "SELL", "BUY"]
    assert tr[0][0] == bars[10]["date"]               # 当日收盘信号 0.95/均线 ≤ 1
    assert tr[1][0] == bars[13]["date"]               # 1.2 / 均线 > 1.07
    assert len(nav) == len(bars)


def test_next_open_fills_one_day_later():
    bars = _bars(_path())
    a, _ = bt.run(bars, ma=10, start=bars[0]["date"], end=bars[-1]["date"])
    b, _ = bt.run(bars, ma=10, start=bars[0]["date"], end=bars[-1]["date"], fill="next_open")
    idx = {x["date"]: i for i, x in enumerate(bars)}
    assert [idx[t[0]] + 1 for t in a] == [idx[t[0]] for t in b]


def test_pe_gate_blocks_buy():
    bars = _bars(_path())
    pe = {bars[0]["date"]: 25.0}                       # 前值填充 → 全程 25 > 20
    tr, _ = bt.run(bars, ma=10, pe=pe, start=bars[0]["date"], end=bars[-1]["date"])
    assert tr == []


def test_trade_detail_marks_open_position():
    bars = _bars(_path())
    tr, _ = bt.run(bars, ma=10, start=bars[0]["date"], end=bars[-1]["date"])
    det = bt.trade_detail(bars, tr)
    assert det[0][3] > 0 and not det[0][5]              # 第一笔已平且盈利
    assert det[-1][5]                                   # 最后一笔未平


def test_author_trade_log_is_alternating():
    rows = bt.author_trades()
    assert len(rows) == 25
    assert [r["side"] for r in rows] == ["BUY", "SELL"] * 12 + ["BUY"]


# ---------------- improve.py（v2 波动率缩放） ----------------
spec2 = importlib.util.spec_from_file_location(
    "dlm_improve", os.path.join(ROOT, "improve.py"))
im = importlib.util.module_from_spec(spec2)
spec2.loader.exec_module(im)


def _noisy(n=400, drift=0.0, amp=0.01, seed=1):
    import random
    rnd = random.Random(seed)
    px, out = 1.0, []
    for _ in range(n):
        px *= 1 + drift + rnd.gauss(0, amp)
        out.append(px)
    return out


def test_v2_without_tv_matches_original_signal_path():
    bars = _bars(_noisy())
    nav0, _, st0 = im.run(bars, fee=0)
    tr, nav1 = bt.run(bars, fill="next_open", start=bars[182]["date"], end=bars[-1]["date"])
    assert abs(nav0[-1][1] - nav1[-1][1]) < 1e-9          # tv=None、无费用 → 与原规则逐日一致
    assert st0["signal"] == (1 if tr and tr[-1][1] == "BUY" else 0)


def test_v2_scales_weight_down_when_vol_high():
    bars = _bars(_noisy(amp=0.03))                          # 年化波动约 48%
    _, _, st = im.run(bars, tv=0.12, buy=10.0, sell=99.0)   # 强制一直持有
    assert st["signal"] == 1
    assert 0.15 < st["target_weight"] < 0.40                # ≈ 12% / 48%


def test_v2_full_weight_when_vol_low():
    bars = _bars(_noisy(amp=0.002))
    _, _, st = im.run(bars, tv=0.12, buy=10.0, sell=99.0)
    assert st["target_weight"] == 1.0


def test_v2_flat_means_zero_weight():
    bars = _bars(_noisy())
    _, _, st = im.run(bars, tv=0.12, buy=0.0)               # 乖离永远 > 0 → 从不买
    assert st["signal"] == 0 and st["target_weight"] == 0.0


# ---------------- v3 估值闸门 ----------------

def test_spread_gate_threshold_and_asof():
    g = im.spread_gate({"2020-01-01": -2.0, "2020-01-10": 0.5}, -1.0)
    assert g("2019-12-31") is True            # 数据开始前放行
    assert g("2020-01-05") is False           # 用最近一个有数据日（−2.0 < −1.0）
    assert g("2020-01-10") is True and g("2020-02-01") is True


def test_gate_blocks_new_buys_only():
    bars = _bars(_noisy())
    _, _, open_ = im.run(bars, buy=10.0, sell=99.0, gate=lambda d: True)
    _, _, shut = im.run(bars, buy=10.0, sell=99.0, gate=lambda d: False)
    assert open_["signal"] == 1 and shut["signal"] == 0
    # 已持仓后闸门关闭不触发卖出
    mid = bars[250]["date"]
    _, _, held = im.run(bars, buy=10.0, sell=99.0, gate=lambda d: d < mid)
    assert held["signal"] == 1


# ---------------- paper.py（纸面账户，不连网） ----------------
def _paper(monkeypatch, tmp_path):
    monkeypatch.setenv("DISTILL_STATE", str(tmp_path))
    spec3 = importlib.util.spec_from_file_location("dlm_paper", os.path.join(ROOT, "paper.py"))
    pp = importlib.util.module_from_spec(spec3)
    spec3.loader.exec_module(pp)
    pp.PDIR = str(tmp_path / "paper")
    pp.ACC, pp.TRD, pp.NAV, pp.NOTION = (os.path.join(pp.PDIR, x) for x in
                                         ("account.json", "trades.csv", "nav.csv", "notion.json"))
    monkeypatch.setattr(pp.I, "market_spread", lambda max_age_h=None: {"2000-01-01": 1.0})
    monkeypatch.setattr(pp.I, "load_pe", lambda ix, max_age_h=None: None)
    return pp


def _feed(pp, monkeypatch, closes, split_at=None):
    bars = _bars(closes)
    raw = {b["date"]: dict(b) for b in bars}
    if split_at is not None:                       # split_at 之前的不复权价 ×2（模拟 1 拆 2）
        for b in bars[:split_at]:
            for k in ("open", "close", "high", "low"):
                raw[b["date"]][k] = b[k] * 2
    state = {"n": len(bars)}
    monkeypatch.setattr(pp, "_bars", lambda code: ({d: raw[d] for d in list(raw)[:state["n"]]}, bars[:state["n"]]))
    monkeypatch.setattr(pp, "session_today", lambda: dt.date.fromisoformat(bars[state["n"] - 1]["date"]))
    return state, bars


def test_paper_fills_next_open_and_is_idempotent(monkeypatch, tmp_path):
    pp = _paper(monkeypatch, tmp_path)
    closes = _noisy(400, amp=0.002)
    state, bars = _feed(pp, monkeypatch, closes)
    state["n"] = 300
    pp.init(1_000_000, "512890", 0.18)
    monkeypatch.setattr(pp.I, "run", lambda fwd, **k: (None, None, {"signal": 1, "target_weight": 1.0, "dev": 0.99, "vol": 0.05}))
    pp.run(False, False)
    acc = pp._load()
    assert acc["pending"]["target"] == 1.0 and acc["shares"] == 0          # 当天只挂单
    pp.run(False, False)                                                   # 同一交易日重跑不动
    assert pp._load()["shares"] == 0
    state["n"] = 301
    pp.run(False, False)
    acc = pp._load()
    px = bars[300]["open"]
    assert acc["shares"] > 0 and acc["shares"] % 100 == 0                  # 下一个交易日开盘成交，整百
    assert acc["cash"] >= 0 and acc["cash"] < px * 100 + 50
    t = pp._rows(pp.TRD)
    assert t[0]["side"] == "BUY" and abs(float(t[0]["price"]) - px) < 1e-4


def test_paper_split_adjusts_shares(monkeypatch, tmp_path):
    pp = _paper(monkeypatch, tmp_path)
    closes = _noisy(400, amp=0.002)
    state, bars = _feed(pp, monkeypatch, closes, split_at=302)
    monkeypatch.setattr(pp.I, "run", lambda fwd, **k: (None, None, {"signal": 1, "target_weight": 1.0, "dev": 0.99, "vol": 0.05}))
    state["n"] = 300
    pp.init(1_000_000, "512890", 0.18)
    pp.run(False, False)
    state["n"] = 301
    pp.run(False, False)
    sh = pp._load()["shares"]
    state["n"] = 303                                                       # 跨过拆分日
    pp.run(False, False)
    acc = pp._load()
    assert abs(acc["shares"] - sh * 2) < 1e-6
    assert pp._rows(pp.TRD)[-1]["side"] == "ADJ"
