#!/usr/bin/env python3
"""manifest.json → 每个人物/合集的 text/<文档>.md（可搜索文本）。

路由：
  .doc/.docx      → textutil
  .pdf            → 逐页 pdftotext；去水印后汉字 < MIN_HAN 的页交给 ocrpdf（Apple Vision）补 OCR
                    同一人物下有同名 .docx 的 pdf 跳过（涅盘重升实盘帖：Word 版是原文）
  图片 / 截图目录   → 逐张 OCR 合并成一个 md
  每日帖 pdf 目录   → 逐个处理合并成一个 md（按文件名排序 = 按日期）
  音视频 / 原始成交 → 跳过（成交数据见 trades.py）
增量：输出已存在且比源新则跳过。状态写 <owner>/text/_status.json。

用法: python3 extract.py [--owner 人物/赵老哥] [--workers 3] [--dry]
"""
import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
DISTILL = Path(os.environ.get("YOUZI_CORPUS", Path.home() / "Baidu-Pan/游资心法+讲座全集1/distill"))  # 语料库（本机，不进 git）
ROOT = DISTILL.parent
OCR = HERE / "ocrpdf"  # swiftc -O ocrpdf.swift -o ocrpdf
MIN_HAN = 40
HAN = re.compile(r"[一-鿿]")
WATERMARK = [
    re.compile(r"ｗｗｗ．９８ｋｅｃｈｅｎｇ．ｃｏｍ"),
    re.compile(r"获取更多股票课程、资讯群"),
    re.compile(r"更多资料更新微信[:：]?\s*QQ\d+"),
    re.compile(r"QQ4149\d{4,}"),
    re.compile(r"(?m)^\s*w*\s*\.?98kecheng\.com\s*$"),
    re.compile(r"ｗｗｗ．９８ｋｅｃｈｅｎｇ．ｃｏｍ|www\.98kecheng\.com"),
]


