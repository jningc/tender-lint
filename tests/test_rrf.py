"""RRF 融合：两路都靠前的候选要得加成，且能标出「来自哪一路」。

为什么值得单测：RRF 和「合并去重」在多数查询上结果相同，
只有在「A 路第 1、B 路第 5」与「两路都第 1」这类排序差别上才分得开——
这正是它存在的理由，必须锁住。
"""

from __future__ import annotations

import pytest

from tender import index as index_mod
from tender.index import RRF_K, SectionIndex


class _FakeBM25:
    def __init__(self, scores):
        self._scores = scores

    def get_scores(self, tokens):
        return self._scores


class _FakeDoc:
    def __init__(self, content, page):
        self.page_content = content
        self.metadata = {"page": page, "bbox": "1,2,3,4"}


class _FakeVS:
    def __init__(self, docs):
        self._docs = docs

    def similarity_search(self, query, k):
        return self._docs[:k]


def _chunk(text, page):
    return {
        "text": text,
        "page": page,
        "bbox": [1, 2, 3, 4],
        "blocks": [],
        "page_height": 842.0,
        "section_type": "notice",
        "section_title": "第二章 投标人须知",
        "kind": "clause",
        "clause_id": f"c{page}",
        "number": "",
        "context": "",
    }


def _make(chunks, bm25_scores, vec_docs):
    si = SectionIndex.__new__(SectionIndex)
    si.chunks = chunks
    si.bm25 = _FakeBM25(bm25_scores)
    si.vs = _FakeVS(vec_docs)
    return si


@pytest.fixture(autouse=True)
def _force_rrf(monkeypatch):
    monkeypatch.setattr(index_mod, "FUSION", "rrf")


def test_both_routes_wins():
    """B 在 BM25 排第 2、向量排第 1 → 融合后应超过只在 BM25 排第 1 的 A。

    旧的「合并去重」会把 A 排第一（BM25 优先），这就是两者行为差异所在。
    """
    chunks = [_chunk("aaa", 1), _chunk("bbb", 2), _chunk("ccc", 3)]
    si = _make(chunks, [3.0, 2.0, 1.0], [_FakeDoc("bbb", 2), _FakeDoc("ccc", 3)])
    hits = si.search("q", k=3)
    assert hits[0]["text"] == "bbb", [h["text"] for h in hits]


def test_recall_tags():
    """只在 BM25、只在向量、两路都在，要分别标出来。"""
    chunks = [_chunk("aaa", 1), _chunk("bbb", 2)]
    si = _make(chunks, [3.0, 2.0], [_FakeDoc("zzz", 9)])
    hits = si.search("q", k=2)
    tags = {h["text"]: h["recall"] for h in hits}
    assert tags["aaa"] == "bm25"
    assert tags["zzz"] == "vec"


def test_fusion_score_formula():
    """分数 = Σ 1/(RRF_K + rank + 1)，rank 从 0 起。"""
    chunks = [_chunk("aaa", 1)]
    si = _make(chunks, [5.0], [_FakeDoc("aaa", 1)])
    hit = si.search("q", k=1)[0]
    assert hit["recall"] == "bm25+vec"
    assert hit["score"] == pytest.approx(round(1 / (RRF_K + 1) + 1 / (RRF_K + 1), 4))


def test_dedup_same_chunk_from_both_routes():
    """同一 chunk 两路都命中只出一条，不是两条。"""
    chunks = [_chunk("aaa", 1)]
    si = _make(chunks, [5.0], [_FakeDoc("aaa", 1)])
    assert len(si.search("q", k=1)) == 1


def test_union_switch_keeps_old_order(monkeypatch):
    """TENDER_FUSION=union 时回到旧行为：BM25 优先。"""
    monkeypatch.setattr(index_mod, "FUSION", "union")
    chunks = [_chunk("aaa", 1), _chunk("bbb", 2)]
    si = _make(chunks, [3.0, 2.0], [_FakeDoc("bbb", 2), _FakeDoc("aaa", 1)])
    hits = si.search("q", k=2)
    assert hits[0]["text"] == "aaa"
