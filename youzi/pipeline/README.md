# 游资语料库整理流水线

把网盘《游资心法 + 讲座全集》整理成按人物归档、可全文检索的语料库，并从交割单解析出持仓段统计。
语料库（原件软链、抽取文本、成交明细）只在本机，**不进 git**；这里只放脚本和人物登记表。

- 语料库位置：`$YOUZI_CORPUS`，默认 `~/Baidu-Pan/游资心法+讲座全集1/distill/`（原始网盘目录是它的上一级）。
- 人物登记表：`people.yaml`（唯一事实源：别名、流派、资料等级）。改完重跑 `build.py` 与 `index.py`。
- 依赖：`pdftotext`（poppler）、macOS `textutil`、`pyyaml`；OCR 用 Apple Vision（`swiftc -O ocrpdf.swift -o ocrpdf`）；
  `ocr_trades.py` / `enrich.py` / `market_fetch.py` 需要 pandas、baostock（`PY=$YOUZI_CORPUS/.venv/bin/python`）。

## 步骤

| # | 脚本 | 作用 | 产出（语料库内） |
|---|---|---|---|
| 1 | `build.py` | 按 `people.yaml` 别名把原始文件归到人物 / 合集，内容去重，建软链 | `manifest.json`、`人物/<名>/src/`、`合集/<名>/src/` |
| 2 | `extract.py` | pdftotext / textutil 抽文本，纯图页走 Vision OCR；`<!-- page N -->` 页码锚点 | `*/text/*.md` |
| 3 | `split_58.py` | 《58位大佬心法》按章切给各人物 | `人物/<名>/text/58位·*.md`、`data/58_toc.yaml` |
| 4 | `run_trades.sh` | `trades.py`（原始 xls/csv）→ `ocr_trades.py`（OCR 交割单成交行，价×量=额 + 当日高低价校验）→ `enrich.py`（接日线分买点类型） | `人物/<名>/trades/` |
| 5 | `index.py` | 人物 × 流派 × 资料总表 | `人物索引.md` |
| — | `market_fetch.py` | 全市场不复权日线（含退市股，baostock） | `~/.trade-strategy/distill/youzi/market/bars/` |

```bash
cd ~/distills/youzi/pipeline
python3 build.py && python3 extract.py --workers 3 && python3 split_58.py
./run_trades.sh
python3 index.py
cd .. && python3 build_data.py      # 汇总进本仓库 data/ 与 docs/
```

## 逐人蒸馏

逐人 `蒸馏笔记.md` / `rules.yaml` 由子代理按 [`DISTILL_BRIEF.md`](DISTILL_BRIEF.md) 写进语料库 `人物/<名>/`，
再由 `build_data.py` 拷进 `docs/people/`（只拷笔记和统计，不拷原文、原件、成交明细）。
