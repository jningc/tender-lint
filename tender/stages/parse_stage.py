"""解析段：PDF → 章节树 + 页码 + 表格结构；建索引或复用已有索引。

产物是 `ParsedDoc`，另外把索引对象带出来——索引是实现细节，不进契约。
表格（`tabular.Table`）跟着索引一起落盘，评分表那条线直接读它，
不参与 BM25/向量。

切分单位由环境变量 `TENDER_CHUNK_UNIT` 决定：默认 `clause`（条款树），
可改 `paragraph`（380 字拼段）。两种索引互不复用——manifest 里记了
`chunk_unit`，对不上就重建。

按页正文（`page_texts`）在**索引建完（或复用就绪）后**才交给前端 / 写入
`page_texts.json`；网页解析栏只读这份，不再现场抽 PDF。
"""

import json
import os
from pathlib import Path

from tender.clauses import build_clauses
from tender.contracts import ParsedDoc, Section
from tender.index import INDEX_VERSION, TenderIndex, build_tender_index, load_tender_index
from tender.parse import (
    load_page_texts,
    page_count,
    pages_as_texts,
    parse_pdf,
    pdf_toc,
    save_page_texts,
)
from tender.sections import section_of_page, split_sections, types_of
from tender.frontmatter import extract, is_frontmatter, to_chunks
from tender.table_chunks import build_chunks as build_table_chunks
from tender.tabular import extract_tables
from tender.workflow import _emit

#: 切分单位：paragraph | clause
#: clause = 条款树（编号族+栈，书签辅助）+ 父标题链 context
DEFAULT_CHUNK_UNIT = "clause"


def chunk_unit() -> str:
    """当前要用的切分单位，写错的值一律回退 paragraph。"""
    value = (os.environ.get("TENDER_CHUNK_UNIT") or DEFAULT_CHUNK_UNIT).strip().lower()
    return value if value in ("paragraph", "clause") else "paragraph"


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


def _ensure_page_texts(pdf_path) -> list[dict]:
    """复用索引时也要有按页正文；没有落盘就当场 parse 并写入。"""
    try:
        return load_page_texts(pdf_path)
    except FileNotFoundError:
        pages = parse_pdf(pdf_path)
        save_page_texts(pdf_path, pages)
        return pages_as_texts(pages)


