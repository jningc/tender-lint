"""保证金事项卡：多源合并、三态、以及「投标保证金 ≠ 履约保证金」。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tender.matters import Field, MatterCard, _is_deposit, build_deposit_card, scan_clauses


def _chunk(text, page=17, typ="notice", cid="c1"):
    return {"text": text, "page": page, "bbox": [0, 0, 1, 1],
            "clause_id": cid, "section_type": typ}


def _index(**stores):
    """构造假索引：stores["notice"].chunks = [...]"""
    return SimpleNamespace(
        stores={k: SimpleNamespace(chunks=v) for k, v in stores.items()},
        sections=[], pdf_path="x.pdf", unit="clause", tables=[],
    )


def _front(name, value, page=25):
    fm = SimpleNamespace(items=[SimpleNamespace(no="18", name=name, value=value,
                                                page=page, bbox=[0, 0, 1, 1])])
    fm.first = lambda *kws: next(
        (it for it in fm.items if all(k in it.name for k in kws)), None)
    return fm


# ---------- 判据 ----------

def test_is_deposit_rejects_performance_bond():
    """这是历史上 bj-neg-perf-bond 违规的根因：履约保证金串进投标保证金。"""
    assert _is_deposit("投标保证金金额：8000 元") is True
    assert _is_deposit("履约保证金金额为中标金额的 5%") is False
    assert _is_deposit("质量保证金") is False


def test_is_deposit_allows_both_when_bid_mentioned():
    text = "投标保证金 8000 元；履约保证金为中标金额 5%"
    assert _is_deposit(text) is True


def test_exclusion_clause_not_consequence():
    """「…投标保证金除外」是排除条款，不是无效后果。"""
    idx = _index(notice=[_chunk("采购人拒绝接受平台以外任何形式提交的投标文件，投标保证金除外", 8)])
    hits, _ = scan_clauses(idx, ["notice"])
    assert hits == []


def test_consequence_clause_kept():
    idx = _index(notice=[_chunk("未按期交纳投标保证金的，其投标无效。", 17)])
    hits, _ = scan_clauses(idx, ["notice"])
    assert len(hits) == 1


# ---------- 半穷尽 ----------

def test_scan_reports_scanned_count():
    """扫描要报「扫过多少条」——这是可证明覆盖的依据。"""
    idx = _index(notice=[_chunk("a", 1), _chunk("b", 2),
                         _chunk("投标保证金未交纳，投标无效", 3)])
    hits, scanned = scan_clauses(idx, ["notice"])
    assert scanned["notice"] == 3
    assert len(hits) == 1


# ---------- 拼卡 ----------

def test_card_from_frontmatter_amount():
    idx = _index(notice=[_chunk("投标保证金未交纳的，投标无效", 17)])
    card = build_deposit_card(idx, [_front("投标保证金金额", "8000 元")])
    assert card.fields["amount"].value == "8000 元"
    assert card.fields["amount"].refs[0]["role"] == "前附表"
    assert card.fields["verdict"].status == "有值"


def test_no_charge_short_circuits_other_fields():
    """前附表写「不收取」时，其余字段是「原文未要求」，不是「待人工」。"""
    card = build_deposit_card(_index(notice=[]),
                              [_front("投标保证金金额", "本项目不收取投标保证金。")])
    for k in ("method", "verdict", "evidence"):
        assert card.fields[k].status == "原文未要求"


def test_coverage_counts_layers():
    card = MatterCard(name="x")
    card.fields["a"] = Field()
    card.fields["a"].add("v", 1, role="条款", layer="notice")
    card.fields["b"] = Field()
    card.fields["b"].add("v", 79, role="格式", layer="format")
    assert card.coverage() == "2/4"


def test_missing_field_stays_manual():
    card = build_deposit_card(_index(notice=[_chunk("无关内容", 1)]), [])
    assert card.fields["amount"].status == "待人工"
