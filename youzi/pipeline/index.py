#!/usr/bin/env python3
"""生成 人物索引.md（人物 × 流派 × 文件 总表）与每个 人物/<name>/README.md。可重复跑。
用法: python3 index.py
"""
import json
from collections import defaultdict
import os
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
DISTILL = Path(os.environ.get("YOUZI_CORPUS", Path.home() / "Baidu-Pan/游资心法+讲座全集1/distill"))  # 语料库（本机，不进 git）
CFG = yaml.safe_load((HERE / "people.yaml").read_text())
MAN = json.loads((DISTILL / "manifest.json").read_text())
TOC = yaml.safe_load((DISTILL / "data" / "58_toc.yaml").read_text()) if (DISTILL / "data" / "58_toc.yaml").exists() else {}


def link(text, rel, base=DISTILL):
    return f"[{text}](<{rel}>)"


def status(owner):
    f = DISTILL / owner / "text" / "_status.json"
    return json.loads(f.read_text()) if f.exists() else {}


def score(owner):
    import re
    f = DISTILL / owner / "蒸馏笔记.md"
    if not f.exists():
        return ""
    t = f.read_text()
    sec = t[t.find("## 7"):] if "## 7" in t else t
    m = re.findall(r"(?:总分|合计)\D{0,12}?(\d{1,2})\s*/\s*20", sec) or re.findall(r"(\d{1,2})\s*/\s*20", sec)
    return f"[{m[-1] + '/20' if m else '笔记'}](<{owner}/蒸馏笔记.md>)"


def jload(p):
    return json.loads(p.read_text()) if p.exists() else None


def wan(x):
    return f"{x / 1e4:,.0f}万"


def person_row(p, scope="人物"):
    name = p["name"]
    owner = f"{scope}/{name}"
    items = MAN["owners"].get(owner, [])
    st = status(owner)
    tdir = DISTILL / owner / "text"
    chap = sorted(tdir.glob("58位*.md")) if tdir.exists() else []
    kinds = defaultdict(int)
    for it in items:
        kinds[it["kind"]] += it.get("n", 1) if it["type"] == "dir" else 1
    han = sum(v["han"] for v in st.values())
    ocr = sum(v["ocr_pages"] for v in st.values())
    prof = jload(DISTILL / owner / "trades" / "profile.json")
    ent = jload(DISTILL / owner / "trades" / "entry_types.json")
    raw = kinds.get("原始成交", 0)
    tier = "A" if raw else ("B" if prof and prof.get("ocr_share", 0) > 0.5 and prof["n_episodes"] >= 30 else "C")
    if prof and tier == "C" and raw == 0:
        tier = "B-"  # 有 OCR 成交但段数太少
    return {"name": name, "owner": owner, "school": p.get("school", []), "tagline": p.get("tagline", ""),
            "book": TOC.get(name, {}).get("label", ""), "items": items, "kinds": kinds, "han": han, "ocr": ocr,
            "chap": chap, "prof": prof, "ent": ent, "tier": tier, "st": st}


def top_etype(ent):
    if not ent:
        return ""
    et = {k: v["占比"] for k, v in ent["etype"].items() if k != "无行情"}
    if not et:
        return ""
    k = max(et, key=et.get)
    s = f"{k} {et[k] * 100:.0f}%"
    if k == "打板" and ent.get("board_pos"):
        bp = {kk: vv["占比"] for kk, vv in ent["board_pos"].items()}
        if bp:
            kk = max(bp, key=bp.get)
            s += f"（{kk} {bp[kk] / max(1e-9, et[k]) * 100:.0f}%）"
    return s


def trade_cell(r):
    pf = r["prof"]
    if not pf:
        return ""
    src = "xls" if pf.get("ocr_share", 0) < 0.5 else "OCR"
    return (f"{pf['period'][0][:7]}~{pf['period'][1][:7]} {src} · {pf['n_episodes']}段 · 胜{pf['win_rate'] * 100:.0f}% · "
            f"PF {(pf['profit_factor'] if pf['profit_factor'] is not None else float('nan')):.2f} · 持{pf['hold_median_td']:.0f}天")


