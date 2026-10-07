#!/bin/bash
# 第 4 步全流程：OCR 成交行 → 统计画像 → 接行情分类（产出写进语料库 人物/<名>/trades/）
cd "$(dirname "$0")"
CORPUS="${YOUZI_CORPUS:-$HOME/Baidu-Pan/游资心法+讲座全集1/distill}"
PY="$CORPUS/.venv/bin/python"
python3 -u trades.py                       # 先跑原始 xls（ocr_trades 需要它的区间）
"$PY" -u ocr_trades.py
python3 trades.py
"$PY" -u enrich.py
