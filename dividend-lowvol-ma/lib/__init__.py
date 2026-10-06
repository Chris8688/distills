"""脚本依赖的取数与交易日历，自包含。"""
import os

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 行情 / 估值缓存，不进 git；删掉即重抓
CACHE = os.path.expanduser(os.environ.get("DISTILL_CACHE", "~/.cache/distill/dividend-lowvol-ma"))
os.makedirs(CACHE, exist_ok=True)
# 纸面账户账本（paper/）与每日日志（logs/）的根目录 —— 个人数据。
# 优先环境变量 DISTILL_STATE；否则本目录下已有账本（私有工作副本）就用本目录；再否则放仓库外。
STATE = os.path.expanduser(os.environ.get("DISTILL_STATE") or (
    _ROOT if os.path.exists(os.path.join(_ROOT, "paper", "account.json"))
    else "~/.local/share/distills/dividend-lowvol-ma"))
