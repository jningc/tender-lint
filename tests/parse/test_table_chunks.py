"""表格 → 可检索文本（small chunk）：字段绑定、表级事实、去重、召回。

核心断言是「不改写原文」和「不发明原文没有的数字」这两条，
它们是这套方案区别于「让 LLM 重新写一遍」的地方。
"""

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from tender.tabular import extract_tables  # noqa: E402
from tender.table_chunks import (  # noqa: E402
    TableIndex, build_chunks, small_chunk, table_fact,
)

SAMPLE = PROJECT_ROOT / "sample"


@pytest.fixture(scope="module")
def tables():
    out = []
    for doc in ("beijing_road", "chongzuo_green", "wenling_video"):
        out += [t for t in extract_tables(SAMPLE / f"{doc}.pdf", stem=doc)
                if t.kind == "pingfen" and t.rows]
    return {t.id: t for t in out}


def test_chunk_has_field_names_and_raw_text(tables):
    """字段名要和值绑在一起，原文一个字不改。"""
    t = tables["beijing_road:p33:2"]
    text = small_chunk(t, t.rows[1])
    assert "评审因素：总监理工程师类似业绩" in text
    assert "分项：3分" in text
    # 「（含）」这种边界词必须原样保留——LLM 改写会吃掉它
    assert "1个以上（含）3分" in text


def test_chunk_carries_table_fact(tables):
    """每一行都带表级事实，否则「商务部分一共多少分」答不出。"""
    t = tables["beijing_road:p33:2"]
    for row in t.rows:
        assert "本表合计10分（3项）" in small_chunk(t, row)


def test_fact_prefers_declared_total(tables):
    """原文写了合计就引用，不标「推定」。"""
    assert table_fact(tables["beijing_road:p33:2"]) == "本表合计10分（3项）"
    # wenling 的合计写成一行「总分 50」，表头不叫「合计」也要认
    assert table_fact(tables["wenling_video:p19:1"]) == "本表合计50分（12项）"


def test_fact_estimated_only_when_every_item_found(tables):
    """累加只在「项数 = 行数」时输出，否则宁可空着。

    chongzuo 9 行只抓到 8 项（实施方案那行没写分值、业绩分被跨页拆成两行），
    累加会得到 100 分，而实际是 80 分——错数字比没数字更糟。
    """
    assert table_fact(tables["chongzuo_green:p46:1"]) == ""
    fact = table_fact(tables["beijing_road:p33:3"])
    assert "推定" in fact and "60" in fact


def test_duplicate_header_becomes_category(tables):
    """chongzuo 表头是两个「序号」，第二列其实是分类，要去重成「分类」。"""
    t = tables["chongzuo_green:p46:1"]
    text = small_chunk(t, t.rows[6])
    assert "分类：商务分（满分20分）" in text


def test_row_level_recall():
    """20 条查询，行级召回要 ≥ 18/20。"""
    import json
    cases = json.loads((PROJECT_ROOT / "eval" / "metrics" / "table_queries.json")
                       .read_text(encoding="utf-8"))["queries"]
    chunks = build_chunks(_all_tables())
    index = TableIndex(chunks)
    hit = 0
    for case in cases:
        hits = index.search(case["q"], k=5)
        ok = any(h["table_id"] == case["expect_table"]
                 and (case.get("expect_row") is None or h["row_index"] == case["expect_row"])
                 for h in hits)
        hit += int(ok)
    assert hit >= 18, f"行级召回 {hit}/{len(cases)}"


def _all_tables():
    out = []
    for doc in ("beijing_road", "chongzuo_green", "wenling_video"):
        out += [t for t in extract_tables(SAMPLE / f"{doc}.pdf", stem=doc)
                if t.kind == "pingfen" and t.rows]
    return out
