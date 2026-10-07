#!/usr/bin/env python3
"""从本地游资语料库（~/Baidu-Pan/游资心法+讲座全集1/distill/）汇总出 skill 用的小数据（进 git）：
  data/people.json       71 人：流派、58位标签、资料等级、可蒸馏性分数与结论、成交画像关键数、笔记路径
  data/rules_index.jsonl 840 条规则（各人 rules.yaml 合并）
  data/benchmarks.json   有成交数据（A/B 级、≥30 段）的人物同口径基准，供 profile.py 横比
  docs/people/<名>/      逐人 蒸馏笔记.md、rules.yaml、trades/{profile,entry_types}.md（只拷派生的笔记与统计）
  docs/schools/          流派对照表 6 份 + 仅合集人物笔记
语料库本身（原文 / OCR 文本 / 交割单）不进 git —— 版权属原作者 / 倒卖站，只留在本机。
用法: python3 build_data.py
"""
import json
import os
import re
import shutil
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
CORPUS = Path(os.environ.get("YOUZI_CORPUS", Path.home() / "Baidu-Pan/游资心法+讲座全集1/distill"))
PEOPLE_YAML = HERE / "pipeline" / "people.yaml"   # 人物登记表（唯一事实源）
SCHOOLS = ["北京炒家/首板模式_对照.md", "赵老哥/龙头接力_对照.md", "涅盘重升/情绪周期_对照.md",
           "乔帮主/低吸与弱市_对照.md", "龙飞虎/仓位风控_对照.md", "水哥割股/实盘画像横比.md"]
# 公开仓库里不出现私有账户 / 引擎名
SCRUB = [("A 股池（emily / chris）", "A 股趋势池"), ("A 股趋势池（emily / chris）", "A 股趋势池"),
         ("emily / chris 池", "A 股趋势池"), ("emily / chris", "A 股趋势池"),
         ("ST_ATL_GUARD", "ST 趋势引擎"), ("`vol-gate` skill", "波动率入场阀检验"), ("vol-gate skill", "波动率入场阀检验"),
         ("`vol-gate`", "波动率入场阀检验"), ("vol-gate", "波动率入场阀检验"),
         ("selector 里", "趋势选股评测里"), ("deploy", "生产仓位")]


def _copy(src: Path, dst: Path):
    dst.parent.mkdir(parents=True, exist_ok=True)
    t = src.read_text()
    for a, b in SCRUB:
        t = t.replace(a, b)
    dst.write_text(t)


def score_and_verdict(note: Path):
    if not note.exists():
        return None, ""
    t = note.read_text()
    sec = t[t.find("## 7"):] if "## 7" in t else t
    m = re.findall(r"(?:总分|合计)\D{0,12}?(\d{1,2})\s*/\s*20", sec) or re.findall(r"(\d{1,2})\s*/\s*20", sec)
    sc = int(m[-1]) if m else None
    v = re.search(r"结论[*：:\s]*(.{4,80}?)(?:[。|\n])", sec)
    return sc, (v.group(1).strip("*（ ") if v else "")


