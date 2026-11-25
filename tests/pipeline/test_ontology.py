"""本体与管道的对齐守卫：consistency.py 有违规就红。

不需要 rdflib，只读两份 TTL 的固定写法；契约侧从 tender/contracts.py 取枚举。
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "ontology"))

import consistency  # noqa: E402


def test_ontology_matches_contracts():
    result = consistency.check()
    assert result["errors"] == [], "\n".join(result["errors"])


def test_no_warnings_either():
    """警告也不是白给的：bbox 没实例在用，就说明样例里没有条款级框。"""
    result = consistency.check()
    assert result["warns"] == [], "\n".join(result["warns"])


def test_counts_are_as_documented():
    model = consistency.check()["model"]
    assert len(model["classes"]) == 23
    assert len(model["oprops"]) == 12
    assert len(model["dprops"]) == 12
    assert len(model["instances"]) == 24


def test_verdict_kind_covers_contract():
    from tender.contracts import Verdict

    declared = set(consistency.check()["model"]["oneof"].get("VerdictKind", []))
    assert declared == set(getattr(Verdict, "__args__", ()))
