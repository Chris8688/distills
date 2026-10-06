"""脚本依赖的取数，自包含。"""
import os

# 行情 / 估值缓存，不进 git；删掉即重抓
CACHE = os.path.expanduser(os.environ.get("DISTILL_CACHE", "~/.cache/distill/dividend-lowvol-ma"))
os.makedirs(CACHE, exist_ok=True)
