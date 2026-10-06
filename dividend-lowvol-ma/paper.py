#!/usr/bin/env python3
"""红利低波均线 v3 · 纸面账户（初始 1000 万人民币，标的 512890）—— 跑 v3 规则，不下真单。

账本是本地文件（唯一事实源）；Notion 只是镜像，由 Claude 用 Notion MCP 同步（见 SKILL.md）。
账本目录 = $DISTILL_STATE/paper/（缺省 ~/.local/share/distills/dividend-lowvol-ma/paper/）：
  account.json   参数 + 现金 + 持仓 + 待执行订单
  trades.csv     流水：BUY / SELL / ADJ（份额拆分 / 分红折算成份额）
  nav.csv        每个交易日一行：现金、市值、总资产、净值、仓位、信号、目标仓位、乖离、波动、股债差，并排 512890 买持净值
  notion.json    Notion 同步进度（数据库 id、已同步的流水 / 净值、持仓行 page id）

规则（v3，与 improve.py 一致，docs/04）：
  · 信号：收盘 / MA182 ≤ 1 且指数 PE ≤ 20 且「上证 A 股股息率 − 10 年国债」≥ −1.0pp → 持有；收盘 / MA182 > 1.07 → 空仓
  · 仓位：持有时目标 = min(1, 18% ÷ 近 60 日年化波动)；空仓目标 0
  · 调仓：新建 / 清仓立即；否则偏离 >10pp 立即，或距上次成交 ≥5 个交易日且偏离 >2pp
成交口径（与回测一致）：
  · 每天北京时间收盘后跑：先按**当天开盘价**执行上一个交易日收盘出的订单，再用当天收盘价算新信号、挂下一个交易日的单。
    同一个交易日只跑一次，重跑只打印（--force 可强制）。当天没有 K 线（停牌）→ 订单顺延。
  · 一手 100 股向下取整；佣金 0.025%（最低 5 元），ETF 免印花税、过户费。
  · 份额拆分 / 基金分红：比较前复权与不复权的比值，变动时把持仓折算成等价份额（分红视为再投入，ETF 分红个人免税），记一笔 ADJ。

用法：
  python3 paper.py init [--cash 10000000 --code 512890 --tv 0.18]
  python3 paper.py run [--dry-run] [--force]
  python3 paper.py status
  python3 paper.py notion-pending          # 给 Claude 看：还没同步到 Notion 的流水 / 净值 / 持仓
  python3 paper.py notion-mark --trades 1,2 --nav 2026-10-08 [--pos <page_id>] [--db k=v,...]
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)

import improve as I  # noqa: E402
from lib import STATE  # noqa: E402
from lib import prices as P  # noqa: E402

PDIR = os.path.join(STATE, "paper")
ACC = os.path.join(PDIR, "account.json")
TRD = os.path.join(PDIR, "trades.csv")
NAV = os.path.join(PDIR, "nav.csv")
NOTION = os.path.join(PDIR, "notion.json")
TRD_COLS = ["id", "session", "code", "name", "side", "shares", "price", "amount", "fee", "cash_after", "reason"]
NAV_COLS = ["session", "cash", "market_value", "total", "nav", "return_pct", "weight_pct", "signal", "target_pct",
            "dev", "vol", "spread", "gate", "bh_nav"]
COMMISSION, COMMISSION_MIN = 0.00025, 5.0
NAMES = {"512890": "红利低波ETF华泰柏瑞", "563020": "红利低波ETF易方达", "515080": "中证红利ETF招商",
         "515180": "红利ETF易方达", "510880": "红利ETF华泰柏瑞"}
INDEX = {"512890": "CSIH30269", "563020": "CSIH30269", "515080": "SH000922", "515180": "SH000922", "510880": "SH000015"}
BAND, MIN_STEP, REBAL_DAYS = 0.10, 0.02, 5


# ───────────────────────── 账本读写 ─────────────────────────
def _load() -> dict:
    if not os.path.exists(ACC):
        raise SystemExit(f"还没有纸面账户：先 python3 paper.py init（账本目录 {PDIR}）")
    return json.load(open(ACC, encoding="utf-8"))


def _save(acc: dict) -> None:
    tmp = ACC + ".tmp"
    json.dump(acc, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    os.replace(tmp, ACC)


def _rows(path: str) -> list[dict]:
    return list(csv.DictReader(open(path, encoding="utf-8"))) if os.path.exists(path) else []


def _append(path: str, cols: list[str], rows: list[dict]) -> None:
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        if new:
            w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in cols})


def fee(amount: float) -> float:
    return round(max(COMMISSION_MIN, amount * COMMISSION), 2)


def session_today() -> dt.date:
    from lib.calendar import last_completed_session
    s = last_completed_session()
    if s is None:
        raise SystemExit("交易日历超出覆盖，无法确定最近交易日")
    return s


# ───────────────────────── 命令 ─────────────────────────
def init(cash: float, code: str, tv: float) -> None:
    if os.path.exists(ACC):
        raise SystemExit(f"账户已存在：{ACC}（要重建先手动移走）")
    os.makedirs(PDIR, exist_ok=True)
    s = session_today().isoformat()
    acc = {"name": "红利低波均线 v3 · 纸面账户", "init_cash": cash, "cash": cash, "code": code,
           "fund": NAMES.get(code, code), "index": INDEX.get(code), "tv": tv, "spread_min": I.SPREAD_MIN,
           "shares": 0, "cost": 0.0, "entry_session": None, "created": dt.date.today().isoformat(),
           "start_session": s, "last_session": None, "last_trade_session": None, "factor": None,
           "pending": None, "bh_base": None, "next_trade_id": 1}
    _save(acc)
    print(f"已建纸面账户：{code} {acc['fund']}，初始 {cash:,.0f}，目标波动 {tv:.0%}；起始交易日 {s}（这一天收盘出第一个信号）")


def _bars(code: str):
    raw = P.klines(code, 600, "none")
    fwd = P.klines(code, 600, "forward")
    rm = {b["date"]: b for b in raw}
    fwd = [b for b in fwd if b["date"] in rm]
    return rm, fwd


def run(dry: bool, force: bool) -> None:
    acc = _load()
    session = session_today().isoformat()
    if acc["last_session"] and session <= acc["last_session"] and not force:
        print(f"{session} 已经跑过（last_session={acc['last_session']}），不重复。--force 强制重跑")
        status()
        return
    code = acc["code"]
    rm, fwd = _bars(code)
    fwd = [b for b in fwd if b["date"] <= session]
    if not fwd or fwd[-1]["date"] != session:
        print(f"{session} 没有 {code} 的 K 线（停牌或数据未到），本次不交易")
        return
    today, raw_today = fwd[-1], rm[session]
    out, notes = [], []

    # ① 份额拆分 / 分红：前复权 / 不复权 比值变动 → 份额折算
    factor = today["close"] / raw_today["close"]
    if acc["factor"] is not None and acc["shares"] and acc["last_session"]:
        prev_raw = rm.get(acc["last_session"])
        prev_fwd = next((b for b in fwd if b["date"] == acc["last_session"]), None)
        if prev_raw and prev_fwd:
            k = (prev_fwd["close"] / prev_raw["close"]) / factor     # 今天的因子恒为 1 附近；上次因子相对今天的变化
            if abs(k - 1) > 1e-4:
                ratio = 1 / k
                new_sh = acc["shares"] * ratio
                out.append(_trade_row(acc, session, "ADJ", new_sh - acc["shares"], raw_today["open"], 0.0, 0.0,
                                      f"份额折算 ×{ratio:.4f}（拆分 / 分红再投入）"))
                acc["cost"] = acc["cost"] / ratio
                acc["shares"] = new_sh

    # ② 执行上一个交易日挂的单（按今天开盘价）
    px_open = raw_today["open"]
    if acc["pending"] and acc["pending"]["signal_session"] < session:
        tgt = acc["pending"]["target"]
        equity = acc["cash"] + acc["shares"] * px_open
        want = 0 if tgt <= 0 else math.floor(tgt * equity / px_open / 100) * 100
        diff = want - int(acc["shares"])
        if diff < 0:
            amt = -diff * px_open
            f = fee(amt)
            acc["cash"] = round(acc["cash"] + amt - f, 2)
            out.append(_trade_row(acc, session, "SELL", -diff, px_open, amt, f, acc["pending"]["reason"]))
            acc["shares"] = acc["shares"] + diff
            if acc["shares"] < 1:
                acc["shares"], acc["cost"], acc["entry_session"] = 0, 0.0, None
            acc["last_trade_session"] = session
        elif diff > 0:
            while diff > 0 and diff * px_open + fee(diff * px_open) > acc["cash"]:
                diff -= 100
            if diff > 0:
                amt = diff * px_open
                f = fee(amt)
                acc["cost"] = (acc["cost"] * acc["shares"] + amt + f) / (acc["shares"] + diff)
                acc["cash"] = round(acc["cash"] - amt - f, 2)
                acc["shares"] = acc["shares"] + diff
                acc["entry_session"] = acc["entry_session"] or session
                out.append(_trade_row(acc, session, "BUY", diff, px_open, amt, f, acc["pending"]["reason"]))
                acc["last_trade_session"] = session
        acc["pending"] = None

    # ③ 用今天收盘算 v3 信号，决定是否给下一个交易日挂单
    spread = I.market_spread(max_age_h=20)
    gate = I.spread_gate(spread, acc["spread_min"])
    pe = I.load_pe(acc["index"], max_age_h=24 * 7) if acc.get("index") else None
    _, _, st = I.run(fwd, tv=acc["tv"], pe=pe, gate=gate)
    px = raw_today["close"]
    mv = acc["shares"] * px
    total = acc["cash"] + mv
    w = mv / total if total else 0.0
    tgt = st["target_weight"]
    sessions_since = _sessions_between(fwd, acc["last_trade_session"], session)
    need = ((tgt == 0) != (w < 1e-6)) or abs(tgt - w) > BAND or (sessions_since >= REBAL_DAYS and abs(tgt - w) > MIN_STEP)
    if tgt > 0 and w < 1e-6 and tgt * total < 100 * px:
        need = False
    if need:
        why = ("建仓" if w < 1e-6 else "清仓" if tgt == 0 else "调仓") + \
              f" {w:.0%}→{tgt:.0%}（乖离 {st['dev']:.3f}，60 日波动 {st['vol'] or 0:.1%}，信号 {'持有' if st['signal'] else '空仓'}）"
        acc["pending"] = {"signal_session": session, "target": round(tgt, 4), "reason": why}
        notes.append(f"挂单（下一个交易日开盘执行）：{why}")
    sp = max((d for d in spread if d <= session), default=None)
    if acc["bh_base"] is None:
        acc["bh_base"] = today["close"]
    nav = {"session": session, "cash": round(acc["cash"], 2), "market_value": round(mv, 2), "total": round(total, 2),
           "nav": round(total / acc["init_cash"], 6), "return_pct": round((total / acc["init_cash"] - 1) * 100, 3),
           "weight_pct": round(w * 100, 2), "signal": "持有" if st["signal"] else "空仓",
           "target_pct": round(tgt * 100, 2), "dev": round(st["dev"], 4), "vol": round(st["vol"] or 0, 4),
           "spread": round(spread[sp], 3) if sp else "", "gate": ("开" if sp and spread[sp] >= acc["spread_min"] else "关"),
           "bh_nav": round(today["close"] / acc["bh_base"], 6)}

    print(f"== {session} {code} {acc['fund']}｜收盘 {px:.3f}｜乖离 {st['dev']:.3f}｜60 日波动 {(st['vol'] or 0):.1%}"
          f"｜股债差 {nav['spread']}（闸门{nav['gate']}）｜信号 {nav['signal']}｜目标 {tgt:.0%}｜当前仓位 {w:.1%}")
    for r in out:
        print(f"  {r['side']} {float(r['shares']):,.0f} 份 @ {float(r['price']):.3f}  金额 {float(r['amount']):,.2f}  费用 {r['fee']}  —— {r['reason']}")
    for n in notes:
        print("  " + n)
    print(f"  总资产 {total:,.2f}（现金 {acc['cash']:,.2f} + 市值 {mv:,.2f}），净值 {nav['nav']:.4f}，512890 买持 {nav['bh_nav']:.4f}")
    if dry:
        print("（--dry-run：不落账）")
        return
    acc["factor"] = factor
    acc["last_session"] = session
    _append(TRD, TRD_COLS, out)
    if not any(r["session"] == session for r in _rows(NAV)):
        _append(NAV, NAV_COLS, [nav])
    _save(acc)


def _trade_row(acc: dict, session: str, side: str, shares: float, price: float, amount: float, f: float, reason: str) -> dict:
    tid = acc["next_trade_id"]
    acc["next_trade_id"] = tid + 1
    cash_after = acc["cash"] if side != "BUY" else acc["cash"]
    return {"id": tid, "session": session, "code": acc["code"], "name": acc["fund"], "side": side,
            "shares": round(shares, 2), "price": round(price, 4), "amount": round(amount, 2), "fee": f,
            "cash_after": round(cash_after, 2), "reason": reason}


def _sessions_between(bars: list[dict], a: str | None, b: str) -> int:
    if not a:
        return 10 ** 6
    return sum(1 for x in bars if a < x["date"] <= b)


def status() -> None:
    acc = _load()
    navs = _rows(NAV)
    last = navs[-1] if navs else None
    print(f"{acc['name']}｜{acc['code']} {acc['fund']}｜初始 {acc['init_cash']:,.0f}｜目标波动 {acc['tv']:.0%}｜"
          f"起始 {acc['start_session']}｜最近 {acc['last_session']}")
    print(f"  持有 {acc['shares']:,.0f} 份，成本 {acc['cost']:.4f}，建仓日 {acc['entry_session']}；现金 {acc['cash']:,.2f}")
    if last:
        print(f"  {last['session']} 总资产 {float(last['total']):,.2f}，净值 {last['nav']}（{last['return_pct']}%），"
              f"仓位 {last['weight_pct']}%，信号 {last['signal']}，目标 {last['target_pct']}%，闸门 {last['gate']}，512890 买持 {last['bh_nav']}")
    if acc["pending"]:
        print(f"  待执行（下一个交易日开盘）：目标 {acc['pending']['target']:.0%} —— {acc['pending']['reason']}")


def _notion() -> dict:
    return json.load(open(NOTION, encoding="utf-8")) if os.path.exists(NOTION) else \
        {"db": {}, "synced_trades": [], "synced_nav": [], "position_page": None}


def notion_pending() -> None:
    acc, n = _load(), _notion()
    trades = [r for r in _rows(TRD) if int(r["id"]) not in set(n["synced_trades"])]
    navs = [r for r in _rows(NAV) if r["session"] not in set(n["synced_nav"])]
    last = _rows(NAV)[-1] if _rows(NAV) else {}
    pos = {"code": acc["code"], "name": acc["fund"], "shares": acc["shares"], "cost": round(acc["cost"], 4),
           "price": round(float(last["market_value"]) / acc["shares"], 4) if acc["shares"] and last else None,
           "market_value": float(last.get("market_value") or 0), "weight_pct": float(last.get("weight_pct") or 0),
           "target_pct": float(last.get("target_pct") or 0), "signal": last.get("signal"),
           "pnl_pct": round((float(last["market_value"]) / acc["shares"] / acc["cost"] - 1) * 100, 2)
           if acc["shares"] and acc["cost"] and last else None,
           "entry_session": acc["entry_session"], "status": "持有" if acc["shares"] else "空仓",
           "notion_page": n.get("position_page")}
    print(json.dumps({"db": n["db"], "trades": trades, "nav": navs, "position": pos,
                      "pending_order": acc["pending"]}, ensure_ascii=False, indent=1))


def notion_mark(trades: str, nav: str, pos: str, db: str) -> None:
    n = _notion()
    if trades:
        n["synced_trades"] = sorted(set(n["synced_trades"]) | {int(x) for x in trades.split(",") if x})
    if nav:
        n["synced_nav"] = sorted(set(n["synced_nav"]) | {x for x in nav.split(",") if x})
    if pos:
        n["position_page"] = pos
    if db:
        for kv in db.split(","):
            k, v = kv.split("=", 1)
            n["db"][k] = v
    json.dump(n, open(NOTION, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("已记录")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    pi = sub.add_parser("init")
    pi.add_argument("--cash", type=float, default=10_000_000)
    pi.add_argument("--code", default="512890")
    pi.add_argument("--tv", type=float, default=I.TV3)
    pr = sub.add_parser("run")
    pr.add_argument("--dry-run", action="store_true")
    pr.add_argument("--force", action="store_true")
    sub.add_parser("status")
    sub.add_parser("notion-pending")
    pm = sub.add_parser("notion-mark")
    pm.add_argument("--trades", default="")
    pm.add_argument("--nav", default="")
    pm.add_argument("--pos", default="")
    pm.add_argument("--db", default="")
    a = ap.parse_args()
    if a.cmd == "init":
        init(a.cash, a.code, a.tv)
    elif a.cmd == "run":
        run(a.dry_run, a.force)
    elif a.cmd == "status":
        status()
    elif a.cmd == "notion-pending":
        notion_pending()
    else:
        notion_mark(a.trades, a.nav, a.pos, a.db)


if __name__ == "__main__":
    main()
