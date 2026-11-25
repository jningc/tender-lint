"""章节切分：一章多类型、同类型多章（实测两套政府采购标准文本的变体）。"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from tender.sections import (  # noqa: E402
    classify_title, classify_title_all, required_types_present, split_sections, types_of,
)


def _page(n, title):
    return {"page": n, "height": 842.0, "source": "x.pdf", "text": title,
            "blocks": [{"text": title, "bbox": [50, 70, 540, 90], "max_size": 12.0}]}


def test_merged_chapter_has_two_types():
    """北京标准文本：「资格审查与评标办法」是一章，两个类型都要有。"""
    assert classify_title_all("第三章 资格审查与评标办法") == ["score", "qualify"]
    assert classify_title("第三章 资格审查与评标办法") == "score"   # 主类型


def test_merged_chapter_feeds_both_types():
    pages = [_page(1, "第一章 投标邀请"), _page(5, "第二章 投标人须知"),
             _page(20, "第三章 资格审查与评标办法"), _page(40, "第四章 合同条款及格式")]
    secs = split_sections(pages)
    merged = next(s for s in secs if "资格审查" in s["title"])
    assert merged["types"] == ["score", "qualify"]
    assert types_of(merged) == ["score", "qualify"]
    # 废标检索要的 qualify 不缺了
    assert required_types_present(secs, ["invite", "notice", "qualify"]) == []


def test_same_type_appears_twice():
    """兵团 2026：两套评标办法都编成「第五章」。"""
    pages = [_page(1, "第一章 招标公告"), _page(10, "第二章 投标人须知"),
             _page(30, "第五章 评标方法及标准(综合评分法)"),
             _page(40, "第五章 评标方法及标准（最低评标价法）"),
             _page(50, "第六章 合同草案")]
    secs = split_sections(pages)
    scores = [s for s in secs if s["type"] == "score"]
    assert len(scores) == 2
    assert scores[0]["start_page"] == 30 and scores[0]["end_page"] == 39
    assert scores[1]["start_page"] == 40


def test_sections_are_contiguous():
    """章节按物理顺序首尾相接，不留空洞也不重叠。"""
    pages = [_page(1, "第一章 招标公告"), _page(5, "第二章 投标人须知"),
             _page(20, "第三章 采购需求"), _page(30, "第四章 评标办法")]
    secs = split_sections(pages)
    for prev, nxt in zip(secs, secs[1:]):
        assert nxt["start_page"] == prev["end_page"] + 1


def test_old_section_without_types_still_works():
    """旧数据只有 type 字段，也要能算类型集合。"""
    assert types_of({"type": "score"}) == ["score"]
    assert types_of(None) == []
