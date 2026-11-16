from __future__ import annotations

"""
招标 PDF 解析：按页抽文本和块坐标，并可渲成页图。

每页：
  page / width / height / text / blocks[{text, bbox}]
bbox 为 PDF 点坐标 [x0, y0, x1, y1]，原点在左上。

块上另记版面线索——**只加字段，不改切分与检索行为**：
  max_size      块内最大字号（条款层级判定要用，原先被丢掉）
  bold_ratio    加粗 span 占比
  spans_count   span 数
  is_header / is_footer  跨页重复的页眉页脚块

页眉页脚判据（三条件同时满足）：单行、长度 ≤ 40、落在页面上下 15% 边缘带，
且规范化后（数字抹成 #）在 ≥ 30% 的页里重复出现。
"""

import re
from collections import Counter
from pathlib import Path

import pymupdf as fitz

#: 页眉页脚所在边缘带（页高占比）
EDGE_BAND = 0.15
#: 跨多少比例的页重复才算页眉页脚
REPEAT_SHARE = 0.30
#: 页眉页脚文本长度上限
MAX_REPEAT_LEN = 40
#: 页眉页脚块的高度上限（页高占比）——大字号章标题不算页眉
MAX_BLOCK_SHARE = 0.03
#: 页眉页脚字号相对正文档的余量
SIZE_SLACK = 1.15
#: PyMuPDF span flags 的粗体位
_BOLD_FLAG = 1 << 4
_BOLD_HINTS = ("bold", "black", "heavy", "semibold", "demibold", "黑体")
_NUMS = re.compile(r"[0-9０-９]+")


def _is_bold(span: dict) -> bool:
    if span.get("flags", 0) & _BOLD_FLAG:
        return True
    font = (span.get("font") or "").lower()
    return any(h in font for h in _BOLD_HINTS)


def _repeat_key(text: str) -> str:
    """页眉里的页码每页不同，抹掉数字才能看出是同一块。"""
    return _NUMS.sub("#", re.sub(r"\s+", " ", text)).strip()


def _body_size(pages: list[dict]) -> float | None:
    """正文号：按字数加权取众数，正文总是占字最多。没有字号信息时返回 None。"""
    weighted: Counter = Counter()
    for page in pages:
        for block in page.get("blocks") or []:
            size = block.get("max_size")
            if size:
                weighted[round(float(size), 1)] += len(block.get("text") or "")
    return weighted.most_common(1)[0][0] if weighted else None


def _edge_kind(block: dict, h: float, body_size: float | None,
               edge_band: float, max_len: int) -> str | None:
    """像页眉页脚就返回 `top` / `bottom`，否则 None。

    四个条件：单行、够短、块不高、字号不超过正文 —— 最后两条把「每页重复的
    大字号章标题」挡在外面，只差页码的重复靠 `_repeat_key` 抹数字识别。
    """
    text = block.get("text") or ""
    if not text:
        return None
    # 页眉常是两块拼在一个 block 里：「单位名 / 第 N 页 共 M 页」，所以允许两行，
    # 但每行都必须短——正文段落不会这么规整。
    lines = [ln for ln in text.split("\n") if ln.strip()]
    if len(lines) > 2 or any(len(ln) > max_len for ln in lines):
        return None
    y0, y1 = block["bbox"][1], block["bbox"][3]
    if y1 - y0 > h * MAX_BLOCK_SHARE:
        return None
    size = block.get("max_size") or 0.0
    if body_size and size > body_size * SIZE_SLACK:
        return None
    if y1 <= h * edge_band:
        return "top"
    if y0 >= h * (1 - edge_band):
        return "bottom"
    return None