def write_person_readme(r, p):
    o = DISTILL / r["owner"]
    o.mkdir(parents=True, exist_ok=True)
    L = [f"# {r['name']}", "",
         f"- 别名：{', '.join(p.get('aliases', [])) or '—'}",
         f"- 流派：{' / '.join(r['school'])}；一句话：{r['tagline']}",
         f"- 《58位大佬心法》章节标签：{r['book'] or '—'}",
         f"- 资料等级 **{r['tier']}**（A=原始成交 xls · B=OCR 交割单可统计 · C=只有心法文字）", ""]
    if r["prof"]:
        pf, ent = r["prof"], r["ent"]
        L += ["## 成交画像", "", f"- {trade_cell(r)}；主买点 {top_etype(ent)}",
              f"- 详见 [trades/profile.md](trades/profile.md) · [trades/entry_types.md](trades/entry_types.md)", ""]
    L += ["## 文件", "", "| 类别 | 原件（软链） | 页数 | 文本 | 抽取方式 | 汉字 |", "|---|---|---|---|---|---|"]
    for it in sorted(r["items"], key=lambda x: (x["kind"], x["link"])):
        ln = Path(it["link"]).relative_to(r["owner"])
        name = ln.name + (f"/（{it['n']} 个）" if it["type"] == "dir" else "")
        stem = ln.name if it["type"] == "dir" else Path(ln.name).stem
        st = r["st"].get(stem + ".md")
        txt = link("md", f"text/{stem}.md") if st else ("—" if it["kind"] in ("音视频", "原始成交") else "（跳过：有同名 Word 版或近似重复）")
        L.append(f"| {it['kind']} | {link(name, ln)} | {st['pages'] if st else ''} | {txt} | {st['method'] if st else ''} | {st['han'] if st else ''} |")
    for c in r["chap"]:
        L.append(f"| 合集章节 | 58位大佬心法 | | {link(c.name, 'text/' + c.name)} | 切章 | |")
    (o / "README.md").write_text("\n".join(L) + "\n")


