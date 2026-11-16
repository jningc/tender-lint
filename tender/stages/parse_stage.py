"""解析段：PDF → 章节树 + 页码；建索引或复用已有索引。

阶段 2 不做表格结构识别（`ParsedDoc.tables` 为空），那是 bbox 那一轮的事。
这一段的产物是 `ParsedDoc`，另外把索引对象带出来——索引是实现细节，不进契约。

切分单位由环境变量 `TENDER_CHUNK_UNIT` 决定：`paragraph`（默认，380 字拼段）
或 `clause`（条款树，见 `docs/条款口径.md`）。两种索引互不复用——manifest 里
记了 `chunk_unit`，对不上就重建。
"""

import json
import os
from pathlib import Path
from typing import Optional

from tender.clauses import build_clauses
from tender.contracts import ParsedDoc, Section
from tender.index import INDEX_VERSION, TenderIndex, build_tender_index, load_tender_index
from tender.parse import page_count, parse_pdf, pdf_toc
from tender.sections import split_sections
from tender.workflow import _emit

#: 切分单位：paragraph | clause
DEFAULT_CHUNK_UNIT = "paragraph"


def chunk_unit() -> str:
    """当前要用的切分单位，写错的值一律回退 paragraph。"""
    value = (os.environ.get("TENDER_CHUNK_UNIT") or DEFAULT_CHUNK_UNIT).strip().lower()
    return value if value in ("paragraph", "clause") else DEFAULT_CHUNK_UNIT


def index_cache_exists(pdf_path, unit: str | None = None) -> bool:
    """缓存存在、版本一致、切分单位一致才算可用。"""
    unit = unit or chunk_unit()
    manifest = Path("data") / "tender_index" / Path(pdf_path).stem / "manifest.json"
    if not manifest.exists():
        return False
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except Exception:
        return False
    if data.get("index_version") != INDEX_VERSION:
        return False
    return (data.get("chunk_unit") or "paragraph") == unit


def run(pdf_path, reuse_index: bool = True) -> tuple[ParsedDoc, TenderIndex]:
    """返回 (ParsedDoc, index)。reuse_index 且缓存存在时跳过解析与向量化。"""
    unit = chunk_unit()
    cached = reuse_index and index_cache_exists(pdf_path, unit)

    if cached:
        _emit("parse", "running", "载入已有向量库")
        index = load_tender_index(pdf_path)
        sections = index.sections
        n_pages = page_count(pdf_path)
        _emit("parse", "done", f"{n_pages} 页（复用索引）", page_count=n_pages)
        _emit("sections", "done", f"{len(sections)} 章", sections=sections)
    else:
        _emit("parse", "running", "按页抽文字和块坐标")
        pages = parse_pdf(pdf_path)
        _emit("parse", "done", f"{len(pages)} 页", page_count=len(pages))
        _emit("sections", "running", "按标题切章")
        sections = split_sections(pages)
        _emit("sections", "done", f"{len(sections)} 章", sections=sections)

        clauses = None
        if unit == "clause":
            _emit("clauses", "running", "按编号切条款")
            clauses = build_clauses(pages, sections, stem=Path(pdf_path).stem,
                                    toc=pdf_toc(pdf_path))
            n_clause = sum(1 for c in clauses if c.is_clause)
            _emit("clauses", "done",
                  f"{len(clauses)} 条（真条款 {n_clause}）", clauses=len(clauses))

        _emit("index", "running", f"开始按章建向量（单位：{unit}）")
        index = build_tender_index(
            pages, sections, pdf_path, unit=unit, clauses=clauses,
            on_chapter=lambda label, count: _emit("index", "running", f"正在建库 {label} {count} 段"),
        )
        n_pages = len(pages)
        _emit("index", "done", "索引建完")

    doc = ParsedDoc(
        pdf=str(Path(pdf_path).resolve()),
        page_count=n_pages,
        sections=[
            Section(type=s["type"], title=s["title"],
                    start_page=s["start_page"], end_page=s["end_page"])
            for s in sections
        ],
        tables=[],  # 阶段 2 不识别表格
    )
    return doc, index
