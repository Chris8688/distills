#!/usr/bin/env python3
"""查游资规则库（data/rules_index.jsonl，71 人 840 条，逐条带原文短引 + 出处 + 证据等级 + 是否可编码 + 实盘对照）。

用法:
  python3 rules.py --person 北京炒家
  python3 rules.py --school 首板打板 --codable
  python3 rules.py --category 止损 --evidence 原文
  python3 rules.py --grep 冰点
  python3 rules.py --conflict            # 只看有实盘对照且标了「冲突」的
  python3 rules.py --stats
"""
import argparse
import json
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
RULES = HERE / "data" / "rules_index.jsonl"
PEOPLE = HERE / "data" / "people.json"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--person")
    ap.add_argument("--school")
    ap.add_argument("--category")
    ap.add_argument("--evidence")
    ap.add_argument("--codable", action="store_true")
    ap.add_argument("--conflict", action="store_true")
    ap.add_argument("--grep")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--limit", type=int, default=200)
    a = ap.parse_args()
    rs = [json.loads(l) for l in RULES.read_text().splitlines() if l.strip()]
    ppl = {p["name"]: p for p in json.loads(PEOPLE.read_text())} if PEOPLE.exists() else {}
    if a.stats:
        print(f"{len(rs)} 条 / {len({r['person'] for r in rs})} 人")
        for k in ("evidence", "category"):
            print(k, Counter(str(r.get(k)) for r in rs).most_common())
        print("codable", sum(1 for r in rs if r.get("codable") in (True, "true")))
        return
    out = []
    for r in rs:
        if a.person and r["person"] != a.person:
            continue
        if a.school and a.school not in ppl.get(r["person"], {}).get("school", []):
            continue
        if a.category and r.get("category") != a.category:
            continue
        if a.evidence and not str(r.get("evidence", "")).startswith(a.evidence):
            continue
        if a.codable and r.get("codable") not in (True, "true"):
            continue
        if a.conflict and "冲突" not in str(r.get("data_check") or ""):
            continue
        if a.grep and a.grep not in json.dumps(r, ensure_ascii=False):
            continue
        out.append(r)
    for r in out[:a.limit]:
        print(f"[{r.get('id')}] {r['person']} · {r.get('category')} · {r.get('evidence')}{' · 可编码' if r.get('codable') in (True, 'true') else ''}")
        print(f"  规则：{r.get('rule')}")
        if r.get("quote"):
            print(f"  原文：「{r['quote']}」  ← {r.get('source')}")
        if r.get("signal"):
            print(f"  信号：{r['signal']}")
        if r.get("data_check"):
            print(f"  实盘：{r['data_check']}")
    print(f"\n共 {len(out)} 条" + (f"（只显示前 {a.limit}）" if len(out) > a.limit else ""))


if __name__ == "__main__":
    main()
