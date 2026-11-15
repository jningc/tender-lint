"""门禁的判定逻辑：多数票、flaky 标记、与上次比较。不跑模型。"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "eval"))

import run_regression as rg  # noqa: E402


def _run(detail: dict, covered: int = 0, star: int = 0) -> dict:
    return {"detail": detail, "markers": {"covered_pages": ["x"] * covered, "star_rows": star},
            "rows": 10, "counts": {}}


def test_majority_unanimous_is_stable():
    assert rg.majority(["hit", "hit", "hit"]) == ("hit", False)


def test_majority_two_of_three_is_flaky():
    assert rg.majority(["hit", "hit", "miss"]) == ("hit", True)


def test_majority_all_different_is_unstable():
    assert rg.majority(["hit", "miss", "absent"]) == ("unstable", True)


def test_aggregate_splits_stable_and_all(monkeypatch):
    monkeypatch.setattr(rg, "KINDS", {
        "p1": {"kind": "positive", "doc": "d"},
        "p2": {"kind": "positive", "doc": "d"},
        "n1": {"kind": "negative", "doc": "d"},
        "other": {"kind": "positive", "doc": "别的文档"},
    })
    runs = [
        _run({"p1": "hit", "p2": "hit", "n1": "hit"}),
        _run({"p1": "hit", "p2": "miss", "n1": "hit"}),
        _run({"p1": "hit", "p2": "miss", "n1": "violated"}),
    ]
    doc = rg.aggregate("d", runs)
    assert doc["flaky"] == ["n1", "p2"]     # 负例与 p2 三次都不一致
    assert doc["positive"] == {"hit": 1, "total": 2}          # p1 hit、p2 多数 miss
    assert doc["positive_stable"] == {"hit": 1, "total": 1}   # 只算 p1
    assert doc["negative"] == {"hit": 1, "total": 1}          # 多数是 hit
    assert doc["negative_stable"] == {"hit": 0, "total": 0}   # 但不算进稳定题


def test_gate_passes_when_equal():
    cur = {"totals": {"pos_stable_hit": 10, "neg_stable_hit": 2}, "markers": {"covered_pages": 3}}
    base = {"totals": {"pos_stable_hit": 10, "neg_stable_hit": 2}, "markers": {"covered_pages": 3}}
    assert rg.gate(cur, base) == []


def test_gate_flags_drops():
    cur = {"totals": {"pos_stable_hit": 9, "neg_stable_hit": 1, "violated": 2},
           "markers": {"covered_pages": 2}}
    base = {"totals": {"pos_stable_hit": 10, "neg_stable_hit": 2, "violated": 1},
            "markers": {"covered_pages": 3}}
    reasons = rg.gate(cur, base)
    assert len(reasons) == 4
    assert any("稳定正例" in r for r in reasons)
    assert any("稳定负例" in r for r in reasons)
    assert any("★" in r for r in reasons)
    assert any("负例违规" in r for r in reasons)


def test_gate_allows_pre_existing_violation():
    """基线自带 1 处已知违规（cz-neg）：不增加就不算退化，增加了才红。"""
    cur = {"totals": {"pos_stable_hit": 14, "neg_stable_hit": 2, "violated": 1},
           "markers": {"covered_pages": 2}}
    base = {"totals": {"pos_stable_hit": 14, "neg_stable_hit": 2, "violated": 1},
            "markers": {"covered_pages": 2}}
    assert rg.gate(cur, base) == []


def test_gate_without_baseline_passes():
    cur = {"totals": {"pos_stable_hit": 0, "neg_stable_hit": 0}, "markers": {"covered_pages": 0}}
    assert rg.gate(cur, None) == []
