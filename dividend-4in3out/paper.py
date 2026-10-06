#!/usr/bin/env python3
"""股息率模式·纸面账户（初始 1000 万人民币）—— 跑我们自己的筛选器（screen.py），不下真单。

账本是本地文件（放在仓库外的 STATE 目录，是唯一事实源）；Notion 只是镜像，由 Claude 用 Notion MCP 同步（见 SKILL.md）。
  paper/account.json   参数 + 现金 + 持仓 + 已处理的除权事件
  paper/trades.csv     流水：BUY / SELL / DIV（现金分红，税后）/ BONUS（送转股）
  paper/nav.csv        每个交易日一行：现金、市值、总资产、净值；并排作者组合净值（同起点归一）
  paper/notion.json    Notion 同步进度（数据库 id、已同步的流水 / 净值、持仓行 page id）

成交口径（写死，改之前先改 docs/02）：
  · 按「最近一个已收盘交易日」的收盘价成交（lib/calendar.last_completed_session）；
    同一个交易日只交易一次，重跑只打印不重复下单（--force 可强制）。
  · 当日没有 K 线（停牌）不交易；收盘涨停不买、跌停不卖（主板 ±10%，创业板/科创板 ±20%）。
  · 一手 100 股向下取整（科创板最少 200 股）；不做再平衡，漂移不管（作者也不管）。
  · 费用：佣金 0.025%（最低 5 元）、过户费 0.001%（双边）、印花税 0.05%（卖出）。
  · 现金分红在除息日入账，红利税按除息日的持有天数近似：≤30 天 20%、≤1 年 10%、>1 年 0
    （实际是卖出时补扣，这里提前扣，近似）。送转股在除权日按比例加股、摊薄成本。
  · 分红再投入：税后分红买回派息的那只（一手取整）；该股本轮被卖 / 停牌 / 涨停 / 不足一手 → 留现金等补位。

用法：
  python3 paper.py init [--cash 10000000 --rules observed --slots 15]
  python3 paper.py run [--dry-run] [--force]
  python3 paper.py status
  python3 paper.py notion-pending          # 给 Claude 看：还没同步到 Notion 的流水 / 净值 / 持仓
  python3 paper.py notion-mark --trades 1,2 --nav 2026-09-30 [--pos 600066=<page_id>,...] [--db k=v,...]
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

import screen as S  # noqa: E402
from lib import STATE  # noqa: E402

P = os.path.join(STATE, "paper")
ACC = os.path.join(P, "account.json")
TRD = os.path.join(P, "trades.csv")
NAV = os.path.join(P, "nav.csv")
NOTION = os.path.join(P, "notion.json")
TRD_COLS = ["id", "session", "run_at", "code", "name", "side", "shares", "price", "amount",
            "fee", "tax", "cash_after", "reason"]
NAV_COLS = ["session", "cash", "market_value", "equity", "nav", "return_pct", "n_pos", "cube_nav"]

COMMISSION, COMMISSION_MIN = 0.00025, 5.0
TRANSFER = 0.00001
STAMP = 0.0005


# ══════════════════════════════════════════════════════════════════════════════
# 小工具
# ══════════════════════════════════════════════════════════════════════════════
def _load() -> dict:
    if not os.path.exists(ACC):
        raise SystemExit("还没有纸面账户：先 python3 paper.py init")
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


def fees(side: str, amount: float) -> tuple[float, float]:
    """(佣金+过户费, 印花税)"""
    fee = max(COMMISSION_MIN, amount * COMMISSION) + amount * TRANSFER
    return round(fee, 2), round(amount * STAMP if side == "SELL" else 0.0, 2)


def lot_floor(code: str, shares: float) -> int:
    n = int(shares // 100) * 100
    return n if not code.startswith("688") or n >= 200 else 0


def limit_pct(code: str) -> float:
    return 0.20 if code.startswith(("300", "301", "688")) else 0.10


def div_tax_rate(days: int) -> float:
    return 0.20 if days <= 30 else (0.10 if days <= 365 else 0.0)


def session_today() -> dt.date:
    from lib.calendar import last_completed_session
    s = last_completed_session()
    if s is None:
        raise SystemExit("交易日历超出覆盖，无法确定最近交易日")
    return s


def bars(codes: list[str]) -> dict:
    """{code: [最近 3 根不复权日线]}（TickFlow）。"""
    from lib import prices as PX
    if not codes:
        return {}
    m = {PX.tf(c, "CN"): c for c in codes}
    raw = PX._klines(list(m), 3, "none")
    return {m[k]: v for k, v in raw.items() if k in m}


def cube_nav_on(day: str) -> float | None:
    p = os.path.join(HERE, "data", "xueqiu", "nav.csv")
    best = None
    for r in _rows(p):
        if r["date"] <= day:
            best = float(r["cube_nav"])
    return best


# ══════════════════════════════════════════════════════════════════════════════
def init(cash: float, rules: str, slots: int) -> None:
    if os.path.exists(ACC):
        raise SystemExit(f"已存在 {ACC}，不覆盖。要重开请先手动备份/删除 paper/。")
    os.makedirs(P, exist_ok=True)
    acc = {"name": "股息率模式·纸面账户", "init_cash": cash, "cash": cash, "rules": rules, "slots": slots,
           "created": dt.date.today().isoformat(), "start_session": None, "last_session": None,
           "positions": {}, "applied_actions": [], "next_trade_id": 1}
    _save(acc)
    print(f"纸面账户已建：{cash:,.0f} 元 · 规则 {rules} · {slots} 只等权")


def _trade(acc: dict, out: list, session: str, code: str, name: str, side: str, shares: int,
           price: float, reason: str, fee: float = 0.0, tax: float = 0.0, amount: float | None = None) -> None:
    amount = round(shares * price, 2) if amount is None else amount
    acc["cash"] = round(acc["cash"] + (amount if side in ("SELL", "DIV") else -amount if side == "BUY" else 0)
                        - fee - tax, 2)
    out.append({"id": acc["next_trade_id"], "session": session, "run_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
                "code": code, "name": name, "side": side, "shares": shares, "price": price, "amount": amount,
                "fee": fee, "tax": tax, "cash_after": acc["cash"], "reason": reason})
    acc["next_trade_id"] += 1


def corporate_actions(acc: dict, out: list, session: dt.date, offline: bool) -> None:
    """除权除息：上次交易日（不含）~ 本交易日（含）之间的事件，股权登记日前已持有才享有。"""
    last = acc["last_session"] or session.isoformat()
    acts = S.corporate_actions(session, offline)
    for code, pos in list(acc["positions"].items()):
        for a in acts.get(code, []):
            key = f"{code}|{a['ex']}|{a['period']}"
            if key in acc["applied_actions"] or not (last < a["ex"] <= session.isoformat()):
                continue
            if a["record"] and pos["open_session"] > a["record"]:
                continue
            acc["applied_actions"].append(key)
            if a["cash_ps"] > 0:
                gross = round(pos["shares"] * a["cash_ps"], 2)
                held = (dt.date.fromisoformat(a["ex"]) - dt.date.fromisoformat(pos["open_session"])).days
                tax = round(gross * div_tax_rate(held), 2)
                _trade(acc, out, a["ex"], code, pos["name"], "DIV", pos["shares"], a["cash_ps"],
                       f"现金分红 每股{a['cash_ps']:g}（{a['period'][:4]}{'年报' if a['period'].endswith('1231') else '中期'}，持有{held}天税率{div_tax_rate(held):.0%}）",
                       tax=tax, amount=gross)
            if a["bonus_ps"] > 0:
                add = int(pos["shares"] * a["bonus_ps"])
                pos["cost"] = round(pos["cost"] * pos["shares"] / (pos["shares"] + add), 4)
                pos["shares"] += add
                _trade(acc, out, a["ex"], code, pos["name"], "BONUS", add, 0.0,
                       f"送转 每股{a['bonus_ps']:g}", amount=0.0)


def reinvest_dividends(acc: dict, out: list, session: str, px, notes: list) -> None:
    """分红再投入（2026-10-02 定）：本轮入账的现金分红（税后）买回派息的那只。
    雪球组合会把分红自动再投入该股，这里照做；派息股本轮已卖出 / 停牌 / 涨停 / 不足一手 → 留现金等补位。"""
    for t in [t for t in out if t["side"] == "DIV"]:
        code = t["code"]
        pos = acc["positions"].get(code)
        if not pos:
            continue
        p, prev = px(code)
        if p is None:
            notes.append(f"分红再投入 {pos['name']}：{prev}，留现金")
            continue
        if prev and p >= prev * (1 + limit_pct(code)) - 0.005:
            notes.append(f"分红再投入 {pos['name']}：涨停，留现金")
            continue
        net = float(t["amount"]) - float(t["tax"])
        sh = lot_floor(code, min(net, acc["cash"]) / (p * (1 + COMMISSION + TRANSFER)))
        while sh > 0 and sh * p + sum(fees("BUY", sh * p)) > acc["cash"]:
            sh -= 100
        if sh <= 0:
            notes.append(f"分红再投入 {pos['name']}：{net:,.0f} 元不足一手，留现金")
            continue
        amt = round(sh * p, 2)
        fee, _ = fees("BUY", amt)
        _trade(acc, out, session, code, pos["name"], "BUY", sh, p, f"分红再投入（税后分红 {net:,.2f}）", fee)
        pos["cost"] = round((pos["cost"] * pos["shares"] + amt + fee) / (pos["shares"] + sh), 4)
        pos["shares"] += sh
        pos["last_price"] = p


def run(dry: bool, force: bool, offline: bool) -> None:
    acc = _load()
    session = session_today()
    ss = session.isoformat()
    if acc["last_session"] == ss and not force:
        print(f"{ss} 已经跑过（同一交易日只交易一次）。看状态用 status；确需重跑加 --force。")
        status()
        return

    out: list[dict] = []
    corporate_actions(acc, out, session, offline)

    held = list(acc["positions"])
    rows, _, _ = S.scan(acc["rules"], held, offline=offline)
    slots = acc["slots"]

    # 本交易日收盘价（只取需要的：持仓 + 买入候选前 3×slots）
    buys_all = sorted((r for r in rows.values() if not r.get("missing") and r["action"] == "BUY"
                       and r["code"] not in acc["positions"]), key=lambda r: (-r["sus_yield"], -r["yield"]))
    need = held + [r["code"] for r in buys_all[:3 * slots]]
    bx = bars(need)

    def px(code):
        b = bx.get(code) or []
        if not b or b[-1]["date"] != ss:
            return None, "停牌/无当日K线"
        prev = b[-2]["close"] if len(b) > 1 else None
        return b[-1]["close"], prev

    # ── 卖出 ───────────────────────────────────────────────────────────────────
    notes = []
    for code in held:
        r = rows.get(code) or {}
        pos = acc["positions"][code]
        p, prev = px(code)
        if p is not None:
            pos["last_price"] = p
        if not r or r.get("missing") or not r["sell_why"]:
            if r and r.get("review"):
                notes.append(f"复核 {code} {pos['name']}：{'；'.join(r['review'])}")
            continue
        if p is None:
            notes.append(f"应卖 {code} {pos['name']} 但{prev}，顺延")
            continue
        if prev and p <= prev * (1 - limit_pct(code)) + 0.005:
            notes.append(f"应卖 {code} {pos['name']} 但跌停，顺延")
            continue
        amt = round(pos["shares"] * p, 2)
        fee, tax = fees("SELL", amt)
        _trade(acc, out, ss, code, pos["name"], "SELL", pos["shares"], p, "；".join(r["sell_why"]), fee, tax)
        del acc["positions"][code]

    # ── 买入：不出则不进 + 行业上限 ─────────────────────────────────────────────
    mv = sum(p["shares"] * p["last_price"] for p in acc["positions"].values())
    equity = acc["cash"] + mv
    target = equity / slots
    n_free = slots - len(acc["positions"])
    held_inds = [p["industry"] for p in acc["positions"].values()]
    cands = []
    for r in buys_all:
        p, prev = px(r["code"])
        if p is None:
            continue
        if prev and p >= prev * (1 + limit_pct(r["code"])) - 0.005:
            notes.append(f"跳过 {r['code']} {r['name']}：涨停买不到")
            continue
        cands.append({**r, "_px": p})
    for r in S.pick(cands, slots, held_inds, n_free):
        p = r["_px"]
        budget = min(target, acc["cash"])
        sh = lot_floor(r["code"], budget / (p * (1 + COMMISSION + TRANSFER)))
        while sh > 0 and sh * p + sum(fees("BUY", sh * p)) > acc["cash"]:
            sh -= 100
        if sh <= 0:
            notes.append(f"现金不足，{r['name']} 没买")
            continue
        amt = round(sh * p, 2)
        fee, _ = fees("BUY", amt)
        _trade(acc, out, ss, r["code"], r["name"], "BUY", sh, p,
               f"股息率{r['yield']:.2%}·可持续{r['sus_yield']:.2%}·连续分红{r['years']}年·扣非{S._pct(r['kf_yoy'])}·保底分红率{(r['floor_payout'] or 0):.0%}", fee)
        acc["positions"][r["code"]] = {"name": r["name"], "industry": r["industry"], "shares": sh,
                                       "cost": round((amt + fee) / sh, 4), "open_session": ss, "last_price": p}

    # ── 分红再投入（补位之后做：补位按总资产/只数定额，再投入只用分红那部分现金）──────
    reinvest_dividends(acc, out, ss, px, notes)

    # ── 估值 ───────────────────────────────────────────────────────────────────
    mv = round(sum(p["shares"] * p["last_price"] for p in acc["positions"].values()), 2)
    equity = round(acc["cash"] + mv, 2)
    acc["start_session"] = acc["start_session"] or ss
    c0, c1 = cube_nav_on(acc["start_session"]), cube_nav_on(ss)
    navrow = {"session": ss, "cash": acc["cash"], "market_value": mv, "equity": equity,
              "nav": round(equity / acc["init_cash"], 6), "return_pct": round((equity / acc["init_cash"] - 1) * 100, 3),
              "n_pos": len(acc["positions"]), "cube_nav": round(c1 / c0, 6) if c0 and c1 else ""}
    acc["last_session"] = ss

    print(f"# 纸面账户 {ss}（规则 {acc['rules']}，{slots} 只等权）{'【预演，不落账】' if dry else ''}")
    for t in out:
        print(f"- {t['side']:<5} {t['code']} {t['name']} {t['shares']} 股 @ {t['price']} = {float(t['amount']):,.2f}"
              f"（费 {t['fee']} 税 {t['tax']}）· {t['reason']}")
    if not out:
        print("- 无成交")
    for n in notes:
        print(f"- {n}")
    print(f"总资产 {equity:,.2f} = 现金 {acc['cash']:,.2f} + 市值 {mv:,.2f} · 净值 {navrow['nav']:.4f}"
          f" · 作者组合同期 {navrow['cube_nav'] or '—'} · 持仓 {len(acc['positions'])} 只")
    if dry:
        return
    _append(TRD, TRD_COLS, out)
    navs = [r for r in _rows(NAV) if r["session"] != ss] + [navrow]
    with open(NAV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=NAV_COLS)
        w.writeheader()
        w.writerows({k: r.get(k, "") for k in NAV_COLS} for r in navs)
    _save(acc)


def status() -> None:
    acc = _load()
    mv = sum(p["shares"] * p["last_price"] for p in acc["positions"].values())
    eq = acc["cash"] + mv
    print(f"# {acc['name']} · 规则 {acc['rules']} · 起始 {acc['start_session'] or '未开始'} · 最近 {acc['last_session'] or '—'}")
    print(f"总资产 {eq:,.2f}（{(eq / acc['init_cash'] - 1):+.2%}）= 现金 {acc['cash']:,.2f} + 市值 {mv:,.2f}")
    try:
        cube = {r["symbol"][-6:] for r in S.latest_cube_snapshot()["rows"]}
    except Exception:      # noqa: BLE001
        cube = set()
    print("| 代码 | 名称 | 行业 | 股数 | 成本 | 现价 | 市值 | 权重 | 盈亏 | 作者也持有 |\n|---|---|---|---|---|---|---|---|---|---|")
    for c, p in sorted(acc["positions"].items(), key=lambda kv: -kv[1]["shares"] * kv[1]["last_price"]):
        v = p["shares"] * p["last_price"]
        print(f"| {c} | {p['name']} | {p['industry']} | {p['shares']} | {p['cost']:.3f} | {p['last_price']} | "
              f"{v:,.0f} | {v / eq:.1%} | {p['last_price'] / p['cost'] - 1:+.1%} | {'✓' if c in cube else ''} |")
    if cube:
        print(f"与作者持仓重合 {len(cube & set(acc['positions']))}/{len(acc['positions'])}")


# ══════════════════════════════════════════════════════════════════════════════
# Notion 镜像（Claude 用 MCP 写，这里只记进度）
# ══════════════════════════════════════════════════════════════════════════════
def _notion() -> dict:
    return json.load(open(NOTION, encoding="utf-8")) if os.path.exists(NOTION) else \
        {"db": {}, "synced_trades": [], "synced_nav": [], "positions": {}}


def notion_pending() -> None:
    n = _notion()
    acc = _load()
    done_t, done_n = set(map(str, n["synced_trades"])), set(n["synced_nav"])
    mv = sum(p["shares"] * p["last_price"] for p in acc["positions"].values())
    eq = acc["cash"] + mv
    try:
        cube = {r["symbol"][-6:] for r in S.latest_cube_snapshot()["rows"]}
    except Exception:      # noqa: BLE001
        cube = set()
    pos = [{"code": c, **p, "market_value": round(p["shares"] * p["last_price"], 2), "author_holds": c in cube,
            "weight_pct": round(p["shares"] * p["last_price"] / eq * 100, 2),
            "pnl_pct": round((p["last_price"] / p["cost"] - 1) * 100, 2),
            "notion_page": n["positions"].get(c)} for c, p in acc["positions"].items()]
    gone = [{"code": c, "notion_page": pid} for c, pid in n["positions"].items() if c not in acc["positions"]]
    json.dump({"db": n["db"],
               "trades": [t for t in _rows(TRD) if t["id"] not in done_t],
               "nav": [r for r in _rows(NAV) if r["session"] not in done_n],
               "positions": pos, "closed_positions": gone,
               "summary": {"session": acc["last_session"], "equity": round(eq, 2), "cash": acc["cash"],
                           "rules": acc["rules"], "slots": acc["slots"]}},
              sys.stdout, ensure_ascii=False, indent=1)


def notion_mark(trades: str, nav: str, pos: str, db: str, drop: str) -> None:
    n = _notion()
    n["synced_trades"] += [int(x) for x in trades.split(",") if x.strip()]
    n["synced_nav"] += [x.strip() for x in nav.split(",") if x.strip()]
    for kv in filter(None, pos.split(",")):
        k, v = kv.split("=", 1)
        n["positions"][k.strip()] = v.strip()
    for k in filter(None, drop.split(",")):
        n["positions"].pop(k.strip(), None)
    for kv in filter(None, db.split(",")):
        k, v = kv.split("=", 1)
        n["db"][k.strip()] = v.strip()
    os.makedirs(P, exist_ok=True)
    json.dump(n, open(NOTION, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"已记：流水 {len(n['synced_trades'])} 笔、净值 {len(n['synced_nav'])} 天、持仓行 {len(n['positions'])}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    pi = sub.add_parser("init")
    pi.add_argument("--cash", type=float, default=10_000_000)
    pi.add_argument("--rules", choices=("1.0", "observed", "strict"), default="observed")
    pi.add_argument("--slots", type=int, default=15)
    pr = sub.add_parser("run")
    pr.add_argument("--dry-run", action="store_true")
    pr.add_argument("--force", action="store_true")
    pr.add_argument("--offline", action="store_true")
    sub.add_parser("status")
    sub.add_parser("notion-pending")
    pm = sub.add_parser("notion-mark")
    for k in ("trades", "nav", "pos", "db", "drop"):
        pm.add_argument(f"--{k}", default="")
    a = ap.parse_args()
    if a.cmd == "init":
        init(a.cash, a.rules, a.slots)
    elif a.cmd == "run":
        run(a.dry_run, a.force, a.offline)
    elif a.cmd == "status":
        status()
    elif a.cmd == "notion-pending":
        notion_pending()
    elif a.cmd == "notion-mark":
        notion_mark(a.trades, a.nav, a.pos, a.db, a.drop)


if __name__ == "__main__":
    main()
