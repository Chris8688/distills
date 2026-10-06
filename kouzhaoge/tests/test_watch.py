"""watch.py —— 检查点清单的解析与分类（不连网）。"""
from __future__ import annotations

import datetime as dt
import importlib.util
import os

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_spec = importlib.util.spec_from_file_location("kzg_watch", os.path.join(ROOT, "watch.py"))
watch = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(watch)


def _write(tmp_path, body: str) -> str:
    p = tmp_path / "w.yaml"
    p.write_text(body, encoding="utf-8")
    return str(p)


def test_shipped_watchlist_parses():
    data = watch.load()
    assert len(data["checkpoints"]) >= 10 and data["monitors"]
    for cp in data["checkpoints"]:
        assert isinstance(cp["due"], dt.date)
        assert cp.get("claim") and cp.get("check") and cp.get("src")


def test_classify_overdue_upcoming_done(tmp_path):
    path = _write(tmp_path, """
checkpoints:
  - {id: a, due: 2026-09-01, claim: x, check: y, src: z}
  - {id: b, due: 2026-10-20, claim: x, check: y, src: z}
  - {id: c, due: 2027-06-01, claim: x, check: y, src: z}
  - {id: d, due: 2026-08-01, claim: x, check: y, src: z, status: hit}
  - {id: e, due: 2026-08-02, claim: x, check: y, src: z, status: miss}
  - {id: f, due: 2026-08-03, claim: x, check: y, src: z, status: void}
""")
    cps = watch.load(path)["checkpoints"]
    g = watch.classify(cps, dt.date(2026, 10, 2), 45)
    assert [c["id"] for c in g["overdue"]] == ["a"]
    assert [c["id"] for c in g["upcoming"]] == ["b"]
    assert [c["id"] for c in g["later"]] == ["c"]
    assert {c["id"] for c in g["done"]} == {"d", "e", "f"}
    s = watch.scorecard(cps)
    assert (s["hit"], s["miss"], s["void"], s["pending"], s["hit_rate"]) == (1, 1, 1, 3, 0.5)


def test_bad_status_and_dup_id_rejected(tmp_path):
    with pytest.raises(ValueError, match="status"):
        watch.load(_write(tmp_path, "checkpoints:\n  - {id: a, due: 2026-10-01, status: done}\n"))
    with pytest.raises(ValueError, match="重复"):
        watch.load(_write(tmp_path, "checkpoints:\n  - {id: a, due: 2026-10-01}\nmonitors:\n  - {id: a, rule: r}\n"))


def test_cli_runs(capsys):
    assert watch.main(["--today", "2026-10-02", "--monitors"]) == 0
    out = capsys.readouterr().out
    assert "已到期未核" in out and "常驻阈值" in out and "命中率" in out
