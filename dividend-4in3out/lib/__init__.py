"""脚本依赖的取数与交易日历，自包含。"""
import os

# 行情 / 财报缓存，不进 git
CACHE = os.path.expanduser(os.environ.get("DISTILL_CACHE", "~/.cache/distill/dividend-4in3out"))
# 纸面账户账本、Notion 同步进度、每日日志 —— 个人数据，放在仓库外
STATE = os.path.expanduser(os.environ.get("DISTILL_STATE", "~/.local/share/distills/dividend-4in3out"))
