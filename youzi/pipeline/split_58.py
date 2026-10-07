#!/usr/bin/env python3
"""把《58位大佬心法》上/中/下三册按章切开 → 人物/<name>/text/58位·<册>·第NN章.md；写 data/58_toc.yaml（书里给的标签）。

上/中册：目录页「第X章 姓名：标题」+ 正文以单独一行「第X章」开章（按顺序对上目录）
下册：目录同上，正文以「姓名----标题」开章
书里的姓名 → people.yaml 名字用 ALIAS 归一；不在登记表里的人物也建目录（只有合集章节，tier C）
用法: python3 split_58.py
"""
import re
import os
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
DISTILL = Path(os.environ.get("YOUZI_CORPUS", Path.home() / "Baidu-Pan/游资心法+讲座全集1/distill"))  # 语料库（本机，不进 git）
TXT = DISTILL / "合集" / "58位大佬心法" / "text"
CFG = yaml.safe_load((HERE / "people.yaml").read_text())
ALIAS = {"养家": "炒股养家", "令狐冲": "令胡冲", "不动明王": "明王", "九二科比": "92科比", "舵主徐翔": "徐翔",
         "章帮主": "章盟主", "小睿睿8": "小睿睿", "糊涂118": "糊涂118", "挑战12345": "挑战12345",
         "创世纪888888": "创世纪888888", "创世纪888888首板新秀": "创世纪888888", "小草骑墻": "小草骑墙", "涅槃重生": "涅盘重升", "独孤一箭": "独股一箭"}
CN = "零一二三四五六七八九十"


def cn2int(s):
    if s.startswith("十"):
        s = "一" + s
    if "十" in s:
        a, b = s.split("十")
        return CN.index(a) * 10 + (CN.index(b) if b else 0)
    return CN.index(s)


def canon(name):
    name = re.sub(r"\s+", "", name)
    name = ALIAS.get(name, name)
    for p in CFG["people"]:
        if name == p["name"] or name in p["aliases"]:
            return p["name"]
    return name


def toc(lines):
    out = []
    for i, l in enumerate(lines[:600]):
        m = re.match(r"^第([一二三四五六七八九十]+)章\s*(.+?)[:：](.+?)[•…⋯:：.\s\d]*$", l.strip())
        if not m:  # 「第二十四章 创世纪888888 首板新秀 ： 205」这种目录行缺冒号
            m = re.match(r"^第([一二三四五六七八九十]+)章\s*(\S+)\s+(\S+?)[•…⋯:：.\s\d]*$", l.strip())
        if m:
            out.append((cn2int(m.group(1)), m.group(2).strip(), m.group(3).strip(" •…⋯：:")))
    if not out:  # 下册：第X章 与 姓名：标题 分两行
        for i, l in enumerate(lines[:200]):
            m = re.match(r"^第([一二三四五六七八九十]+)章\s*$", l.strip())
            if m:
                for l2 in lines[i + 1:i + 4]:
                    m2 = re.match(r"^(.+?)\s*[:：](.+?)\.{3,}\s*\d+\s*$", l2.strip())
                    if m2:
                        out.append((cn2int(m.group(1)), m2.group(1).strip(), m2.group(2).strip()))
                        break
    return out


def main():
    book = {}
    for f in sorted(TXT.glob("58*.md")):
        vol = "上册" if "上册" in f.name else "中册" if "中册" in f.name else "下册"
        lines = f.read_text().splitlines()
        t = toc(lines)
        starts = []
        if vol != "下册":
            body = [i for i, l in enumerate(lines) if re.match(r"^第[一二三四五六七八九十]+章\s*$", l.strip())]
            starts = body[-len(t):] if len(body) >= len(t) else body
        else:
            for n, name, title in t:
                key = re.sub(r"\s+", "", name)
                tkey = re.sub(r"\s+", "", title)[:4]
                hit = next((i for i, l in enumerate(lines) if i > 100 and (
                    re.sub(r"\s+", "", l).startswith(key + "--")
                    or re.fullmatch(re.escape(key) + r"[:：]" + re.escape(tkey) + r".{0,12}", re.sub(r"\s+", "", l)))), None)
                starts.append(hit)
        for k, (n, name, title) in enumerate(t):
            s = starts[k] if k < len(starts) else None
            if s is None:
                continue
            e = next((x for x in starts[k + 1:] if x is not None), len(lines))
            person = canon(name)
            out = DISTILL / "人物" / person / "text"
            out.mkdir(parents=True, exist_ok=True)
            body = "\n".join(lines[s:e])
            (out / f"58位·{vol}·第{n:02d}章.md").write_text(
                f"<!-- source: {f.name} 第{n}章 {name}：{title} | lines {s}-{e} -->\n\n{body}\n")
            book[person] = {"book_name": name, "label": title, "vol": vol, "chapter": n, "lines": e - s}
    (DISTILL / "data").mkdir(exist_ok=True)
    (DISTILL / "data" / "58_toc.yaml").write_text(yaml.safe_dump(book, allow_unicode=True, sort_keys=False))
    known = {p["name"] for p in CFG["people"]}
    print(f"chapters={len(book)} known={sum(1 for p in book if p in known)} new={[p for p in book if p not in known]}")


if __name__ == "__main__":
    main()
