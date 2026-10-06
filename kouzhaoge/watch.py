#!/usr/bin/env python3
"""口罩哥 · K-Research 可证伪检查点（只读、只打印，不下单）。

数据在 docs/watchlist.yaml：checkpoints（带到期日）+ monitors（常驻阈值）。
规则出处见 docs/01-蒸馏笔记.md §3 / §4 与 docs/notes/K1~K3。

用法：
  python3 watch.py                  # 已到期未核 + 未来 45 天到期 + 命中率
  python3 watch.py --days 120 --theme 存储
  python3 watch.py --all            # 全部检查点
  python3 watch.py --monitors       # 常驻阈值
  python3 watch.py --json
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PATH = os.path.join(HERE, "docs", "watchlist.yaml")
STATUSES = ("pending", "hit", "miss", "void")


def load(path: str = DEFAULT_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    cps = data.get("checkpoints") or []
    for cp in cps:
        due = cp.get("due")
        cp["due"] = due if isinstance(due, dt.date) else dt.date.fromisoformat(str(due))
        if cp.get("status", "pending") not in STATUSES:
            raise ValueError(f"{cp.get('id')}: status 只能是 {STATUSES}，现在是 {cp.get('status')!r}")
    ids = [cp["id"] for cp in cps] + [m["id"] for m in data.get("monitors") or []]
    dup = {i for i in ids if ids.count(i) > 1}
    if dup:
        raise ValueError(f"id 重复：{sorted(dup)}")
    return {"checkpoints": cps, "monitors": data.get("monitors") or []}


def classify(cps: list[dict], today: dt.date, days: int) -> dict[str, list[dict]]:
    """overdue = 已到期仍 pending；upcoming = days 天内到期的 pending；done = 已回填。"""
    out: dict[str, list[dict]] = {"overdue": [], "upcoming": [], "later": [], "done": []}
    for cp in sorted(cps, key=lambda c: c["due"]):
        st = cp.get("status", "pending")
        if st != "pending":
            out["done"].append(cp)
        elif cp["due"] < today:
            out["overdue"].append(cp)
        elif (cp["due"] - today).days <= days:
            out["upcoming"].append(cp)
        else:
            out["later"].append(cp)
    return out


def scorecard(cps: list[dict]) -> dict:
    """命中率只算 hit / miss；void 与 pending 不进分母。"""
    hit = sum(1 for c in cps if c.get("status") == "hit")
    miss = sum(1 for c in cps if c.get("status") == "miss")
    n = hit + miss
    return {"hit": hit, "miss": miss, "void": sum(1 for c in cps if c.get("status") == "void"),
            "pending": sum(1 for c in cps if c.get("status", "pending") == "pending"),
            "hit_rate": round(hit / n, 3) if n else None}


def _fmt(cp: dict, today: dt.date) -> str:
    d = (cp["due"] - today).days
    when = f"逾期 {-d} 天" if d < 0 else ("今天" if d == 0 else f"{d} 天后")
    lines = [f"  [{cp['due']}｜{when}] {cp['id']}（{cp.get('theme', '')}｜{cp.get('src', '')}）",
             f"      判断：{cp.get('claim', '')}",
             f"      核对：{cp.get('check', '')}"]
    if cp.get("status", "pending") != "pending":
        lines.append(f"      结果：{cp['status']} {cp.get('outcome', '')}".rstrip())
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file", default=DEFAULT_PATH)
    ap.add_argument("--today", help="YYYY-MM-DD，缺省今天")
    ap.add_argument("--days", type=int, default=45, help="未来多少天内到期算 upcoming")
    ap.add_argument("--theme", help="只看某主题：存储 / 算力 / 电力 / 宏观")
    ap.add_argument("--all", action="store_true", help="列出全部检查点")
    ap.add_argument("--monitors", action="store_true", help="列出常驻阈值")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    today = dt.date.fromisoformat(a.today) if a.today else dt.date.today()
    data = load(a.file)
    cps = [c for c in data["checkpoints"] if not a.theme or c.get("theme") == a.theme]
    mons = [m for m in data["monitors"] if not a.theme or m.get("theme") == a.theme]
    groups = classify(cps, today, a.days)
    score = scorecard(cps)

    if a.json:
        conv = lambda xs: [{**c, "due": c["due"].isoformat()} for c in xs]
        print(json.dumps({"today": today.isoformat(), "score": score,
                          **{k: conv(v) for k, v in groups.items()},
                          "monitors": mons if a.monitors else []}, ensure_ascii=False, indent=2))
        return 0

    print(f"口罩哥 K-Research 检查点 · {today}" + (f" · 主题={a.theme}" if a.theme else ""))
    sections = (("全部", sorted(cps, key=lambda c: c["due"])),) if a.all else (
        ("已到期未核（去回填 status/outcome）", groups["overdue"]),
        (f"{a.days} 天内到期", groups["upcoming"]),
    )
    for title, xs in sections:
        print(f"\n■ {title}：{len(xs)}")
        for cp in xs:
            print(_fmt(cp, today))
    if a.monitors:
        print(f"\n■ 常驻阈值：{len(mons)}")
        for m in mons:
            print(f"  {m['id']}（{m.get('theme', '')}｜{m.get('src', '')}｜{m.get('cadence', '')}）\n      {m.get('rule', '')}")
    rate = "—" if score["hit_rate"] is None else f"{score['hit_rate']:.0%}"
    print(f"\n命中率：hit {score['hit']} / miss {score['miss']}（{rate}）· void {score['void']} · 待核 {score['pending']}")
    print("提醒：三情景概率是模板化的（BASE≈55%），只核他写死的阈值与日期；结论只作待核实线索。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
