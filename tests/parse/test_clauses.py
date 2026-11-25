"""条款树：档位探测、起点判定、跨页聚合。不调模型，不读 PDF。"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from tender.clauses import (  # noqa: E402
    build_clauses, detect_numbering, heading_family,
    pick_start_bands, stats,
)

BODY = 12.0
SECTIONS = [{"type": "notice", "title": "第二章 投标人须知", "start_page": 1, "end_page": 5}]
SCORE_SECTIONS = [{"type": "score", "title": "第三章 评标办法", "start_page": 1, "end_page": 5}]


def _block(text, y=100.0, size=BODY):
    return {"text": text, "bbox": [50.0, y, 540.0, y + 16.0], "max_size": size,
            "bold_ratio": 0.0, "spans_count": 1}


def _page(n, blocks, height=842.0):
    return {"page": n, "height": height, "width": 595.0, "source": "doc.pdf", "blocks": blocks}


# ---------- 档位探测 ----------

def test_detect_numbering_counts_bands():
    pages = [_page(1, [_block("1. 项目编号：X"), _block("1. 满足第二十二条规定", y=150),
                       _block("正文没有编号", y=200), _block("（1）投标文件提交方式", y=250)])]
    counts = detect_numbering(pages)
    assert counts["1."] == 2 and counts["（1）"] == 1 and counts["X.Y"] == 0


def test_pick_start_bands_includes_sparse_high_band():
    """「第X条」只 24 次（0.8%）也必须纳入——它是层级最高的一档。"""
    counts = {"第X条": 24, "X.Y": 220, "（一）": 30, "①": 2,
              "1.": 138, "一、": 30, "（1）": 76, "字号": 10}
    bands = pick_start_bands(counts, total_blocks=2025)
    assert "第X条" in bands and "X.Y" in bands
    assert "①" not in bands      # 2 次，既不够密也不够多


def test_pick_start_bands_falls_back_to_font():
    counts = {k: 0 for k in ("第X条", "X.Y", "（一）", "①", "1.", "一、", "（1）", "字号")}
    assert pick_start_bands(counts, total_blocks=500) == ["字号"]


# ---------- 正则收紧 ----------

def test_amount_is_not_a_clause_number():
    """2263.76 是金额，不是 2263.76 条。"""
    assert heading_family(_block("2263.76 元"), ["X.Y"], BODY) is None
    assert heading_family(_block("12.1 投标保证金金额"), ["X.Y"], BODY) == ("decimal1", "12.1")


def test_decimal_is_not_a_section_number():
    """"1.5 倍" 不是第 1.5 条。"""
    assert heading_family(_block("1.5 倍价格"), ["1."], BODY) is None
    assert heading_family(_block("1.项目编号：ZRDX"), ["1."], BODY) == ("digit", "1.")


def test_clause_number_beats_dotted():
    hit = heading_family(_block("第一条 为规范政府采购行为"), ["第X条", "X.Y"], BODY)
    assert hit == ("article", "第一条")


def test_band_must_be_selected():
    """没被探进起点档的编号不算起点。"""
    assert heading_family(_block("12.1 投标保证金"), ["1."], BODY) is None


# ---------- （1） 按章节区分 ----------

def test_item_number_independent_in_score_section():
    hit = heading_family(_block("（1）价格分 30 分"), ["（1）"], BODY, section_type="score")
    assert hit == ("digit_paren", "（1）")


def test_item_number_is_child_elsewhere():
    assert heading_family(_block("（1）投标文件提交方式"), ["（1）"], BODY, section_type="notice") is None


# ---------- 聚合 ----------

def test_build_clauses_groups_and_spans_pages():
    pages = [
        _page(1, [_block("12.1 投标人应按资料表交纳保证金。", y=100),
                  _block("投标人自愿超额缴纳的，不做无效处理。", y=130),
                  _block("12.2 交纳形式：支票、汇票。", y=160)]),
        _page(2, [_block("银行支付等形式提交的，应在截止前到账。", y=100)]),
    ]
    nodes = build_clauses(pages, SECTIONS)
    assert [n.number for n in nodes] == ["12.1", "12.2"]
    first = nodes[0]
    assert first.page_start == 1 and first.page_end == 1 and not first.continued
    assert "不做无效处理" in first.text
    # 12.2 的块跨到第 2 页
    assert nodes[1].page_start == 1 and nodes[1].page_end == 2 and nodes[1].continued
    assert nodes[1].bbox[3] > 100.0


def test_bbox_is_union_of_clause():
    pages = [_page(1, [_block("12.1 第一行", y=100), _block("续行", y=130)])]
    node = build_clauses(pages, SECTIONS)[0]
    assert node.bbox == [50.0, 100.0, 540.0, 146.0]


def test_unstarted_blocks_marked_as_block():
    pages = [_page(1, [_block("封面：某某项目招标文件", y=50), _block("目录", y=80)])]
    nodes = build_clauses(pages, SECTIONS)
    assert len(nodes) == 1 and nodes[0].kind == "block" and nodes[0].band == ""


def test_table_blocks_are_skipped():
    pages = [_page(1, [_block("12.1 保证金", y=100),
                       _block("表格里的行 30 分", y=200)])]
    nodes = build_clauses(pages, SECTIONS, table_boxes={1: [[40.0, 190.0, 550.0, 260.0]]})
    assert "表格里的行" not in nodes[0].text


def test_stats_reports_bands():
    pages = [_page(1, [_block("12.1 甲", y=100), _block("12.2 乙", y=130),
                       _block("无编号续行", y=160)])]
    s = stats(build_clauses(pages, SECTIONS))
    assert s["total"] == 2 and s["clause"] == 2 and s["by_band"]["X.Y"] == 2


def test_high_band_stops_low_band_clause():
    """北京的真实错误：12.3 吞掉整章「第十三条 保密」。高层级档必须能截断。"""
    blocks = [_block(f"第{i}条 内容{i}", y=60 + i * 18) for i in range(1, 6)]
    blocks += [_block(f"12.{i} 联合体条款{i}", y=200 + i * 18) for i in range(1, 6)]
    blocks += [_block("第十三条 保密", y=320), _block("双方不得泄露保密资料。", y=350)]
    nodes = build_clauses([_page(1, blocks)], SECTIONS)
    numbers = [n.number for n in nodes]
    assert numbers[-1] == "第十三条"
    last_xy = next(n for n in nodes if n.number == "12.5")
    assert "不得泄露" not in last_xy.text
    assert "不得泄露" in nodes[-1].text


def test_long_clause_is_cut_not_demoted():
    """一条条款吃超过 60 块：断开并标 truncated，节点不降级消失。"""
    blocks = [_block("12.1 甲", y=60)]
    blocks += [_block(f"续行{i}", y=80 + i * 10) for i in range(70)]
    nodes = build_clauses([_page(1, blocks)], SECTIONS)
    assert all(n.blocks <= 60 for n in nodes)
    assert len(nodes) >= 2
    assert any(n.truncated for n in nodes)


# ---------- 父子关系（检索用条款，返回用父级） ----------

def test_parent_by_number_prefix():
    """5.1.1 / 5.1.2 的父是 5.1，不是「上一条」。"""
    blocks = [_block("5.1 甲", y=60), _block("5.1.1 乙", y=90), _block("5.1.2 丙", y=120)]
    nodes = build_clauses([_page(1, blocks)], SECTIONS)
    by_num = {n.number: n for n in nodes}
    assert by_num["5.1.1"].parent_id == by_num["5.1"].id
    assert by_num["5.1.2"].parent_id == by_num["5.1"].id
    assert by_num["5.1"].parent_id is None      # 没有更浅的编号，也无前序条款


def test_parent_falls_back_to_previous():
    """兄弟不是父子：12.1 / 12.2 并列，没有父标题就不许瞎猜。"""
    blocks = [_block("12.1 甲", y=60), _block("12.2 乙", y=90)]
    nodes = build_clauses([_page(1, blocks)], SECTIONS)
    assert nodes[1].parent_id is None


def test_heading_family_from_numbering():
    """族由编号形式给出；深度留给栈，这里只认「是不是标题 / 哪一族」。"""
    assert heading_family(_block("第二章 投标人须知"), ["X.Y"], BODY) == ("chapter", "")
    assert heading_family(_block("12 投标保证金"), ["X.Y"], BODY) == ("bare", "12")
    assert heading_family(_block("12.1 保证金金额"), ["X.Y"], BODY) == ("decimal1", "12.1")
    assert heading_family(_block("11.2.1 明细费用"), ["X.Y"], BODY) == ("decimal2", "11.2.1")
    assert heading_family(_block("普通正文没有编号"), ["X.Y"], BODY) is None


def test_bare_number_heading_is_parent():
    """`12 投标保证金` 没有点号，但它是 12.1/12.2 的父——早先版本把它当正文吞了。"""
    blocks = [_block("12 投标保证金", y=60), _block("12.1 甲", y=90), _block("12.2 乙", y=120)]
    nodes = build_clauses([_page(1, blocks)], SECTIONS)
    by_text = {n.text: n for n in nodes}
    head = by_text["12 投标保证金"]
    # depth 由栈推导：无章时第一个标题是顶层（0），不是旧 DEPTH_FIXED 的 1
    assert head.depth == 0
    assert by_text["12.1 甲"].parent_id == head.id
    assert by_text["12.2 乙"].parent_id == head.id
    assert head.parent_id is None


def test_chunk_context_follows_heading_chain():
    """返回给模型的是「父标题 + 本条」，不是兄弟全集。"""
    from tender.index import chunk_clauses

    blocks = [_block("12 投标保证金", y=60), _block("12.1 甲", y=90), _block("12.2 乙", y=120)]
    pages = [_page(1, blocks)]
    nodes = build_clauses(pages, SECTIONS)
    chunks = chunk_clauses(nodes, pages, SECTIONS)
    chunk = next(c for c in chunks if c["text"].startswith("12.1"))
    assert "12 投标保证金" in chunk["context"]      # 父标题在
    assert "12.2" not in chunk["context"]           # 兄弟不塞进来
    assert chunk["text"] == "12.1 甲"               # 证据仍是单条
