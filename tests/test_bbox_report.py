"""bbox 口径统计的两档新指标：条款级覆盖、评分表行级 bbox。"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from eval.bbox_report import (  # noqa: E402
    _contains, covered_by, row_level_stats,
)


def test_contains_with_tolerance():
    """边缘差一两像素也算包含：ref 和条款的框来自不同抽取层。"""
    outer = [10.0, 10.0, 100.0, 50.0]
    assert _contains(outer, [12.0, 12.0, 98.0, 48.0])
    assert _contains(outer, [10.5, 10.5, 99.5, 49.5])      # 容差内
    assert not _contains(outer, [5.0, 12.0, 98.0, 48.0])   # 左边超出


def test_covered_by_uses_geometry_only():
    """带 `clause` 字段不算命中——那字段有就必然 100%，量不出框指没指到条款里。"""
    ref = {"page": 33, "bbox": [20, 20, 60, 40], "clause": "12.1"}
    boxes = {33: [[10, 10, 100, 60]]}
    assert covered_by(ref, boxes) is True
    # 几何上不在任何条款框里 → False，即使有 clause 字段
    assert covered_by(ref, {33: [[200, 200, 300, 300]]}) is False


def test_covered_by_missing_page():
    ref = {"page": 99, "bbox": [20, 20, 60, 40]}
    assert covered_by(ref, {33: [[10, 10, 100, 60]]}) is False


def test_row_level_stats_beijing():
    """北京三张评分表共 12 行，每行都该有自己的框。"""
    stats = row_level_stats("beijing_road")
    assert stats["rows"] == 12
    assert stats["with_bbox"] == 12
    assert stats["max_h_ratio"] < 0.6      # 行级框不能是整页框


def test_row_level_stats_missing_doc():
    assert row_level_stats("不存在的文档")["rows"] == 0
