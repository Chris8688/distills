#!/usr/bin/env python3
"""交割单画像：任意券商导出的 A 股成交（csv / xls / xlsx，可多个文件）→ 持仓段统计 + 买点类型 + 对标游资基准。

口径与语料库 7 位 A 级游资完全相同（lib/trades.py + lib/enrich.py），所以可以直接横比：
  胜率 / PF / 盈亏比 / 持有天数 / 前 10 段占净利 / 次日低开是否就走 / 买点结构（打板 半路 平盘 低吸 竞价）/ 首板二板 / 封板率
用法:
  python3 profile.py 我的交割单.xls [更多文件...] [--name 我] [--out DIR]
输出: DIR（默认 ~/.trade-strategy/distill/youzi/profiles/<name>/）下 trades.csv episodes.csv profile.md entry_types.md
"""
import argparse
import json
import sys
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "lib"))
import enrich as E  # noqa: E402
import trades as T  # noqa: E402

BENCH = HERE / "data" / "benchmarks.json"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--name", default="me")
    ap.add_argument("--out")
    a = ap.parse_args()
    out = Path(a.out) if a.out else Path.home() / ".trade-strategy/distill/youzi/profiles" / a.name
    out.mkdir(parents=True, exist_ok=True)
    frames = []
    for f in a.files:
        for _, df in T.read_any(Path(f)).items():
            n = T.normalize(df, Path(f).name)
            if not n.empty:
                frames.append(n)
    if not frames:
        sys.exit("没有识别出成交表（需要含 证券代码 / 买卖标志或操作 / 成交价格 / 成交数量 等列）")
    t = T.clean_trades(pd.concat(frames, ignore_index=True))
    e, meta = T.episodes(t, T.trading_calendar())
    if e.empty:
        sys.exit("没有完整的持仓段（空仓→建仓→空仓）")
    t.to_csv(out / "trades.csv", index=False)
    md, js = T.profile(a.name, t, e, meta)
    (out / "profile.md").write_text(md)
    (out / "profile.json").write_text(json.dumps(js, ensure_ascii=False, indent=1))
    e["code"] = e["code"].astype(str).str.zfill(6)
    rng = e.groupby("code").agg(s=("entry", "min"), t=("exit", "max"))
    bars = {}
    for c, r in rng.iterrows():
        try:
            bars[c] = E.fetch(c, str((r.s - pd.Timedelta(days=40)).date()), str((r.t + pd.Timedelta(days=10)).date()))
        except RuntimeError:
            bars[c] = pd.DataFrame()
    x = E.classify(e, bars)
    x.to_csv(out / "episodes.csv", index=False)
    md2, js2 = E.summarize(a.name, x)
    if BENCH.exists():
        b = json.loads(BENCH.read_text())
        md2 += "\n## 对标游资（同口径）\n\n| 人物 | 区间 | 段数 | 胜率 | PF | 持 1 天 | 主买点 |\n|---|---|---|---|---|---|---|\n"
        md2 += f"| **{a.name}** | {js['period'][0][:7]}~{js['period'][1][:7]} | {js['n_episodes']} | {js['win_rate'] * 100:.0f}% | {js['profit_factor'] or 0:.2f} | " \
               f"{js['hold_dist'].get('1 天', 0) * 100:.0f}% | {max(js2['etype'], key=lambda k: js2['etype'][k]['占比'])} |\n"
        for r in b:
            md2 += f"| {r['person']} | {r['period']} | {r['n']} | {r['win'] * 100:.0f}% | {r['pf']:.2f} | {r['hold1'] * 100:.0f}% | {r['main']} |\n"
    (out / "entry_types.md").write_text(md2)
    print(md)
    print(md2)
    print(f"\n输出目录：{out}")


if __name__ == "__main__":
    main()
