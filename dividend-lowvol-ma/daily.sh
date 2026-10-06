#!/bin/zsh
# 红利低波均线 v3 · 每日定时任务（建议每天北京时间 17:00 后调用，A 股收盘数据已齐）
#   ① 纸面账户 run：按今天开盘价执行昨天收盘挂的单 → 用今天收盘算 v3 信号、挂下一个交易日的单
#      （同一交易日只跑一次，周末 / 节假日自动跳过）
#   ② 今日信号面板（闸门状态 + 6 只红利 ETF 的信号与 v3 目标仓位）存进日志
# 不做：Notion（要 Claude）—— 下次调用 skill 时补齐。
# 输出：$STATE/logs/<日期>.log、$STATE/logs/last_run.json；失败弹 macOS 通知。
set -u
cd "$(dirname "$0")"
PY=${PYTHON:-python3}
export DISTILL_STATE=${DISTILL_STATE:-$HOME/.local/share/distills/dividend-lowvol-ma}
STATE=$DISTILL_STATE
mkdir -p "$STATE/logs"
DAY=$(date +%F)
LOG="$STATE/logs/$DAY.log"
STATUS=ok
FAILED=""

step() {   # step <名字> <命令...>
  local name=$1; shift
  echo "\n===== $name $(date '+%F %T') =====" >> "$LOG"
  if ! "$@" >> "$LOG" 2>&1; then
    STATUS=fail; FAILED="$FAILED $name"
    echo "!!! $name 失败" >> "$LOG"
  fi
}

step 纸面账户 $PY paper.py run
step 今日信号 $PY improve.py signal

PENDING=$($PY paper.py notion-pending 2>/dev/null | $PY -c "import json,sys; d=json.load(sys.stdin); print(len(d['trades']), len(d['nav']))" 2>/dev/null || echo "? ?")
$PY - "$STATUS" "$FAILED" "$PENDING" "$LOG" "$STATE" <<'PYEOF' > "$STATE/logs/last_run.json"
import json, sys, datetime as dt
st, failed, pending, log, state = sys.argv[1:6]
t, n = (pending.split() + ["?", "?"])[:2]
try:
    acc = json.load(open(f"{state}/paper/account.json"))
except Exception:
    acc = {}
print(json.dumps({"at": dt.datetime.now().isoformat(timespec="seconds"), "status": st,
                  "failed": failed.split(), "last_session": acc.get("last_session"),
                  "pending_order": acc.get("pending"),
                  "notion_pending": {"trades": t, "nav": n}, "log": log}, ensure_ascii=False, indent=1))
PYEOF

if [ "$STATUS" != ok ]; then
  osascript -e "display notification \"失败步骤:$FAILED（见 $LOG）\" with title \"红利低波均线 每日任务失败\"" 2>/dev/null
fi
find "$STATE/logs" -name '20*.log' -mtime +60 -delete 2>/dev/null
exit 0