def run(pdf_path, reuse_index: bool = True) -> tuple[ParsedDoc, TenderIndex, list[dict]]:
    """返回 (ParsedDoc, index, page_texts)。page_texts 为 [{page, text}, ...]。"""
    unit = chunk_unit()
    cached = reuse_index and index_cache_exists(pdf_path, unit)

    if cached:
        _emit("parse", "running", "载入已有向量库")
        index = load_tender_index(pdf_path)
        sections = index.sections
        page_texts = _ensure_page_texts(pdf_path)
        n_pages = len(page_texts) or page_count(pdf_path)
        _emit("parse", "done", f"{n_pages} 页（复用索引）", page_count=n_pages)
        _emit("sections", "done", f"{len(sections)} 章", sections=sections)
        # 复用时索引已在，正文此时才对前端可见（与全量「index done」对齐）
        _emit("index", "done", "索引已复用", page_texts=page_texts)
    else:
        _emit("parse", "running", "按页抽文字和块坐标")
        pages = parse_pdf(pdf_path)
        page_texts = pages_as_texts(pages)
        _emit("parse", "done", f"{len(pages)} 页", page_count=len(pages))
        _emit("sections", "running", "按标题切章（正文优先）")
        sections = split_sections(pages, toc=pdf_toc(pdf_path))
        _emit("sections", "done", f"{len(sections)} 章", sections=sections)

        # 表格要在切条款之前抽出来：表格区不参与条款聚合，否则一张跨页表格
        # 会被整段吸进上一条（崇左「一、项目基本情况」就是这么被吸爆的）。
        _emit("tables", "running", "识别表格结构")
        tables = extract_tables(pdf_path, stem=Path(pdf_path).stem)
        table_boxes: dict[int, list[list[float]]] = {}
        # 排除两类表：
        # 1. 评分表——纯数据，行不进条款树（单独走 tabular）
        # 2. 前附表——「条款号 | 条目 | 内容」是**稀疏对照表**，左列的 `2.2/11.2/26.1`
        #    指向正文里已有的条款，不是条款本身。混进树里会让编号看起来乱序
        #    （北京 p7–9 的 2.2→5.2.5→11.2→…→ 然后 p10 又从 1.1 重来）。
        #    内容由 frontmatter.py 单独抽成 {条款号, 项目, 值}。
        for t in [x for x in tables if x.kind == "pingfen" or is_frontmatter(x)]:
            for pno, bb in (t.page_bboxes or {}).items():
                table_boxes.setdefault(int(pno), []).append(list(bb))
            for row in t.rows or []:
                table_boxes.setdefault(row.page, []).append(list(row.bbox))

        # 前附表的条目转成独立 chunk：它们不进条款树（见上），但必须能被检索到。
        # 北京「01包：8000元」只出现在附表 p8，不这么并进来就问不出金额。
        heights = {p["page"]: p.get("height") or 842.0 for p in pages}
        fms = extract(tables)
        fm_chunks = to_chunks(fms, sections, source=Path(pdf_path).name, heights=heights)

        # 评分表行也要进索引：表在条款树外（行号会建假节点），但内容必须能查到。
        # 实测「商务部分多少分」返回的全是条款标题，一条分值都搜不到——就是这个缺口。
        # 走 table_chunks 的 small chunk（模板拼、不调模型），补全 chunk 需要的字段。
        table_chunks: list[dict] = []
        for c in build_table_chunks(
                tables,
                section_of=lambda t: (section_of_page(sections, (t.pages or [0])[0]) or {}).get("title")):
            sec = section_of_page(sections, c["page"]) or {}
            table_chunks.append({
                **c,
                "context": c["text"],          # 表格行不拼父链，它自己带表头
                "page_height": heights.get(c["page"]) or 842.0,
                "source": Path(pdf_path).name,
                "section_type": sec.get("type") or "score",
                "section_types": types_of(sec),
                "section_title": sec.get("title") or "",
                "kind": "table_row",
                "clause_id": f"{Path(pdf_path).stem}:{c['table_id']}:r{c['row_index']}",
                "number": "",
            })
        if fms:
            _emit("frontmatter", "done",
                  f"{len(fms)} 张前附表 / {sum(len(f.items) for f in fms)} 条进索引",
                  frontmatter=sum(len(f.items) for f in fms))

        clauses = None
        if unit == "clause":
            _emit("clauses", "running", "按编号切条款")
            clauses = build_clauses(pages, sections, stem=Path(pdf_path).stem,
                                    toc=pdf_toc(pdf_path), table_boxes=table_boxes)
            n_clause = sum(1 for c in clauses if c.is_clause)
            _emit("clauses", "done",
                  f"{len(clauses)} 条（真条款 {n_clause}）", clauses=len(clauses))

        _emit("index", "running", f"开始按章建向量（单位：{unit}）")
        n_pf = sum(1 for t in tables if t.kind == "pingfen" and t.rows)
        _emit("tables", "done", f"{len(tables)} 张表（评分表 {n_pf}）", tables=len(tables))
        index = build_tender_index(
            pages, sections, pdf_path, unit=unit, clauses=clauses, tables=tables,
            extra_chunks=list(fm_chunks) + table_chunks,
            on_chapter=lambda label, count: _emit("index", "running", f"正在建库 {label} {count} 段"),
        )
        # 索引目录会 rmtree 重建，正文必须在建库之后再写；前端也只在此时拿到 page_texts
        save_page_texts(pdf_path, pages)
        n_pages = len(pages)
        _emit("index", "done", "索引建完", page_texts=page_texts)

    doc = ParsedDoc(
        pdf=str(Path(pdf_path).resolve()),
        page_count=n_pages,
        sections=[
            Section(type=s["type"], types=s.get("types") or [s["type"]], title=s["title"],
                    start_page=s["start_page"], end_page=s["end_page"])
            for s in sections
        ],
        tables=[],  # 阶段 2 不识别表格
    )
    return doc, index, page_texts
