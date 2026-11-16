"""术语表：同义归一、检索扩展、互斥对，以及「不许凭空加词」的机械守卫。"""

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from tender.terms import (  # noqa: E402
    exclusive_pairs, expand_query, load_terms, normalize, standard_of, weak_words,
)


def test_alias_normalized_to_standard():
    assert normalize("磋商保证金") == "投标保证金"
    assert normalize("投标担保") == "投标保证金"


def test_exclusive_concept_not_crossed():
    """履约保证金不能被归一成投标保证金——这是最关键的一条防错。"""
    assert normalize("履约保证金") == "履约保证金"
    assert normalize("履约保函") == "履约保证金"


def test_longer_alias_wins():
    """「投标保证金凭证」整体替换，不被「投标保证金」截断成奇怪的结果。"""
    assert normalize("投标保证金凭证") == "投标保证金"


def test_weak_words_not_normalized():
    """泛词只扩召回，不改写原文。"""
    assert normalize("保证金缴纳方式") == "保证金缴纳方式"
    assert "保证金" in weak_words()


def test_expand_query_adds_aliases_and_weak():
    q = expand_query("投标保证金")
    assert "投标保证金" in q and "磋商保证金" in q and "保证金" in q
    # 不相关的概念不会被扯进来
    assert "付款方式" not in q


def test_exclusive_pairs_declared():
    pairs = exclusive_pairs()
    assert ("投标保证金", "履约保证金") in pairs


def test_standard_of():
    assert standard_of("磋商保证金") == "投标保证金"
    assert standard_of("投标保证金") == "投标保证金"
    assert standard_of("不存在的词") is None


def test_every_active_alias_exists_in_samples():
    """词表里每个生效别名必须在三份样本里真实出现过。

    这条守的是「不许凭想象加词」——手改 yaml 加了个没出现过的词，测试会红。
    """
    from tender.parse import parse_pdf

    corpus = ""
    for pdf in sorted((PROJECT_ROOT / "sample").glob("*.pdf")):
        corpus += "\n".join((p.get("text") or "") for p in parse_pdf(pdf))

    missing = []
    for concept, body in load_terms().items():
        for item in body.get("aliases") or []:
            word = item.get("word") or ""
            if word and word not in corpus:
                missing.append(f"{concept}/{word}")
    assert not missing, f"这些别名在样本里没出现过，应移到 pending：{missing}"


def test_every_alias_has_source():
    """每个别名都要记出处，否则无法复核。"""
    for concept, body in load_terms().items():
        for item in body.get("aliases") or []:
            assert item.get("source"), f"{concept}/{item.get('word')} 缺 source"
