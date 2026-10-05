"""脚本依赖的取数与判据函数，自包含，只供本目录的计算器和回测复现使用。"""
import os

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 缓存（行情 / 申万指数 / 回测面板），不进 git
CACHE = os.path.expanduser(os.environ.get("DISTILL_CACHE", "~/.cache/distill/sixiang-gangyin"))
# 研究数据目录（美股 SEC XBRL 财报、A股财报与日K，格式见 README），不进 git
DATA = os.path.expanduser(os.environ.get("DISTILL_DATA", os.path.join(_HERE, "data")))