def main():
    cfg = yaml.safe_load(PEOPLE_YAML.read_text())
    toc = yaml.safe_load((CORPUS / "data" / "58_toc.yaml").read_text())
    people = []
    for scope, lst in (("独立资料", cfg["people"]), ("仅合集", cfg.get("compilation_people", []))):
        for p in lst:
            d = CORPUS / "人物" / p["name"]
            prof = json.loads((d / "trades" / "profile.json").read_text()) if (d / "trades" / "profile.json").exists() else None
            ent = json.loads((d / "trades" / "entry_types.json").read_text()) if (d / "trades" / "entry_types.json").exists() else None
            sc, verdict = score_and_verdict(d / "蒸馏笔记.md")
            rec = {"name": p["name"], "scope": scope, "aliases": p.get("aliases", []), "school": p.get("school", []),
                   "tagline": p.get("tagline", ""), "book_label": toc.get(p["name"], {}).get("label", ""),
                   "score20": sc, "verdict": verdict,
                   "note": f"docs/people/{p['name']}/蒸馏笔记.md" if (d / "蒸馏笔记.md").exists() else None}
            if prof:
                rec["trades"] = {"period": prof["period"], "episodes": prof["n_episodes"], "win": round(prof["win_rate"], 3),
                                 "pf": round(prof["profit_factor"] or 0, 2), "hold1": round(prof["hold_dist"].get("1 天", 0), 3),
                                 "src": "OCR" if prof.get("ocr_share", 0) > 0.5 else "xls",
                                 "sell_match": round(prof.get("sell_match_ratio", 1), 2)}
            if ent:
                et = {k: round(v["占比"], 3) for k, v in ent["etype"].items()}
                rec["entry_mix"] = et
            people.append(rec)
    (HERE / "data").mkdir(exist_ok=True)
    (HERE / "data" / "people.json").write_text(json.dumps(people, ensure_ascii=False, indent=1))
    # 规则
    rows = []
    files = sorted((CORPUS / "人物").glob("*/rules.yaml")) + [CORPUS / "合集/58位大佬心法/仅合集人物_rules.yaml"]
    for f in files:
        if not f.exists():
            continue
        d = yaml.safe_load(f.read_text())
        person = d.get("person") if isinstance(d, dict) else None
        rs = d.get("rules", []) if isinstance(d, dict) else d
        for r in rs or []:
            if isinstance(r, dict):
                rows.append({"person": r.get("person") or person or f.parent.name,
                             **{k: r.get(k) for k in ["id", "category", "rule", "quote", "source", "evidence", "codable", "signal", "data_check"]},
                             "file": str(f.relative_to(CORPUS))})
    with (HERE / "data" / "rules_index.jsonl").open("w") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    # 基准
    bench = []
    for r in people:
        t = r.get("trades")
        if t and t["episodes"] >= 30:
            mix = r.get("entry_mix", {})
            main = max((k for k in mix if k != "无行情"), key=lambda k: mix[k], default="")
            bench.append({"person": r["name"], "period": f"{t['period'][0][:7]}~{t['period'][1][:7]}", "n": t["episodes"],
                          "win": t["win"], "pf": t["pf"], "hold1": t["hold1"], "main": f"{main} {mix.get(main, 0) * 100:.0f}%", "src": t["src"]})
    bench.sort(key=lambda b: (b["src"] != "xls", -b["n"]))
    (HERE / "data" / "benchmarks.json").write_text(json.dumps(bench, ensure_ascii=False, indent=1))
    n_docs = sync_docs()
    print(f"people={len(people)} rules={len(rows)} benchmarks={len(bench)} docs={n_docs}")


def sync_docs() -> int:
    """派生笔记拷进 docs/（整目录重建）。原文 text/、原件 src/、成交明细 csv 不拷。"""
    n = 0
    dst = HERE / "docs" / "people"
    shutil.rmtree(dst, ignore_errors=True)
    for d in sorted((CORPUS / "人物").iterdir()):
        for rel in ["蒸馏笔记.md", "rules.yaml", "trades/profile.md", "trades/entry_types.md"]:
            if (d / rel).exists():
                _copy(d / rel, dst / d.name / rel)
                n += 1
    sch = HERE / "docs" / "schools"
    shutil.rmtree(sch, ignore_errors=True)
    sch.mkdir(parents=True)
    for rel in SCHOOLS:
        _copy(CORPUS / "人物" / rel, sch / Path(rel).name)
        n += 1
    for name in ["仅合集人物_蒸馏笔记.md", "仅合集人物_rules.yaml"]:
        _copy(CORPUS / "合集" / "58位大佬心法" / name, sch / name)
        n += 1
    return n


if __name__ == "__main__":
    main()
