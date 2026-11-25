"""合并单元格填充：None 继承上一行，空字符串不继承。"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from tender.tabular import _fill_merged  # noqa: E402


def _rows(*cells_list):
    return [{"cells": list(c), "page": 1, "bbox": [0, 0, 1, 1]} for c in cells_list]


def test_none_inherits_previous_row():
    """跨行合并：技术分只写在第一行，后面几行要继承。"""
    rows = _rows(
        ["序号", "分类", "评审因素"],
        ["1", "技术分（60分）", "实施方案"],
        ["2", None, "质量保证体系"],
        ["3", None, "施工总进度"],
    )
    _fill_merged(rows, 3)
    assert rows[2]["cells"][1] == "技术分（60分）"
    assert rows[3]["cells"][1] == "技术分（60分）"


def test_empty_string_does_not_inherit():
    """真空白（备注列没写）不能被上一行污染。"""
    rows = _rows(
        ["序号", "项目", "备注"],
        ["1", "甲", "重要"],
        ["2", "乙", ""],
        ["3", "丙", ""],
    )
    _fill_merged(rows, 3)
    assert rows[2]["cells"][2] == ""      # 第三行（乙）的备注是空
    assert rows[3]["cells"][2] == ""


def test_index_column_not_inherited():
    """序号列天然递增，不能继承上一行——否则 4 个评分项全变成「2」。"""
    rows = _rows(
        ["序号", "分类", "评审因素"],
        ["2", "技术分（60分）", "实施方案"],
        [None, None, "质量保证体系"],
        [None, None, "施工总进度"],
    )
    _fill_merged(rows, 3, header=["序号", "分类", "评审因素"])
    assert rows[1]["cells"][0] == "2"                     # 自带序号保留
    assert rows[2]["cells"][0] == ""                      # 序号不继承
    assert rows[3]["cells"][0] == ""
    assert rows[1]["cells"][1] == "技术分（60分）"          # 分类继承


def test_header_none_becomes_empty():
    """表头行没有上一行，None 只能变空。"""
    rows = _rows(["序号", None, "分值"])
    _fill_merged(rows, 3)
    assert rows[0]["cells"] == ["序号", "", "分值"]


def test_short_row_padded_to_col_count():
    rows = _rows(["1", "甲"], ["2", "乙"])
    _fill_merged(rows, 3)
    assert all(len(r["cells"]) == 3 for r in rows)


def test_real_table_merges_category():
    """chongzuo 的评分表：技术分跨多行，填充后每行都要有分类。"""
    from tender.tabular import extract_tables, pingfen_tables

    tables = pingfen_tables(extract_tables(Path(PROJECT_ROOT) / "sample" / "chongzuo_green.pdf"))
    assert tables
    rows = [r for r in tables[0].clean_rows() if any(r)]
    # 第 3 列是「评审因素」，第 2 列是分类；至少有一行以上继承了分类
    assert len(rows) >= 4
    filled = [r[1] for r in rows if len(r) > 1 and r[1]]
    assert any("技术分" in c for c in filled)
