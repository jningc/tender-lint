"""bbox 条款级：块级框挑选、段内存块、索引版本失效。不调模型。"""

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from tender.index import chunk_pages, pick_block  # noqa: E402
from tender.stages.parse_stage import index_cache_exists  # noqa: E402

TALL = [47.0, 60.0, 548.0, 800.0]     # 740pt 高，超过 A4 的 60%
LINE = [47.0, 300.0, 548.0, 320.0]    # 20pt，一行


def test_pick_block_takes_best_overlap():
    blocks = [
        {"text": "投标保证金金额：01 包 8000 元", "bbox": LINE},
        {"text": "投标有效期自提交截止之日起算90 日历天", "bbox": [47.0, 340.0, 548.0, 360.0]},
    ]
    picked = pick_block("投标保证金 金额 8000", blocks)
    assert picked["bbox"] == LINE and picked["kind"] == "block"


def test_pick_block_prefers_shorter_on_tie():
    blocks = [
        {"text": "质保金", "bbox": TALL},
        {"text": "质保金", "bbox": LINE},
    ]
    assert pick_block("质保金", blocks)["bbox"] == LINE


def test_pick_block_marks_page_tall():
    assert pick_block("任何", [{"text": "整页表格", "bbox": TALL}])["kind"] == "page"


def test_pick_block_without_blocks():
    assert pick_block("随便", []) is None


def _page(n, blocks, height=842.0):
    return {"page": n, "height": height, "source": "x.pdf", "blocks": blocks}


SECTIONS = [{"type": "notice", "title": "第二章 投标人须知", "start_page": 1, "end_page": 5}]


def test_chunks_keep_blocks_and_page_height():
    pages = [_page(1, [{"text": "第一条 " + "甲" * 10, "bbox": LINE},
                       {"text": "第二条 " + "乙" * 10, "bbox": [47.0, 330.0, 548.0, 350.0]},
                       {"text": "第三条 " + "丙" * 400, "bbox": [47.0, 360.0, 548.0, 780.0]}],
               height=1200.0)]
    chunks = chunk_pages(pages, SECTIONS, target_len=100)
    assert all(c["page_height"] == 1200.0 for c in chunks)
    assert all(c["blocks"] for c in chunks)
    # 第一段装下前两条：段并集是两条的框，块仍是各自的窄框
    assert len(chunks[0]["blocks"]) == 2
    assert chunks[0]["blocks"][0]["bbox"] == LINE
    assert chunks[0]["bbox"] == [47.0, 300.0, 548.0, 350.0]
    # 第三条太长单独成段
    assert len(chunks[1]["blocks"]) == 1 and chunks[1]["bbox"] == [47.0, 360.0, 548.0, 780.0]


def test_chunks_never_span_pages():
    pages = [_page(1, [{"text": "甲" * 300, "bbox": LINE}]),
             _page(2, [{"text": "乙" * 300, "bbox": LINE}])]
    chunks = chunk_pages(pages, SECTIONS, target_len=380)
    assert sorted({c["page"] for c in chunks}) == [1, 2]


def test_index_cache_version_gate(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    manifest = tmp_path / "data" / "tender_index" / "doc" / "manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"sections": [], "types": []}), encoding="utf-8")
    assert index_cache_exists("sample/doc.pdf") is False      # 老缓存没版本号 → 必须重建
    manifest.write_text(json.dumps({"index_version": 2}), encoding="utf-8")
    assert index_cache_exists("sample/doc.pdf") is True