def main():
    rows = [person_row(p) for p in CFG["people"]]
    crow = [person_row(p) for p in CFG.get("compilation_people", [])]
    for r, p in zip(rows, CFG["people"]):
        write_person_readme(r, p)
    for r, p in zip(crow, CFG.get("compilation_people", [])):
        write_person_readme(r, p)
    tot_han = sum(r["han"] for r in rows) + sum(v["han"] for c in MAN["owners"] if c.startswith("合集/") for v in status(c).values())
    tot_ocr = sum(r["ocr"] for r in rows) + sum(v["ocr_pages"] for c in MAN["owners"] if c.startswith("合集/") for v in status(c).values())
    st = MAN["stats"]
    L = ["# 人物索引 · 游资心法 + 讲座全集", "",
         f"> 自动生成（`python3 index.py`）。原始资料 {st['files']} 个文件，内容去重后 {st['unique']} 个（重复 {st['duplicates']}）；"
         f"可搜索文本约 **{tot_han / 1e4:,.0f} 万汉字**（OCR {tot_ocr:,} 页，Apple Vision 本地识别）。",
         f"> 人物 **{len(rows)}** 位有独立资料 + **{len(crow)}** 位只出现在《58位大佬心法》合集。原件只做软链，不移动不复制。", "",
         "## 怎么用", "",
         "- 找某人的全部资料：点人物名进 `人物/<名>/README.md`；全文检索：`grep -rn 关键词 人物/*/text/`",
         "- 资料等级：**A** 有原始成交 xls/csv（机械统计最可信）· **B** 交割单 OCR 后可统计（有漏行，方向参考）· **C** 只有心法文字",
         "- 成交画像口径：持仓段 episode = 空仓→建仓→空仓；买点类型按首笔买价相对昨收与涨停价判定（`pipeline/enrich.py`）",
         "- **蒸馏成果**：逐人 `人物/<名>/蒸馏笔记.md` + `rules.yaml`（「蒸馏」列 = 可蒸馏性 /20）；流派对照表 6 份（见下）；"
         "已做成 skill：`youzi`（心法库 + 规则库 + 交割单画像，`~/distills/youzi/`）、`youzi-sentiment`（情绪周期面板 + 证伪）",
         "- 流派对照表：`人物/北京炒家/首板模式_对照.md` · `人物/赵老哥/龙头接力_对照.md` · `人物/涅盘重升/情绪周期_对照.md` · "
         "`人物/乔帮主/低吸与弱市_对照.md` · `人物/龙飞虎/仓位风控_对照.md` · `人物/水哥割股/实盘画像横比.md`",
         "- 流水线：`build.py`（归属+软链）→ `extract.py`（文本/OCR）→ `split_58.py`（合集切章）→ `trades.py` / `ocr_trades.py` / `enrich.py`（成交）→ `index.py`（本文件）", "",
         "## 总表：人物 × 流派 × 资料", "",
         "| # | 人物 | 流派 | 58位标签 | 等级 | 蒸馏 | 心法 | 交割单 | 原始成交 | 音视频 | 文本(万字) | 成交画像 | 主买点（实测） |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    order = {"A": 0, "B": 1, "B-": 2, "C": 3}
    rows.sort(key=lambda r: (order[r["tier"]], -r["han"]))
    for i, r in enumerate(rows, 1):
        k = r["kinds"]
        L.append(f"| {i} | {link(r['name'], r['owner'] + '/README.md')} | {' / '.join(r['school'])} | {r['book']} | {r['tier']} | {score(r['owner'])} | "
                 f"{k.get('心法', 0) + len(r['chap'])} | {k.get('交割单', 0)} | {k.get('原始成交', 0)} | {k.get('音视频', 0)} | "
                 f"{r['han'] / 1e4:.1f} | {trade_cell(r)} | {top_etype(r['ent'])} |")
    L += ["", "## 只出现在《58位大佬心法》里的人物", "", "| 人物 | 流派（按章节标题推断） | 章节 | 文本 |", "|---|---|---|---|"]
    for r in crow:
        L.append(f"| {link(r['name'], r['owner'] + '/README.md')} | {' / '.join(r['school'])} | {r['tagline']} | "
                 + " ".join(link(c.name, f"{r['owner']}/text/{c.name}") for c in r["chap"]) + " |")
    L += ["", "## 按流派反查", ""]
    by = defaultdict(list)
    for r in rows + crow:
        for s in r["school"]:
            by[s].append(r["name"])
    for s in sorted(by, key=lambda x: -len(by[x])):
        L.append(f"- **{s}**（{len(by[s])}）：{'、'.join(by[s])}")
    L += ["", "## 合集 / 课程（不归个人）", "", "| 合集 | 条目 | 文本 | 说明 |", "|---|---|---|---|"]
    notes = {"58位大佬心法": "已按章切到各人物目录（58 章）", "视频课程": "音视频未转写；含 2019 市场情绪监控表 xlsx",
             "超短百科全书": "纯图版已 OCR", "游资实战交割单带K线图": "多人交割单 + K 线，纯图版已 OCR",
             "新生300天图解教程": "广通均线体系教材，非游资", "日本蜡烛图技术": "Nison 经典教材", "游资心法音频": "3 段 mp3 未转写"}
    for c in [x for x in MAN["owners"] if x.startswith("合集/")]:
        stc = status(c)
        L.append(f"| {c.split('/', 1)[1]} | {len(MAN['owners'][c])} | {sum(v['han'] for v in stc.values()) / 1e4:.1f} 万字 | {notes.get(c.split('/', 1)[1], '')} |")
    L += ["", "## 文件明细（人物 × 文件）", "",
          "| 人物 | 类别 | 文件 | 页数 | 抽取 | 文本 |", "|---|---|---|---|---|---|"]
    for r in rows:
        for it in sorted(r["items"], key=lambda x: (x["kind"], x["link"])):
            ln = Path(it["link"])
            stem = ln.name if it["type"] == "dir" else Path(ln.name).stem
            s = r["st"].get(stem + ".md")
            nm = ln.name + (f"/（{it['n']}）" if it["type"] == "dir" else "")
            L.append(f"| {r['name']} | {it['kind']} | {link(nm, it['link'])} | {s['pages'] if s else ''} | "
                     f"{s['method'] if s else ''} | {link('md', r['owner'] + '/text/' + stem + '.md') if s else ''} |")
    L += ["", "## 已知缺口", "",
          "- 网盘仍在下载：`游资训练营讲座视频全集` 下有 `.downloading` 文件，下完后重跑 `build.py → extract.py → index.py` 即可增量补齐",
          "- 音视频（炒股养家 60 集心法音频、92科比 直播录音、职业炒手视频、训练营课程）尚未转写",
          "- OCR 成交（等级 B）有缺页漏行：看各 profile.md 的「数据完整度」",
          "- 泽熙「缠论操盘手」视频为营销挂名，不归入徐翔"]
    (DISTILL / "人物索引.md").write_text("\n".join(L) + "\n")
    print(f"人物索引.md: {len(rows)} 人 + {len(crow)} 合集人物；A={sum(r['tier'] == 'A' for r in rows)} "
          f"B={sum(r['tier'] == 'B' for r in rows)} C={sum(r['tier'] in ('C', 'B-') for r in rows)}")


if __name__ == "__main__":
    main()
