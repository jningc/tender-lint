"""术语表：同义归一等、检索词扩展、互斥对。

三条规矩：

1. **只认 `aliases`，不认 `weak`**。`weak` 是泛词（「保证金」会命中履约保证金），
   只能拿去扩检索，不能拿来改写原文做判定。
2. **长词优先替换**。`投标保证金凭证` 必须整体匹配，否则会被 `投标保证金`
   截断成 `投标保证金凭证` → 两步替换后语义漂移。
3. **`pending` 不生效**。那是「没在三份样本里出现过、等扩样本再启用」的词，
   留在表里但不参与匹配——术语表里每个生效词都必须有真实出处。

词表本身在 `ontology/terms.yaml`，由 `scripts/terms_probe.py` 的实测数据生成。
"""

from __future__ import annotations

from pathlib import Path

import yaml

TERMS_PATH = Path(__file__).resolve().parent.parent / "ontology" / "terms.yaml"

_cache: dict | None = None
_pairs: list[tuple[str, str]] | None = None


def load_terms(path=None) -> dict:
    """读词表：{标准词: {table, field, aliases:[{word,source}], weak, pending, ...}}。"""
    global _cache, _pairs
    if path is not None:
        spec = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        _cache, _pairs = spec.get("terms") or {}, None
        return _cache or {}
    if _cache is None:
        spec = yaml.safe_load(TERMS_PATH.read_text(encoding="utf-8")) or {}
        _cache = spec.get("terms") or {}
    return _cache or {}


def _alias_pairs() -> list[tuple[str, str]]:
    """(别名, 标准词)，按别名长度降序——长词先替换。"""
    global _pairs
    if _pairs is None:
        pairs = []
        for standard, body in load_terms().items():
            for item in body.get("aliases") or []:
                word = (item.get("word") or "").strip()
                if word and word != standard:
                    pairs.append((word, standard))
        pairs.sort(key=lambda p: -len(p[0]))
        _pairs = pairs
    return _pairs


def normalize(text: str) -> str:
    """把别名换成标准词，供比对用。`weak` 词不动。"""
    if not text:
        return ""
    for alias, standard in _alias_pairs():
        if alias in text:
            text = text.replace(alias, standard)
    return text


def expand_query(query: str) -> str:
    """检索词扩展：query 里出现的标准词，补上它的别名（含 weak 泛词）。

    只扩召回，不改判定——返回的是给 BM25/向量用的词串。
    """
    if not query:
        return ""
    terms = load_terms()
    extra = []
    for standard, body in terms.items():
        if standard not in query:
            continue
        extra += [item["word"] for item in body.get("aliases") or []]
        extra += list(body.get("weak") or [])
    return " ".join(dict.fromkeys([query, *extra]))


def weak_words() -> list[str]:
    return [w for body in load_terms().values() for w in (body.get("weak") or [])]


def exclusive_pairs() -> list[tuple[str, str]]:
    """互斥对：出现 A 就不能判成 B（投标保证金 ≠ 履约保证金）。"""
    pairs = []
    for standard, body in load_terms().items():
        for other in body.get("exclusive_with") or []:
            if (other, standard) not in pairs:
                pairs.append((standard, other))
    return pairs


def standard_of(word: str) -> str | None:
    """某个词属于哪个标准概念（先精确、再别名）。"""
    terms = load_terms()
    if word in terms:
        return word
    for alias, standard in _alias_pairs():
        if alias == word:
            return standard
    return None
