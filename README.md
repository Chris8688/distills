# distill —— 已废弃投资思路蒸馏的存档

这里存放**已经证伪、退出使用**的投资思路蒸馏：把外部作者的投资方法整理成笔记，把能量化的规则写成脚本做 point-in-time 回测。

> 本仓库内容**只作历史存档，不要用于选股或交易**，也不构成任何投资建议。

## 索引

| 目录 | 来源 | 一句话 | 废弃原因 |
|---|---|---|---|
| [sixiang-gangyin](sixiang-gangyin/)（思想钢印，[最终报告](sixiang-gangyin/README.md)） | 知识星球 51284144254414，2020 ~ 2026 共 2161 帖、约 510 万字 | 白马价投「确定性 / 景气度 / 赔率」三原则 → 赔率胜率量化与机械规则（均线网格、乖离率波段）→ 宏观对冲 → 资金面 + AI | 2026-10-04 起废弃：可量化选股规则做 PIT 检验，没有一条值得用，极限赔率选股显著差于随机（[docs/03](sixiang-gangyin/docs/03-选股规则检验.md)）；交易规则跑不赢同仓位不择时（[docs/02](sixiang-gangyin/docs/02-交易规则回测.md)） |

## 目录结构

```
<slug>/
  README.md             # 最终报告：结论、体系、检验结果、仍可参考的部分
  DEPRECATED            # 废弃日期与原因
  SKILL.md              # Claude Code skill 定义（保留，便于回看当时怎么用）
  docs/01-蒸馏笔记.md    # 演化阶段、规则出处、与常见交易纪律的对照
  docs/02-*.md          # 交易规则回测
  docs/03-*.md          # 选股规则检验
  docs/notes/           # 分时期原文证据摘录
  *.py                  # 计算器与回测脚本
  lib/                  # 脚本依赖的取数与判据函数（自包含）
```

## 复现

在仓库根目录运行：

```bash
pip install numpy yfinance akshare
python3 sixiang-gangyin/bands.py odds --price 12 --up 18 --down 8 --win 0.5   # 计算器，不需要数据
python3 sixiang-gangyin/backtest_trading.py      # 交易规则：行情从 TickFlow / akshare 在线抓取
python3 sixiang-gangyin/backtest.py --years      # 美股选股规则：需要研究数据目录
python3 sixiang-gangyin/backtest_cn.py --years   # A股选股规则：需要研究数据目录
```

- 缓存默认放在 `~/.cache/distill/sixiang-gangyin/`，可用环境变量 `DISTILL_CACHE` 改。
- 选股规则回测需要研究数据目录，默认是 `sixiang-gangyin/data/`（不进 git），可用环境变量 `DISTILL_DATA` 指到别处。需要的文件：
  - `fundamentals.jsonl.gz`：美股 SEC XBRL 财报，每行一条 `{ticker, concept, kind, start, end, filed, val}`
  - `wide_bars.json`：美股日K `{ticker: [{date, close, ...}]}`
  - `cn_*_fd.json` / `cn_fundamentals.json`：A股累计口径财报；`cn_*_px.json`：A股日K
- 原始语料是付费内容，不进 git。

## 思想钢印：结论速览

| 规则 | 样本 | 结论 |
|---|---|---|
| 极限赔率选股（2:1 / 3:1） | 美股 206 只，PIT | 显著差于随机剔除 |
| 高 ROE 均值回归、营收增速见顶 | 美股 + A股 696 只 | 勉强通过，效应弱且不稳，不作工具 |
| 估值扩张、PBR 历史分位 | 同上 | 随行情翻转，不能作剔除或卖出依据 |
| 长线波段（60 日乖离率） | 16 只宽基与行业 ETF / 申万二级 124 个行业 | 只在 2021 ~ 2023H1 震荡下跌段赢；申万 26 年输给恒定同仓位 |
| 均线网格（含 60/120 止损版） | 同上 | 同上 |
| 超跌反弹网格（年线） | 同上 | 同上 |

根本原因是方向：这些交易规则都是逆势的，跌了加仓、涨了减仓。震荡市里有效，单边下跌段（2001 ~ 2008）越跌越加，单边上涨段提前卖光；而未来是不是震荡市只能事后知道。
仍可参考的是他的风险框架，以及「周期股看 PB、别把周期当成长」「同属性的标的起不到分散作用」这类观点。
