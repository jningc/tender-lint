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


def _doc(name: str, questions: dict, flaky: list[str] | None = None) -> dict:
    return {"doc": name, "questions": questions, "flaky": flaky or []}


def _kinds(*ids, kind="positive", doc="d"):
    return {q: {"kind": kind, "doc": doc} for q in ids}


def test_gate_passes_when_equal(monkeypatch):
    monkeypatch.setattr(rg, "KINDS", {**_kinds("p1"), **_kinds("n1", kind="negative")})
    cur = {"docs": [_doc("d", {"p1": "hit", "n1": "hit"})],
           "totals": {"violated": 0}, "markers": {"covered_pages": 3}}
    base = {"docs": [_doc("d", {"p1": "hit", "n1": "hit"})],
            "totals": {"violated": 0}, "markers": {"covered_pages": 3}}
    assert rg.gate(cur, base) == []


def test_gate_flags_drops(monkeypatch):
    monkeypatch.setattr(rg, "KINDS", {**_kinds("p1", "p2"), **_kinds("n1", kind="negative")})
    cur = {"docs": [_doc("d", {"p1": "miss", "p2": "miss", "n1": "miss"})],
           "totals": {"violated": 2}, "markers": {"covered_pages": 2}}
    base = {"docs": [_doc("d", {"p1": "hit", "p2": "hit", "n1": "hit"})],
            "totals": {"violated": 1}, "markers": {"covered_pages": 3}}
    reasons = rg.gate(cur, base)
    assert len(reasons) == 4
    assert any("正例" in r for r in reasons)
    assert any("负例" in r for r in reasons)
    assert any("★" in r for r in reasons)
    assert any("负例违规" in r for r in reasons)


def test_gate_ignores_shrinking_stable_set(monkeypatch):
    """稳定集变小不是退化：同一批共同稳定的题一条没掉，就不该判红。

    上一版按「稳定题得分」总数比，温岭 4 题变 flaky 后分母 16→14，
    分数自然从 15 掉到 12，把模型抖动画成了代码退化。
    """
    monkeypatch.setattr(rg, "KINDS", {q: {"kind": "positive", "doc": "d"}
                                      for q in ("p1", "p2", "p3")})
    base = {"docs": [_doc("d", {"p1": "hit", "p2": "hit", "p3": "hit"})],
            "totals": {"violated": 0}, "markers": {"covered_pages": 2}}
    # 这次 p3 变 flaky（不参与比较），p1/p2 仍然 hit
    cur = {"docs": [_doc("d", {"p1": "hit", "p2": "hit", "p3": "miss"}, flaky=["p3"])],
           "totals": {"violated": 0}, "markers": {"covered_pages": 2}}
    assert rg.gate(cur, base) == []


def test_gate_reports_which_question_dropped(monkeypatch):
    monkeypatch.setattr(rg, "KINDS", {"p1": {"kind": "positive", "doc": "d"},
                                      "p2": {"kind": "positive", "doc": "d"}})
    base = {"docs": [_doc("d", {"p1": "hit", "p2": "hit"})],
            "totals": {"violated": 0}, "markers": {"covered_pages": 2}}
    cur = {"docs": [_doc("d", {"p1": "miss", "p2": "hit"})],
           "totals": {"violated": 0}, "markers": {"covered_pages": 2}}
    reasons = rg.gate(cur, base)
    assert len(reasons) == 1 and "p1" in reasons[0]


def test_gate_allows_pre_existing_violation(monkeypatch):
    """基线自带 1 处已知违规：不增加就不算退化，增加了才红。"""
    monkeypatch.setattr(rg, "KINDS", _kinds("n1", kind="negative"))
    cur = {"docs": [_doc("d", {"n1": "violated"})],
           "totals": {"violated": 1}, "markers": {"covered_pages": 2}}
    base = {"docs": [_doc("d", {"n1": "violated"})],
            "totals": {"violated": 1}, "markers": {"covered_pages": 2}}
    assert rg.gate(cur, base) == []


def test_gate_without_baseline_passes():
    cur = {"docs": [], "totals": {"violated": 0}, "markers": {"covered_pages": 0}}
    assert rg.gate(cur, None) == []
