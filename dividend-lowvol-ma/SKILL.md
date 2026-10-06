---
name: dividend-lowvol-ma
description: 红利低波均线策略 v3（蒸馏自雪球 稳我才「低波均线」，2026-09-30）—— 512890 红利低波ETF：收盘 ≤ 182 日均线且指数 PE ≤ 20 且「上证 A 股股息率 − 10 年国债 ≥ −1.0pp」才买，收盘 > 均线 7% 卖；持有时仓位 = min(1, 18% ÷ 近 60 日波动)；信号按收盘、下一个交易日开盘成交。作者原规则回测已逐笔复现（25/25），原规则熊市满仓到底（上证红利 2008 年 −64%），v3 修补后上证红利 2007 起 +7.8% / −19%、2008 年 −3.3%，512890 +19.5% / −12%。四件事：① 今日信号（闸门状态、乖离、v3 目标仓位）② 回测与检验（复现、网格、分段、跨 5 只红利 ETF、估值闸门）③ 1000 万人民币纸面账户（512890，v3 规则，每日定时落账）④ 把纸面账户记账到 Notion「红利低波均线 v3 纸面交易」板块；以及与股息率「四进三出」的对比。只出分析，不下真单。别名：红利低波均线、低波均线、稳我才、182 日均线、红利低波择时、红利低波 v3。当用户提到红利低波均线 / 稳我才 / 低波均线，或问 512890 / 563020 现在该持有还是空仓、182 日均线乖离、股债差闸门、这个策略靠不靠谱、它和四进三出哪个好、红利低波纸面账户 / 纸面交易怎么样了、更新红利低波 Notion 记账时使用。
user-invocable: true
allowed-tools:
  - Bash
  - Read
---

# 红利低波均线 v3

一个**投资思路蒸馏**，不下真单。报告在 `README.md`；细节在 `docs/01-蒸馏笔记.md`（规则、复现、稳健性）、
`docs/02-修补检验.md`（v2）、`docs/03-与四进三出对比.md`、`docs/04-估值闸门探索.md`（v3）、`docs/05-纸面交易与同步.md`；
原始截图与转录在 `data/`。回答前先读报告，不要凭记忆。Notion 方法论页与本地 docs 一致，改规则时两边一起改。

所有命令在本目录下运行。依赖 Python 3.11+、`akshare`；行情 TickFlow，缓存 `~/.cache/distill/dividend-lowvol-ma/`（`DISTILL_CACHE`）。
纸面账户账本与日志在 `$DISTILL_STATE/paper/`、`$DISTILL_STATE/logs/`：没设环境变量时，本目录下已有 `paper/account.json` 就用本目录，
否则用 `~/.local/share/distills/dividend-lowvol-ma/`。

## 定时任务（launchd，每天北京时间收盘后）

`daily.sh` 自动跑：`paper.py run`（按当天开盘价执行昨天收盘挂的单 → 用当天收盘算 v3 信号、挂下一个交易日的单；
同一交易日只跑一次，周末 / 节假日自动跳过）→ `improve.py signal`（6 只红利 ETF 的信号面板存进日志）。
输出 `logs/<日期>.log`、`logs/last_run.json`；失败弹 macOS 通知。**不做** Notion（要 Claude）—— 由默认流程第 0 步补齐。
- 安装：`cp launchd/com.ktrade.dividend-lowvol-ma.plist ~/Library/LaunchAgents/ && launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.ktrade.dividend-lowvol-ma.plist`
- 手动触发：`launchctl kickstart gui/$(id -u)/com.ktrade.dividend-lowvol-ma`；停用：`launchctl bootout gui/$(id -u)/com.ktrade.dividend-lowvol-ma`
- 每天的成交会改 `paper/` 下的账本文件，用户说提交时再提交。

## 默认流程（用户只说「红利低波均线」/ 直接调用、没有具体问题时）

0. **看定时任务**：读 `logs/last_run.json` —— `status` 不是 ok 就先报告失败步骤和日志；`notion_pending` 有积压就在第 3 步补齐。
   最近几天的成交从 `logs/*.log` 汇总给用户。
1. **纸面账户**：`python3 paper.py run`（已跑过只打印）→ `python3 paper.py status` —— 报告本次成交、当前仓位、待执行订单。
2. **今日信号**：`python3 improve.py signal` —— 闸门状态（股债差）、6 只红利 ETF 的乖离、60 日波动、信号、v3 目标仓位。
3. **Notion 记账**（见 ④）—— 有新流水 / 净值才写。

最后用一张短表总结：本次成交、仓位 vs 目标、闸门、下一个交易日要执行的单。用户问的是具体问题就只做对应的那一步。

