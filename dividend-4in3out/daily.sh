#!/bin/zsh
# 股息率模式 · 每日定时任务（建议每天北京时间 17:00 后调用，A 股收盘数据已齐）
#   ① 作者净值（匿名）  ② 纸面账户 run（同一交易日只交易一次，周末/节假日自动跳过）
#   ③ 纸面持仓体检 + 买入清单存档
# 不做：雪球持仓/调仓（要登录）、Notion（要 Claude）—— 下次调用 skill 时补齐。
# 输出：$STATE/logs/<日期>.log、$STATE/logs/last_run.json（STATE 在仓库外）；失败弹 macOS 通知。
set -u
cd "$(dirname "$0")"
PY=${PYTHON:-python3}
STATE=${DISTILL_STATE:-$HOME/.local/share/distills/dividend-4in3out}
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

step 作者净值 $PY sync_xueqiu.py nav
step 纸面账户 $PY paper.py run
step 持仓体检与清单 $PY screen.py --rules observed --paper --top 30

PENDING=$($PY paper.py notion-pending 2>/dev/null | $PY -c "import json,sys; d=json.load(sys.stdin); print(len(d['trades']), len(d['nav']))" 2>/dev/null || echo "? ?")
$PY - "$STATUS" "$FAILED" "$PENDING" "$LOG" "$STATE" <<'EOF' > "$STATE/logs/last_run.json"
import json, sys, datetime as dt
st, failed, pending, log, state = sys.argv[1:6]
t, n = (pending.split() + ["?", "?"])[:2]
acc = json.load(open(f"{state}/paper/account.json"))
print(json.dumps({"at": dt.datetime.now().isoformat(timespec="seconds"), "status": st,
                  "failed": failed.split(), "last_session": acc.get("last_session"),
                  "notion_pending": {"trades": t, "nav": n}, "log": log}, ensure_ascii=False, indent=1))
EOF

if [ "$STATUS" != ok ]; then
  osascript -e "display notification \"失败步骤:$FAILED（见 $LOG）\" with title \"股息率模式 每日任务失败\"" 2>/dev/null
fi
# 只保留最近 60 天日志
find "$STATE/logs" -name '20*.log' -mtime +60 -delete 2>/dev/null
exit 0
