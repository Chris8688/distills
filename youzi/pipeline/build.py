#!/usr/bin/env python3
"""扫描原始资料 → 按 people.yaml 归属到人物/合集 → 内容去重 → 软链到 人物/<name>/src/ → 写 manifest.json。

可重复跑（网盘还在下载时新文件会被补进来）。不移动、不复制原文件。
用法: python3 build.py
"""
import hashlib
import json
import os
import re
from collections import defaultdict
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
DISTILL = Path(os.environ.get("YOUZI_CORPUS", Path.home() / "Baidu-Pan/游资心法+讲座全集1/distill"))  # 语料库（本机，不进 git）
ROOT = DISTILL.parent
CFG = yaml.safe_load((HERE / "people.yaml").read_text())

RAW_EXT = {".csv", ".xls", ".xlsx"}
AV_EXT = {".mp3", ".mp4", ".avi", ".m4a", ".wav"}
IMG_EXT = {".png", ".jpg", ".jpeg"}
DOC_EXT = {".pdf", ".doc", ".docx", ".txt"}
TRADE_KW = re.compile(r"交割单|交歌单|交G单|割单|实盘|持仓|成交|交易记录|百万杯|梦想杯|时光杯|再战杯")
BULK_MIN = 12  # 目录（递归）内全是同一类文件且 >= 12 个时整目录软链（截图 / 每日帖）


def kind_of(rel: str) -> str:
    ext = Path(rel).suffix.lower()
    if ext in RAW_EXT:
        return "原始成交"
    if ext in AV_EXT:
        return "音视频"
    if TRADE_KW.search(Path(rel).name) or ext in IMG_EXT:
        return "交割单"
    return "心法"


def owner_of(rel: str):
    """最浅的、命中别名的路径层级决定归属（人物目录名优先于文件名里顺带提到的别名）。"""
    for c in CFG["collections"]:
        if "/" in c["aliases"][0] and any(a in rel for a in c["aliases"]):
            return "合集", c["name"]
    for part in Path(rel).parts:
        for p in CFG["people"]:
            if any(a in part for a in p["aliases"]):
                return "人物", p["name"]
        for c in CFG["collections"]:
            if any(a in part for a in c["aliases"]):
                return "合集", c["name"]
    return "合集", "未归类"


def fkey(path: Path) -> str:
    st = path.stat()
    h = hashlib.md5()
    with open(path, "rb") as f:
        h.update(f.read(1 << 20))
        if st.st_size > 2 << 20:
            f.seek(-(1 << 20), 2)
            h.update(f.read())
    return f"{st.st_size}-{h.hexdigest()}"


def ext_class(r):
    e = Path(r).suffix.lower()
    return "img" if e in IMG_EXT else e


def bulk_dirs(rels, aliases):
    """最高层的批量目录：递归内主导类型（截图 / 每日帖）>= BULK_MIN 且其它文件 <= 3 个。
    人物顶层目录（目录名本身含别名）只有全是截图时才整体收起，避免把心法文档吞掉。
    返回 {dir: 主导类型}；非主导文件由调用方单独软链。"""
    desc = defaultdict(list)
    for r in rels:
        parts = Path(r).parts[:-1]
        for i in range(2, len(parts) + 1):
            desc["/".join(parts[:i])].append(r)
    out = {}
    for d in sorted(desc, key=lambda x: x.count("/")):
        if any(d.startswith(o + "/") for o in out):
            continue
        rs = desc[d]
        cls = defaultdict(int)
        for r in rs:
            cls[ext_class(r)] += 1
        dom, n = max(cls.items(), key=lambda kv: kv[1])
        parts = Path(d).parts
        hit = [i for i, x in enumerate(parts) if any(a in x for a in aliases)]
        if not hit and not any(a in d for a in aliases):
            continue  # 人物目录的祖先（如 大佬合集/合集内容），不能整体收起
        top = bool(hit) and hit[0] == len(parts) - 1
        others_raw = all(c == dom or c in RAW_EXT for c in cls)
        if n >= BULK_MIN and len(rs) - n <= 3 and (not top or (dom == "img" and others_raw)):
            out[d] = dom
    return out


def scan():
    files = []
    for d, dirs, fs in os.walk(ROOT):
        dirs[:] = [x for x in dirs if not x.startswith(".")]
        for f in fs:
            p = Path(d) / f
            rel = str(p.relative_to(ROOT))
            if f.startswith(".") or any(ig in "/" + rel for ig in CFG["ignore"]):
                continue
            ext = p.suffix.lower()
            if ext not in RAW_EXT | AV_EXT | IMG_EXT | DOC_EXT:
                continue
            files.append(rel)
    return sorted(files)


def main():
    files = scan()
    # 1) 内容去重：同一内容只保留一个 canonical（优先 大佬合集 > 交割单汇总 > 其它）
    pref = lambda r: (0 if r.startswith("大佬合集") else 1 if r.startswith("《游资交割单") else 2, len(r), r)
    by_key = defaultdict(list)
    for rel in files:
        by_key[fkey(ROOT / rel)].append(rel)
    canon = {}
    for k, rels in by_key.items():
        rels.sort(key=pref)
        canon[rels[0]] = rels[1:]

    # 2) 归属 + 批量目录
    groups = defaultdict(list)  # (scope, owner) -> [rel]
    for rel in canon:
        groups[owner_of(rel)].append(rel)

    manifest = {"root": str(ROOT), "owners": {}}
    for (scope, owner), rels in sorted(groups.items()):
        base = DISTILL / scope / owner / "src"
        items = []
        aliases = next((x["aliases"] for x in CFG[("people" if scope == "人物" else "collections")]
                        if x["name"] == owner), [])
        bulk = bulk_dirs(rels, aliases)
        done = set()
        for d, dom in bulk.items():
            rs = sorted(r for r in rels if r.startswith(d + "/") and ext_class(r) == dom)
            done.update(rs)
            k = kind_of(rs[0])
            if k == "心法" and TRADE_KW.search(Path(d).name):
                k = "交割单"
            items.append({"type": "dir", "path": d, "n": len(rs), "kind": k,
                          "exts": sorted({Path(x).suffix.lower() for x in rs}), "files": rs})
        for r in sorted(set(rels) - done):
            items.append({"type": "file", "path": r, "kind": kind_of(r), "dups": canon[r],
                          "size": (ROOT / r).stat().st_size})
        # 软链
        used = set()
        for it in items:
            sub = base / it["kind"]
            sub.mkdir(parents=True, exist_ok=True)
            name = Path(it["path"]).name.strip()
            stem, ext = (name, "") if it["type"] == "dir" else (Path(name).stem.strip(), Path(name).suffix)
            cand, i = f"{stem}{ext}", 2
            while (it["kind"], cand) in used:
                cand, i = f"{stem}_{i}{ext}", i + 1
            used.add((it["kind"], cand))
            link = sub / cand
            if link.is_symlink() or link.exists():
                link.unlink()
            link.symlink_to(ROOT / it["path"])
            it["link"] = str(link.relative_to(DISTILL))
        manifest["owners"][f"{scope}/{owner}"] = items

    dup_n = sum(len(v) for v in canon.values())
    manifest["stats"] = {"files": len(files), "unique": len(canon), "duplicates": dup_n}
    (DISTILL / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1))
    print(f"files={len(files)} unique={len(canon)} dup={dup_n} owners={len(manifest['owners'])}")
    for k, items in manifest["owners"].items():
        print(f"  {k}: {len(items)} items")


if __name__ == "__main__":
    main()