def mark_edge_repeats(pages: list[dict], repeat_share: float = REPEAT_SHARE,
                      edge_band: float = EDGE_BAND, max_len: int = MAX_REPEAT_LEN) -> list[dict]:
    """给跨页重复的页眉页脚块打 `is_header` / `is_footer`。

    只打标，不删块——删不删是下一层（切条款 / 建索引）的决定，这层不做。
    """
    if not pages:
        return pages
    threshold = max(3, int(len(pages) * repeat_share))
    body_size = _body_size(pages)
    counts: Counter = Counter()

    for page in pages:
        h = page.get("height") or 842.0
        seen = set()
        for block in page.get("blocks") or []:
            kind = _edge_kind(block, h, body_size, edge_band, max_len)
            if kind is None:
                continue
            key = (_repeat_key(block["text"]), kind)
            if key in seen:
                continue
            seen.add(key)
            counts[key] += 1

    repeated = {k for k, c in counts.items() if c >= threshold}
    if not repeated:
        return pages
    for page in pages:
        h = page.get("height") or 842.0
        for block in page.get("blocks") or []:
            kind = _edge_kind(block, h, body_size, edge_band, max_len)
            if kind is None:
                continue
            if (_repeat_key(block["text"]), kind) in repeated:
                block["is_header" if kind == "top" else "is_footer"] = True
    return pages


def parse_pdf(pdf_path) -> list[dict]:
    """按页抽出文本块、bbox 和版面线索，并标出跨页重复的页眉页脚。"""
    path = Path(pdf_path)
    if not path.exists():
        raise FileNotFoundError(f"PDF 不存在: {path}")

    doc = fitz.open(path)
    pages = []
    for i, page in enumerate(doc, start=1):
        raw = page.get_text("dict")
        blocks = []
        texts = []
        for block in raw.get("blocks", []):
            if block.get("type") != 0:
                continue
            lines = []
            sizes = []
            spans_count = 0
            bold_spans = 0
            for line in block.get("lines", []):
                parts = []
                for span in line.get("spans", []):
                    t = span.get("text", "")
                    parts.append(t)
                    spans_count += 1
                    if t.strip():
                        sizes.append(float(span.get("size", 0.0)))
                        if _is_bold(span):
                            bold_spans += 1
                lines.append("".join(parts))
            text = "\n".join(part for part in lines if part).strip()
            if not text:
                continue
            x0, y0, x1, y1 = block["bbox"]
            blocks.append({
                "text": text,
                "bbox": [float(x0), float(y0), float(x1), float(y1)],
                "max_size": round(max(sizes), 2) if sizes else 0.0,
                "bold_ratio": round(bold_spans / spans_count, 3) if spans_count else 0.0,
                "spans_count": spans_count,
            })
            texts.append(text)
        pages.append({
            "source": path.name,
            "page": i,
            "width": float(page.rect.width),
            "height": float(page.rect.height),
            "text": "\n".join(texts),
            "blocks": blocks,
        })
    doc.close()
    return mark_edge_repeats(pages)


def render_page_png(pdf_path, page_num: int, bboxes: list | None = None, scale: float = 1.6) -> bytes:
    """把一页渲成 PNG。bboxes 有值时在原件上画黄框。"""
    path = Path(pdf_path)
    doc = fitz.open(path)
    if page_num < 1 or page_num > doc.page_count:
        doc.close()
        raise ValueError(f"页码超出范围: {page_num}")
    page = doc[page_num - 1]
    if bboxes:
        shape = page.new_shape()
        for box in bboxes:
            if not box or len(box) != 4:
                continue
            rect = fitz.Rect(box)
            shape.draw_rect(rect)
        shape.finish(color=(0.83, 0.53, 0.02), fill=(1, 0.77, 0), fill_opacity=0.28, width=1.2)
        shape.commit()
    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
    data = pix.tobytes("png")
    doc.close()
    return data


def page_count(pdf_path) -> int:
    """PDF 页数。"""
    doc = fitz.open(pdf_path)
    n = doc.page_count
    doc.close()
    return n


def page_texts(pdf_path) -> dict[int, str]:
    """{页码: 该页纯文本}。校验段要比对原文，但不想要整份 blocks，所以单独给一个轻的。"""
    doc = fitz.open(pdf_path)
    try:
        return {i + 1: doc[i].get_text() for i in range(doc.page_count)}
    finally:
        doc.close()
