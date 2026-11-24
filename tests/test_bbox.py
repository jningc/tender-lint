"""索引结构：段不跨页、版本闸失效。

`pick_block()` 和 `chunk["blocks"]` 已于 2026-11-23 移除（窄框无消费者、UI 不画框），
对应的 4 条选块测试一并删除。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from tender.index import INDEX_VERSION, chunk_pages  # noqa: E402
from tender.stages.parse_stage import index_cache_exists  # noqa: E402


def _page(n, blocks):
    return {"page": n, "width": 595.0, "height": 842.0, "text": "", "blocks": blocks}


def _sec(t="notice"):
    return {"type": t, "title": "第二章 投标人须知", "start_page": 1, "end_page": 99}


def test_chunks_never_span_pages():
    """一段只能落在一页里——跨页会让页码和框都指向错的地方。"""
    pages = [
        _page(1, [{"text": "甲" * 300, "bbox": [0, 0, 10, 10]}]),
        _page(2, [{"text": "乙" * 300, "bbox": [0, 0, 10, 10]}]),
    ]
    chunks = chunk_pages(pages, [_sec()])
    assert {c["page"] for c in chunks} == {1, 2}
    for c in chunks:
        assert c["page"] in (1, 2)


def test_chunk_has_no_blocks_field():
    """blocks 字段已移除——它只为 pick_block 服务，而 pick_block 不再存在。"""
    chunks = chunk_pages([_page(1, [{"text": "内容", "bbox": [0, 0, 10, 10]}])], [_sec()])
    assert chunks and all("blocks" not in c for c in chunks)


def test_index_cache_version_gate(tmp_path, monkeypatch):
    """索引版本对不上时必须判失效，否则会读到结构不同的旧库。"""
    from tender.stages import parse_stage as ps

    # index_cache_exists 用相对路径 data/tender_index/<stem>/，所以切 cwd
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "data" / "tender_index" / "doc"
    root.mkdir(parents=True)
    old = {"index_version": INDEX_VERSION - 1, "chunk_unit": "clause", "types": [], "sections": []}
    (root / "manifest.json").write_text(json.dumps(old), encoding="utf-8")
    assert ps.index_cache_exists("doc.pdf", unit="clause") is False
    old["index_version"] = INDEX_VERSION
    (root / "manifest.json").write_text(json.dumps(old), encoding="utf-8")
    assert ps.index_cache_exists("doc.pdf", unit="clause") is True