## ① 今日信号

```bash
python3 improve.py signal       # 股债差闸门状态 + 6 只红利 ETF 的乖离、信号、v3 目标仓位（重抓行情）
```

股息率 / 国债收益率读缓存（`market_dy_sh.csv`、`cn10y.csv`）；`paper.py run` 会把超过 20 小时的缓存自动重抓。

## ② 回测与检验

```bash
python3 improve.py compare      # 5 只 × 买持 / 原规则 / v2 / v3
python3 improve.py oos          # 2018 前 / 2019 后
python3 improve.py year 510880  # 逐年
python3 improve.py gate         # 估值闸门网格（股息率 / 股债差）
python3 backtest.py replicate   # 与作者 25 笔调仓逐笔对照
python3 backtest.py grid        # MA 长度 × 卖出乖离 网格
python3 -m pytest tests/        # 单元测试（不连网）
```

## ③ 纸面账户（1000 万人民币，512890，v3）

```bash
python3 paper.py run --dry-run   # 预演
python3 paper.py run             # 落账：份额折算 → 执行昨天挂的单（今天开盘价）→ 算今天收盘信号、挂单 → 记净值
python3 paper.py status          # 持仓、现金、净值、512890 买持对照、待执行订单
```

- 成交：收盘出信号，**下一个交易日开盘价**成交（与回测一致）；一手 100 股取整；佣金 0.025%（最低 5 元），ETF 免印花税。
- 调仓：新建 / 清仓立即；偏离 >10pp 立即；距上次成交 ≥5 个交易日且偏离 >2pp 再调。
- 份额拆分 / 基金分红：前复权与不复权比值变动时折算份额（分红视为再投入），记一笔 ADJ。
- 2026-09-30 收盘出第一个信号（持有、目标 100%），下一个交易日（2026-10-08）开盘建仓。

## ④ Notion 记账（每次 `paper.py run` 落账后做）

板块：Notion 私有页「红利低波均线 v3 纸面交易」，下有方法论页和三张表。ID 都在 `paper/notion.json` 的 `db` 里：
`trades`（交易流水）/ `positions`（当前持仓，只有一行）/ `nav`（每日净值）是 `collection://…` 数据源，`root` / `methodology` 是页面。

1. `python3 paper.py notion-pending` → 未同步的 `trades`、`nav`，当前 `position`（带 `notion_page`）和 `pending_order`。
2. 用 Notion MCP：
   - `trades` 每条 → 交易流水建一行（标的=name、流水号=id、交易日=session、代码、方向、股数、价格、金额、费用、成交后现金、原因、状态=有效）。
   - `nav` 每条 → 每日净值建一行（交易日标签=交易日=session、现金、市值、总资产、净值、累计收益%=return_pct、仓位%=weight_pct、
     信号、目标仓位%=target_pct、乖离=dev、60日波动=vol、股债差=spread、闸门=gate、512890买持净值=bh_nav）。
   - `position`：`update_properties` 更新那一行的股数 / 成本价 / 现价 / 市值 / 权重% / 目标仓位% / 盈亏% / 信号 / 建仓日 / 状态（持有 / 空仓）。
3. `python3 paper.py notion-mark --trades <id,…> --nav <session,…>` 记进度；再跑一次 `notion-pending` 确认 trades / nav 为空。
4. 方法论有变化时同步更新 Notion 方法论页（`methodology` id）。

## 回答口径

- 给信号时写清数据日期、闸门状态（股债差）、乖离、60 日波动、信号和 **v3 目标仓位**，以及「信号次日开盘执行」。
- 缺省推荐 v3（闸门 −1.0pp + 缩仓 18% + PE≤20）；缩仓目标是风险偏好（12% 最稳 / 18% 缺省 / 不缩仓收益最高、回撤 −25%）。
- 159905 深证红利：按报告结论不适用这条规则。
- 引用作者的 20% 年化时，**同时**给出打折后的预期（12%~15%）和熊市回撤。
- 与四进三出比较：是取舍（四进三出长期收益高、熊市 −30%；v3 回撤浅、一只 ETF），提醒回撤口径不同（月末 vs 日频），且都押同一个高股息风格。

## 边界

- 纸面账户不是回测：从 2026-09-30 才开始，短期内没有统计意义；判断规则好坏看 `docs/02`、`docs/04` 的历史回放。
- 接入真实交易属于「新增策略引擎」，要单独规划，不在本 skill 里做。
- 不是投资建议；数据源（TickFlow / 蛋卷 / 乐咕乐股 / akshare）偶有缺漏。
