#!/usr/bin/env python3
"""同步雪球组合 ZH1027925（作者）的持仓 / 调仓 / 净值到本地 data/xueqiu/。

雪球的调仓接口要登录、组合页面有阿里云 WAF 的 JS 验证，命令行拿不到 —— 所以分两路：
  · 净值、组合行情：匿名 token 即可，本脚本 `nav` 直接拉。
  · 持仓、调仓：在用户已登录的 Chrome 里执行 `js` 打印的脚本，把它返回的行原样喂给 `ingest`。
    （流程见 SKILL.md「同步雪球」。不要尝试绕过 WAF / 登录。）

本地文件（进 git，是作者实盘的留档）：
  data/xueqiu/rebalancing.csv        每笔成交一行，(rb_id, symbol) 去重；早期存档的行 rb_id 为空
  data/xueqiu/holdings/<日期>.csv    每次同步一份持仓快照（与上一份相同则不新建）
  data/xueqiu/nav.csv                日净值 + 沪深300
  data/xueqiu/sync_log.md            每次同步的变化摘要

用法：
  python3 sync_xueqiu.py js [--pages N]      打印浏览器里要执行的 JS（N 页 × 50 次调仓，缺省 1）
  python3 sync_xueqiu.py ingest < rows.txt   合并浏览器返回的行
  python3 sync_xueqiu.py nav                 拉净值
  python3 sync_xueqiu.py status              最近调仓与持仓
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import os
import sys

CUBE = "ZH1027925"
HERE = os.path.dirname(os.path.realpath(__file__))
D = os.path.join(HERE, "data", "xueqiu")
REB = os.path.join(D, "rebalancing.csv")
HOLD = os.path.join(D, "holdings")
NAV = os.path.join(D, "nav.csv")
LOG = os.path.join(D, "sync_log.md")
REB_COLS = ["rb_id", "date", "time", "symbol", "name", "prev_weight", "target_weight", "price"]
HOLD_COLS = ["symbol", "name", "weight", "segment"]
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")

# 在「已登录、且已通过 WAF」的 xueqiu.com 页面里执行。
# 先在组合页 https://xueqiu.com/P/ZH1027925 取持仓（SNB.cubeInfo），再翻调仓接口。
# 返回字符串数组（浏览器工具会截断 >100 项的数组 → 超了就分批 window.__xq.slice(...)）。
JS = r"""
(async () => {
  const pages = __PAGES__;
  const out = [];
  const ci = window.SNB && SNB.cubeInfo;
  if (!ci) return ['ERR|not on cube page (open https://xueqiu.com/P/ZH1027925 first)'];
  for (const h of ((ci.view_rebalancing || {}).holdings || []))
    out.push(['H', h.stock_symbol, h.stock_name, h.weight, h.segment_name || ''].join('|'));
  for (let p = 1; p <= pages; p++) {
    const t = await fetch(`/cubes/rebalancing/history.json?cube_symbol=__CUBE__&count=50&page=${p}`,
                          {credentials: 'include'}).then(r => r.text());
    let j; try { j = JSON.parse(t) } catch (e) { out.push('ERR|waf ' + t.slice(0, 40)); break }
    if (j.error_code) { out.push('ERR|' + j.error_code + ' ' + (j.error_description || '') + ' (需要登录)'); break }
    for (const x of (j.list || [])) {
      if (x.status !== 'success' || x.category !== 'user_rebalancing') continue;
      for (const h of (x.rebalancing_histories || []))
        out.push(['R', x.id, x.created_at, h.stock_symbol, h.stock_name,
                  h.prev_weight_adjusted ?? 0, h.target_weight, h.price].join('|'));
    }
    if (p >= (j.maxPage || 1)) break;
    await new Promise(r => setTimeout(r, 400));
  }
  window.__xq = out;
  return out.length <= 100 ? out : ['MORE|' + out.length].concat(out.slice(0, 99));
})()
"""


def _read(path, cols):
    if not os.path.exists(path):
        return []
    return list(csv.DictReader(open(path, encoding="utf-8")))


def _write(path, cols, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in cols})


def _f(x) -> str:
    """权重 / 价格统一成最短表示，保证同一笔成交去重键稳定。"""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return str(x)
    return f"{v:g}"


def _key(r: dict) -> tuple:
    # 早期存档没有 rb_id → 用 (日期, 代码, 目标权重, 价格) 认同一笔
    return (r["date"], r["symbol"], _f(r["target_weight"]), _f(r["price"]))


def _log(lines: list[str]) -> None:
    os.makedirs(D, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"\n## {dt.datetime.now():%Y-%m-%d %H:%M}\n" + "\n".join(lines) + "\n")


# ══════════════════════════════════════════════════════════════════════════════
def ingest(lines: list[str]) -> dict:
    errs = [ln for ln in lines if ln.startswith("ERR|")]
    if errs:
        raise SystemExit("浏览器返回错误：" + "；".join(errs) + "\n→ 让用户在该标签页登录雪球后重跑。")
    reb = _read(REB, REB_COLS)
    have = {_key(r) for r in reb}
    new, hold = [], []
    for ln in lines:
        p = ln.rstrip("\n").split("|")
        if p[0] == "R" and len(p) >= 8:
            t = dt.datetime.fromtimestamp(int(p[2]) / 1000, dt.UTC) + dt.timedelta(hours=8)   # 北京时间
            r = {"rb_id": p[1], "date": t.strftime("%Y-%m-%d"), "time": t.strftime("%H:%M:%S"),
                 "symbol": p[3], "name": p[4], "prev_weight": _f(p[5]), "target_weight": _f(p[6]),
                 "price": _f(p[7])}
            if _key(r) not in have:
                have.add(_key(r))
                new.append(r)
        elif p[0] == "H" and len(p) >= 4:
            hold.append({"symbol": p[1], "name": p[2], "weight": _f(p[3]), "segment": p[4] if len(p) > 4 else ""})

    if new:
        reb = sorted(reb + new, key=lambda r: (r["date"], r.get("time") or "", r["symbol"]))
        _write(REB, REB_COLS, reb)

    changed = False
    if hold:
        hold.sort(key=lambda r: -float(r["weight"]))
        prev = latest_holdings()
        if prev is None or {r["symbol"] for r in prev["rows"]} != {r["symbol"] for r in hold}:
            changed = True
        # 权重每天漂移，只在成分变化或距上次快照 ≥7 天时落新文件，免得一天一份
        stale = prev is None or (dt.date.today() - dt.date.fromisoformat(prev["date"])).days >= 7
        if changed or stale:
            _write(os.path.join(HOLD, f"{dt.date.today()}.csv"), HOLD_COLS, hold)

    msg = [f"- 新调仓 {len(new)} 笔：" + ("；".join(f"{r['date']} {r['name']} {r['prev_weight']}→{r['target_weight']}% @{r['price']}" for r in new) or "无")]
    if hold:
        prev_syms = {r["symbol"] for r in (prev["rows"] if prev else [])}
        cur = {r["symbol"]: r["name"] for r in hold}
        add = [cur[s] for s in cur if s not in prev_syms]
        rm = [r["name"] for r in (prev["rows"] if prev else []) if r["symbol"] not in cur]
        msg.append(f"- 持仓 {len(hold)} 只" + (f"；新进 {'、'.join(add)}" if add else "")
                   + (f"；退出 {'、'.join(rm)}" if rm else "") + ("" if changed else "（成分未变）"))
    _log(msg)
    print("\n".join(msg))
    return {"new": new, "holdings": hold, "changed": changed}


def latest_holdings() -> dict | None:
    if not os.path.isdir(HOLD):
        return None
    fs = sorted(x for x in os.listdir(HOLD) if x.endswith(".csv"))
    if not fs:
        return None
    return {"date": fs[-1][:-4], "rows": _read(os.path.join(HOLD, fs[-1]), HOLD_COLS)}


def nav() -> None:
    """匿名 token 拉净值（/hq 页面会种下 xq_a_token）。"""
    import requests
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Referer": f"https://xueqiu.com/P/{CUBE}"})
    s.get("https://xueqiu.com/hq", timeout=20)
    j = s.get("https://xueqiu.com/cubes/nav_daily/all.json", params={"cube_symbol": CUBE}, timeout=30).json()
    idx = {x["date"]: x["value"] for x in j[1]["list"]} if len(j) > 1 else {}
    rows = [{"date": x["date"], "cube_nav": x["value"], "hs300": idx.get(x["date"], "")} for x in j[0]["list"]]
    _write(NAV, ["date", "cube_nav", "hs300"], rows)
    q = s.get("https://xueqiu.com/cubes/quote.json", params={"code": CUBE}, timeout=20).json().get(CUBE, {})
    print(f"净值 {len(rows)} 天，最新 {rows[-1]['date']} = {rows[-1]['cube_nav']}"
          f"（总收益 {q.get('total_gain')}%，月 {q.get('monthly_gain')}%）")


def status(n: int = 10) -> None:
    reb = _read(REB, REB_COLS)
    print(f"调仓存档 {len(reb)} 笔，最近 {n} 笔：")
    for r in reb[-n:]:
        print(f"  {r['date']} {r['name']} {r['prev_weight']}→{r['target_weight']}% @{r['price']}")
    h = latest_holdings()
    if h:
        print(f"持仓快照 {h['date']}：{len(h['rows'])} 只 · " + "、".join(f"{r['name']}{r['weight']}" for r in h["rows"]))


def migrate() -> None:
    """一次性：把蒸馏时的存档挪进 data/xueqiu/。"""
    old_reb = os.path.join(HERE, "data", f"{CUBE}_rebalancing.csv")
    if os.path.exists(old_reb) and not os.path.exists(REB):
        rows = [{"rb_id": "", "time": "", **r} for r in _read(old_reb, None)]
        for r in rows:
            r["prev_weight"], r["target_weight"], r["price"] = (_f(r["prev_weight"]), _f(r["target_weight"]), _f(r["price"]))
        _write(REB, REB_COLS, rows)
        os.remove(old_reb)
        print(f"迁移调仓存档 {len(rows)} 笔 → {os.path.relpath(REB, HERE)}")
    for f in os.listdir(os.path.join(HERE, "data")):
        if f.startswith(f"{CUBE}_holdings_") and f.endswith(".csv"):
            day = f[len(f"{CUBE}_holdings_"):-4]
            os.makedirs(HOLD, exist_ok=True)
            os.replace(os.path.join(HERE, "data", f), os.path.join(HOLD, f"{day}.csv"))
            print(f"迁移持仓快照 {day}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    pj = sub.add_parser("js")
    pj.add_argument("--pages", type=int, default=1)
    sub.add_parser("ingest")
    sub.add_parser("nav")
    sub.add_parser("status")
    sub.add_parser("migrate")
    a = ap.parse_args()
    if a.cmd == "js":
        print(JS.replace("__PAGES__", str(a.pages)).replace("__CUBE__", CUBE).strip())
    elif a.cmd == "ingest":
        ingest([ln for ln in sys.stdin.read().splitlines() if ln.strip()])
    elif a.cmd == "nav":
        nav()
    elif a.cmd == "status":
        status()
    elif a.cmd == "migrate":
        migrate()


if __name__ == "__main__":
    main()
