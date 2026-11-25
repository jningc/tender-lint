"""版面线索：字号 / 粗体 / 页眉页脚打标。不调模型，不依赖 sample。"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from tender.parse import _repeat_key, mark_edge_repeats, parse_pdf  # noqa: E402

H = 800.0
TOP = [50.0, 8.0, 500.0, 20.0]        # 页面上边缘带（y1=20 < 800*0.15）
BODY = [50.0, 300.0, 500.0, 340.0]
FOOT = [50.0, 760.0, 500.0, 772.0]    # 下边缘带（y0=760 > 800*0.85=680）


def _page(n, blocks):
    return {"page": n, "width": 595.0, "height": H, "text": "", "blocks": blocks}


def _strip(n, text="", pos=TOP, size=12.0):
    return {"text": text, "bbox": list(pos), "max_size": size}


def test_repeat_key_erases_page_numbers():
    assert _repeat_key("崇左市政府采购公开招标采购文件 第105页") == _repeat_key("崇左市政府采购公开招标采购文件 第1页")
    assert _repeat_key("崇左市政府采购公开招标采购文件 第1页") != _repeat_key("北京市道路工程监理招标文件 第1页")


def test_header_marked_across_pages():
    pages = [_page(i, [_strip(i, f"崇左市政府采购公开招标采购文件 第{i}页"),
                       _strip(i, f"正文第{i}段", BODY)]) for i in range(1, 11)]
    out = mark_edge_repeats(pages)
    assert all(p["blocks"][0].get("is_header") for p in out)
    assert not any(p["blocks"][1].get("is_header") for p in out)


def test_footer_marked():
    pages = [_page(i, [_strip(i, f"第{i}页 共10页", FOOT)]) for i in range(1, 11)]
    out = mark_edge_repeats(pages)
    assert all(p["blocks"][0].get("is_footer") for p in out)
    assert not any(p["blocks"][0].get("is_header") for p in out)


def test_unique_text_not_marked():
    """每页不同的章标题（只差章号也不行）：抹数字后文字不同，不算重复。"""
    titles = ["项目概况", "投标人须知", "评标办法", "合同条款", "采购需求",
              "资格审查", "投标文件格式", "附件目录", "评分细则", "其他事项"]
    pages = [_page(i, [_strip(i, f"第{i}章 {titles[i - 1]}", TOP),
                       _strip(i, "正文内容" * 10, BODY)]) for i in range(1, 11)]
    out = mark_edge_repeats(pages)
    assert not any(b.get("is_header") for p in out for b in p["blocks"])


def test_big_font_repeated_block_not_marked():
    """每页重复的大字号块是章标题，不是页眉——字号判据要挡住它。"""
    pages = [_page(i, [_strip(i, "某某项目招标文件", TOP, size=18.0),
                       _strip(i, "正文内容" * 20, BODY, size=12.0)]) for i in range(1, 11)]
    out = mark_edge_repeats(pages)
    assert not any(b.get("is_header") for p in out for b in p["blocks"])


def test_small_font_repeated_block_marked():
    """同一份文档里字号是正文号的重复块才算页眉。"""
    pages = [_page(i, [_strip(i, f"某某项目招标文件 第{i}页", TOP, size=9.0),
                       _strip(i, "正文内容" * 20, BODY, size=12.0)]) for i in range(1, 11)]
    out = mark_edge_repeats(pages)
    assert all(p["blocks"][0].get("is_header") for p in out)


def test_multiline_and_long_blocks_skipped():
    """超过两行、或任意一行过长，就不是页眉。"""
    three_lines = [_strip(i, "第一行\n第二行\n第三行", TOP) for i in range(1, 11)]
    long_text = [_strip(i, "重复" * 30, TOP) for i in range(1, 11)]
    assert not any(b.get("is_header") for p in mark_edge_repeats([_page(i, [three_lines[i - 1]]) for i in range(1, 11)]) for b in p["blocks"])
    assert not any(b.get("is_header") for p in mark_edge_repeats([_page(i, [long_text[i - 1]]) for i in range(1, 11)]) for b in p["blocks"])


def test_two_line_header_marked():
    """实测温岭的页眉就是一个块里两行：单位名 + 页码。"""
    pages = [_page(i, [_strip(i, f"温岭市政府采购招标文件（电子招投标）\n第{i} 页，共10 页", TOP, size=9.0),
                       _strip(i, "正文内容" * 20, BODY, size=11.1)]) for i in range(1, 11)]
    out = mark_edge_repeats(pages)
    assert all(p["blocks"][0].get("is_header") for p in out)


def test_body_block_not_marked_even_if_repeated():
    """正文里重复的句子（如「详见投标人须知」）不该被当页眉。"""
    pages = [_page(i, [_strip(i, "详见投标人须知前附表", BODY)]) for i in range(1, 11)]
    assert not any(b.get("is_header") for p in mark_edge_repeats(pages) for b in p["blocks"])


def test_parse_pdf_records_size_and_spans(tmp_path):
    """字号必须留下来——它是条款层级判定唯一的排版依据。"""
    import pymupdf

    doc = pymupdf.open()
    for _ in range(3):
        page = doc.new_page()
        page.insert_text((72, 90), "HEADING", fontsize=18, fontname="hebo")
        page.insert_text((72, 130), "body text here", fontsize=10, fontname="helv")
    pdf = tmp_path / "probe.pdf"
    doc.save(pdf)
    doc.close()

    pages = parse_pdf(pdf)
    sizes = sorted({b["max_size"] for p in pages for b in p["blocks"]})
    assert 18.0 in sizes and 10.0 in sizes
    assert all(b["spans_count"] >= 1 for p in pages for b in p["blocks"])
    assert all(0.0 <= b["bold_ratio"] <= 1.0 for p in pages for b in p["blocks"])
