"""前附表直读：识别判据、行抽取、查找、存取。

这块是纯代码（零模型），所以每条判据都能锁死。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from tender.frontmatter import FrontItem, FrontMatter, extract, from_table, is_frontmatter


def _table(header, rows, pages=(23,), col_count=3, tid="doc:p23:1"):
    """构造一张假表，形状与 tabular.Table 一致。

    `rows` 的每项可以是纯 cells 列表（页码取 pages[0]），也可以是 (cells, page)。
    """
    trs = []
    for r in rows:
        cells, pg = (r, pages[0]) if isinstance(r, list) else r
        trs.append(SimpleNamespace(cells=cells, page=pg, bbox=[0, 0, 1, 1]))
    return SimpleNamespace(
        id=tid, pages=list(pages), col_count=col_count,
        header=list(header), header_levels=[list(header)],
        rows=trs, bbox=[0, 0, 1, 1],
    )


# ---------- 识别判据 ----------

def test_recognizes_standard_header():
    t = _table(["条款号", "项目内容", "编列内容☑"],
               [[f"{i}.1", "某项目", "某值"] for i in range(6)])
    assert is_frontmatter(t, "notice") is True


def test_recognizes_alt_header():
    """温岭用「序号 | 项目 | 内容」，也要认。"""
    t = _table(["序号", "项目", "内容"],
               [[str(i), "某项目", "某值"] for i in range(6)])
    assert is_frontmatter(t, "notice") is True


def test_rejects_two_column_table():
    t = _table(["项目", "内容"], [[str(i), "值"] for i in range(8)], col_count=2)
    assert is_frontmatter(t, "notice") is False


def test_rejects_too_few_rows():
    t = _table(["条款号", "项目内容", "编列内容"], [["1", "a", "b"]])
    assert is_frontmatter(t, "notice") is False


def test_rejects_wrong_header():
    """「序号 | 审查因素 | 审查内容」是资格审查表，不是前附表。"""
    t = _table(["序号", "审查因素", "审查内容"],
               [[str(i), "a", "b"] for i in range(10)])
    assert is_frontmatter(t, "notice") is False


def test_rejects_when_section_type_not_notice():
    t = _table(["条款号", "项目内容", "编列内容"],
               [[str(i), "a", "b"] for i in range(10)])
    assert is_frontmatter(t, "score") is False
    assert is_frontmatter(t, None) is True      # 章类型未知时放宽


def test_long_header_is_not_matched():
    """表头太长说明是正文被当成表头了，不能认。"""
    t = _table(["序号", "投标人应当提供下列全部证明材料并加盖公章", "内容"],
               [[str(i), "a", "b"] for i in range(10)])
    assert is_frontmatter(t, "notice") is False


def test_to_chunks_one_per_item_with_table_fact():
    """前附表每个条目一条 chunk，且每条都带表级事实（总条数）。

    前附表被排除出条款树后，内容只能靠这条路径进索引；
    北京「01包：8000元」只在这个表里出现，少一条就问不出金额。
    """
    from tender.frontmatter import FrontItem, FrontMatter, to_chunks

    fm = FrontMatter(table_id="x:p7:1", pages=[7], items=[
        FrontItem(no="12.1", name="投标保证金", value="01包：8000元", page=8),
        FrontItem(no="12.2", name="交纳方式", value="电汇或保函", page=8),
    ])
    chunks = to_chunks([fm], sections=[{"type": "notice", "title": "第二章 投标人须知",
                                        "start_page": 7, "end_page": 22}],
                       source="x.pdf", heights={8: 842.0})
    assert len(chunks) == 2
    assert "8000" in chunks[0]["text"]
    assert chunks[0]["kind"] == "frontmatter"
    assert chunks[0]["page"] == 8
    assert chunks[0]["section_type"] == "notice"
    assert "2 条" in chunks[0]["context"]


# ---------- 行抽取 ----------

def test_from_table_basic():
    t = _table(["条款号", "项目", "内容"], [
        ["17.2", "投标有效期", "自投标截止之日起90日。"],
        ["18", "投标保证金金额", "本项目不收取投标保证金。"],
    ])
    fm = from_table(t)
    assert len(fm) == 2
    assert fm.first("投标有效期").value == "自投标截止之日起90日。"
    assert fm.first("投标有效期").page == 23


def test_merged_cell_inherits_clause_no():
    """崇左有 13.1 跨四行（资格/商务/技术/报价文件组成），后三行的条款号是空的。"""
    t = _table(["条款号", "项目", "内容"], [
        ["13.1", "资格证明文件组成", "1、承诺函"],
        ["", "商务文件组成", "1、无串通承诺函"],
        ["", "技术文件组成", "1、偏离表"],
    ])
    fm = from_table(t)
    assert [it.no for it in fm.items] == ["13.1", "13.1", "13.1"]
    assert [it.name for it in fm.items] == ["资格证明文件组成", "商务文件组成", "技术文件组成"]


def test_four_column_value_is_joined():
    """4 列表：值列被拆成两列时要拼回去，不丢字。"""
    t = _table(["条款号", "项目", "内容", "备注"], [
        ["1", "投标有效期", "90", "日历天"],
    ], col_count=4)
    fm = from_table(t)
    assert fm.items[0].value == "90 日历天"


def test_row_without_name_is_skipped():
    t = _table(["条款号", "项目", "内容"], [
        ["1", "投标有效期", "90日"],
        ["2", "", "孤立的值"],
    ])
    fm = from_table(t)
    assert len(fm) == 1


# ---------- 查找 ----------

def test_find_requires_all_keywords():
    t = _table(["条款号", "项目", "内容"], [
        ["1", "投标保证金金额", "8000 元"],
        ["2", "投标有效期", "90 日"],
    ])
    fm = from_table(t)
    assert fm.first("投标保证金", "金额").value == "8000 元"
    assert fm.first("投标保证金", "有效期") is None


def test_to_dict_handles_duplicate_names():
    t = _table(["条款号", "项目", "内容"], [
        ["13.1", "文件组成", "a"],
        ["13.1", "文件组成", "b"],
    ])
    d = from_table(t).to_dict()
    assert len(d) == 2          # 重名用条款号加后缀，不丢行


# ---------- 存取 ----------

def test_dump_load_roundtrip(tmp_path):
    fm = FrontMatter(table_id="x:p23:1", pages=[23], items=[
        FrontItem(no="17.2", name="投标有效期", value="90日", page=23, bbox=[1, 2, 3, 4]),
    ])
    from tender import frontmatter as F
    F.dump([fm], tmp_path, "doc")
    back = F.load(tmp_path, "doc")
    assert len(back) == 1
    assert back[0].items[0].name == "投标有效期"
    assert back[0].items[0].bbox == [1, 2, 3, 4]


def test_load_missing_returns_empty(tmp_path):
    from tender import frontmatter as F
    assert F.load(tmp_path, "nope") == []


# ---------- 批量识别 ----------

def test_extract_filters_by_section_type():
    good = _table(["条款号", "项目内容", "编列内容"],
                  [[str(i), "a", "b"] for i in range(8)], pages=(23,))
    bad = _table(["序号", "审查因素", "审查内容"],
                 [[str(i), "a", "b"] for i in range(8)], pages=(26,), tid="t26")
    out = extract([good, bad], {23: "notice", 26: "score"})
    assert len(out) == 1
    assert out[0].table_id == "doc:p23:1"