def clean(t: str) -> str:
    for w in WATERMARK:
        t = w.sub("", t)
    t = re.sub(r"[ \t]+\n", "\n", t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def han(t: str) -> int:
    return len(HAN.findall(t))


def run(cmd, timeout=3600):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def norm_stem(name: str) -> str:
    s = Path(name).stem.strip()
    s = re.sub(r"_\d+$", "", s)
    return re.sub(r"\s+", "", s)


def ocr_pages(pdf: Path, pages, scale="2.0") -> dict:
    out = {}
    if not pages:
        return out
    with tempfile.NamedTemporaryFile(suffix=".md", delete=False) as tf:
        tmp = tf.name
    # 分块，避免命令行过长 / 单次内存过大
    for i in range(0, len(pages), 200):
        chunk = pages[i:i + 200]
        r = run([str(OCR), str(pdf), tmp, scale, "list:" + ",".join(map(str, chunk))])
        if r.returncode != 0:
            continue
        txt = Path(tmp).read_text(errors="ignore")
        for m in re.finditer(r"<!-- page (\d+) -->\n(.*?)(?=\n<!-- page \d+ -->|\Z)", txt, re.S):
            out[int(m.group(1))] = m.group(2)
    Path(tmp).unlink(missing_ok=True)
    return out


def pdf_text(pdf: Path):
    """返回 (pages[list[str]], n_ocr)。"""
    r = run(["pdftotext", "-enc", "UTF-8", str(pdf), "-"])
    pages = r.stdout.split("\f") if r.returncode == 0 else []
    if pages and pages[-1].strip() == "":
        pages = pages[:-1]
    if not pages:
        info = run(["pdfinfo", str(pdf)]).stdout
        m = re.search(r"Pages:\s+(\d+)", info)
        pages = [""] * (int(m.group(1)) if m else 0)
    pages = [clean(p) for p in pages]
    need = [i + 1 for i, p in enumerate(pages) if han(p) < MIN_HAN]
    got = ocr_pages(pdf, need)
    for p, t in got.items():
        t = clean(t)
        if han(t) > han(pages[p - 1]):
            pages[p - 1] = t
    return pages, len(need)


def doc_text(path: Path) -> str:
    r = run(["textutil", "-convert", "txt", "-stdout", str(path)])
    return clean(r.stdout)


def img_text(path: Path) -> str:
    with tempfile.NamedTemporaryFile(suffix=".md", delete=False) as tf:
        tmp = tf.name
    run([str(OCR), str(path), tmp])
    t = Path(tmp).read_text(errors="ignore") if Path(tmp).exists() else ""
    Path(tmp).unlink(missing_ok=True)
    return clean(re.sub(r"<!-- page \d+ -->\n", "", t))


def one_file(rel: str):
    p = ROOT / rel
    ext = p.suffix.lower()
    if ext == ".pdf":
        pages, n_ocr = pdf_text(p)
        body = "\n\n".join(f"<!-- page {i + 1} -->\n{t}" for i, t in enumerate(pages))
        return body, {"method": "pdftotext+ocr" if n_ocr else "pdftotext", "pages": len(pages), "ocr_pages": n_ocr}
    if ext in (".doc", ".docx", ".txt"):
        return doc_text(p), {"method": "textutil", "pages": 0, "ocr_pages": 0}
    if ext in (".png", ".jpg", ".jpeg"):
        return img_text(p), {"method": "ocr-img", "pages": 1, "ocr_pages": 1}
    return None, None


def job(owner: str, it: dict, out: Path):
    t0 = time.time()
    if it["type"] == "file":
        body, meta = one_file(it["path"])
    else:
        parts, meta = [], {"method": "dir", "pages": 0, "ocr_pages": 0, "files": it["n"]}
        for rel in it["files"]:
            b, m = one_file(rel)
            if b is None:
                continue
            parts.append(f"## {Path(rel).name}\n\n{b}")
            meta["pages"] += m["pages"]
            meta["ocr_pages"] += m["ocr_pages"]
        body = "\n\n".join(parts)
    if body is None:
        return None
    hdr = f"<!-- source: {it['path']} | method: {meta['method']} | pages: {meta['pages']} | ocr_pages: {meta['ocr_pages']} -->\n\n"
    out.write_text(hdr + body)
    meta.update({"source": it["path"], "out": str(out.relative_to(DISTILL)), "han": han(body),
                 "secs": round(time.time() - t0, 1)})
    return meta


def plan(manifest, only=None):
    jobs = []
    for owner, items in manifest["owners"].items():
        if only and owner != only:
            continue
        docx_stems = {norm_stem(i["path"]) for i in items if i["type"] == "file" and i["path"].lower().endswith(".docx")}
        tdir = DISTILL / owner / "text"
        seen_pdf = set()  # 近似重复：同名（去 _2 后缀）且页数相同的 pdf 只处理一个
        for it in items:
            if it["type"] == "file" and it["path"].lower().endswith(".pdf"):
                info = run(["pdfinfo", str(ROOT / it["path"])]).stdout
                m = re.search(r"Pages:\s+(\d+)", info)
                k = (norm_stem(it["path"]), m.group(1) if m else "?")
                if k in seen_pdf:
                    continue
                seen_pdf.add(k)
            exts = it.get("exts") or [Path(it["path"]).suffix.lower()]
            if any(e in (".mp3", ".mp4", ".avi", ".csv", ".xls", ".xlsx") for e in exts):
                continue
            if it["type"] == "file" and it["path"].lower().endswith(".pdf") and norm_stem(it["path"]) in docx_stems:
                continue
            name = Path(it["link"]).name
            out = tdir / (Path(name).stem + ".md" if it["type"] == "file" else name + ".md")
            src_m = (ROOT / it["path"]).stat().st_mtime
            if out.exists() and out.stat().st_mtime > src_m and out.stat().st_size > 200:
                continue
            jobs.append((owner, it, out))
    return jobs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--owner")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    manifest = json.loads((DISTILL / "manifest.json").read_text())
    jobs = plan(manifest, a.owner)
    print(f"{len(jobs)} jobs", flush=True)
    if a.dry:
        for o, it, out in jobs:
            print(o, it["path"])
        return
    for o, _, out in jobs:
        out.parent.mkdir(parents=True, exist_ok=True)
    done = 0
    with ThreadPoolExecutor(a.workers) as ex:
        futs = {ex.submit(job, o, it, out): (o, it, out) for o, it, out in jobs}
        for f in as_completed(futs):
            o, it, out = futs[f]
            done += 1
            try:
                m = f.result()
            except Exception as e:  # noqa
                print(f"[{done}/{len(jobs)}] FAIL {o} {it['path']}: {e}", flush=True)
                continue
            if m is None:
                continue
            st = out.parent / "_status.json"
            s = json.loads(st.read_text()) if st.exists() else {}
            s[out.name] = m
            st.write_text(json.dumps(s, ensure_ascii=False, indent=1))
            print(f"[{done}/{len(jobs)}] {o} {out.name} pages={m['pages']} ocr={m['ocr_pages']} han={m['han']} {m['secs']}s", flush=True)


if __name__ == "__main__":
    sys.exit(main())
