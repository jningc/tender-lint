"""表格结构：评分表判定、跨页合并、行清洗。不调模型。"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from tender.tabular import (  # noqa: E402
    EDGE_BOTTOM, EDGE_TOP, Table, TableRow, _can_merge, _looks_pingfen,
)

H = 842.0


def _raw(page, y0, y1, cols=4, x0=61.0, x1=534.0, rows=None):
    cells = rows or [["序号", "评分因素", "评分标准", "分值"]]
    return {
        "page": page,
        "bbox": [x0, y0, x1, y1],
        "rows": [{"cells": c, "page": page, "bbox": [x0, y0, x1, y0 + 20.0]}
                 for c in cells],
        "cols": cols,
        "page_height": H,
    }


def test_pingfen_header_detected():
    assert _looks_pingfen(["序号", "评分因素", "评分标准", "分值"])
    assert _looks_pingfen(["序号", "评审因素", "评标标准"])
    assert not _looks_pingfen(["序号", "设备名称", "技术参数"])


def test_merge_when_bottom_and_top_align():
    upper = _raw(46, 544, 771)
    lower = _raw(47, 57, 779)
    assert _can_merge(upper, lower) is True


def test_no_merge_when_middle_of_page():
    upper = _raw(46, 200, 400)      # 不贴页底
    lower = _raw(47, 57, 779)
    assert _can_merge(upper, lower) is False


def test_no_merge_when_column_mismatch():
    upper = _raw(46, 544, 771, cols=4)
    lower = _raw(47, 57, 779, cols=6)
    assert _can_merge(upper, lower) is False


def test_no_merge_when_x_shifted():
    upper = _raw(46, 544, 771, x0=61.0)
    lower = _raw(47, 57, 779, x0=120.0)
    assert _can_merge(upper, lower) is False


def test_no_merge_across_gap_page():
    upper = _raw(46, 544, 771)
    lower = _raw(48, 57, 779)        # 隔了一页
    assert _can_merge(upper, lower) is False


def test_clean_rows_drops_empty_and_repeated_header():
    table = Table(
        id="d:t000", kind="pingfen",
        header=["序号", "评分因素", "分值"],
        rows=[
            TableRow(cells=["", "", ""], page=1, bbox=[0, 0, 1, 1]),
            TableRow(cells=["序号", "评分因素", "分值"], page=1, bbox=[0, 0, 1, 1]),
            TableRow(cells=["1", "拟投入项目\n经理情况", "2"], page=1, bbox=[0, 0, 1, 1]),
        ],
    )
    assert table.clean_rows() == [["1", "拟投入项目经理情况", "2"]]


def test_edge_thresholds():
    assert EDGE_TOP == 0.15 and EDGE_BOTTOM == 0.85


def test_row_keeps_its_own_page():
    """跨页合并后，行级页码不能都抄首页。"""
    from tender.tabular import extract_tables  # 真实 PDF，跑一次即可

    tables = extract_tables(Path(PROJECT_ROOT) / "sample" / "chongzuo_green.pdf")
    merged = [t for t in tables if t.kind == "pingfen" and t.continued]
    assert merged, "chongzuo 的评分表应该被合并"
    pages = {r.page for r in merged[0].rows}
    assert pages == {46, 47, 48}, f"行级页码应为 46/47/48，实测 {pages}"
    assert all(r.bbox and len(r.bbox) == 4 for r in merged[0].rows)
