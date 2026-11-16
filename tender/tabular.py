"""表格结构：把表识别成「表头 + 行」，跨页的接回去。

为什么单独一层：正文的抽取单位是句子/条款，表格的抽取单位是**行**——一个评分项
= 一行，单元格不单独成条款。混在一起，评分表就会变成一堆散句（实测 chongzuo
的评分表被 p46/p47 截成两段，第二段没有表头，行全错位）。

只调 PyMuPDF 的 `find_tables`，不调模型。

跨页合并判据（三条同时满足）：
1. 上表贴页底（y1 > 85% 页高）、下表贴页顶（y0 < 15% 页高）；
2. 列数相同；
3. 左右边界差 < 12pt（同一张表的续页排版位置基本一致）。
第 2、3 条是关键：只看「贴边」会把页面上下两块无关的表拼起来。
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

import pymupdf

#: 评分 / 评审表的列头关键词
PINGFEN_KEYS = ("分值", "评分因素", "评分标准", "评审因素", "评分内容", "评审标准", "标准分",
                "评分项目", "评标标准")
#: 出现这些说明是「投标人自评 / 对应页码」之类的填写表，不是评标办法里的评分表
NEGATIVE_KEYS = ("自评", "对应页码", "投标人填写", "供应商填写")
#: 贴页边阈值
EDGE_TOP = 0.15
EDGE_BOTTOM = 0.85
#: 同一张表续页的左右边界容差（pt）
X_TOLERANCE = 12.0
#: 单元格里被换行切断的词，合并成一行文本
_WS = re.compile(r"\s+")


@dataclass
class TableRow:
    cells: list[str]
    page: int
    bbox: list[float]


@dataclass
class Table:
    id: str
    kind: str                 # pingfen | other
    header: list[str]
    rows: list[TableRow] = field(default_factory=list)
    pages: list[int] = field(default_factory=list)
    bbox: list[float] = field(default_factory=list)          # 首页的表区域
    page_bboxes: dict = field(default_factory=dict)          # {页: 该页的表区域}
    continued: bool = False
    col_count: int = 0

    def clean_rows(self) -> list[list[str]]:
        """去掉全空行和重复表头行——评分表里这两种特别多。"""
        head = [_clean(c) for c in self.header]
        out = []
        for row in self.rows:
            cells = [_clean(c) for c in row.cells]
            if not any(cells):
                continue
            if cells == head:
                continue
            out.append(cells)
        return out


def _clean(value) -> str:
    return _WS.sub("", str(value)) if value is not None else ""


def _looks_pingfen(cells: list) -> bool:
    text = "".join(_clean(c) for c in cells)
    if any(k in text for k in NEGATIVE_KEYS):
        return False
    return any(k in text for k in PINGFEN_KEYS)


def _fill_merged(rows: list[dict], col_count: int, header: list | None = None) -> None:
    """合并单元格向下继承：`None` 取上一行同列的值，`""` 保持空。

    不补的后果（实测 chongzuo）：`技术分（满分60分）` 是跨 4 行的合并单元格，
    只有第一行有值，后 3 行的「评审因素/分类」是 `None` → 抽出来后缺 key 字段
    → 被 `tables.py` 判成「人工看」，等于白抽。

    两条例外：
    - 真空白（`""`，比如备注列没写）不继承，否则会把上一行的备注抄下去；
    - **序号列不继承**——它天然逐行递增，PyMuPDF 把没写序号的格子给 None，
      照抄上一行会让 4 个评分项全都变成「2」。
    """
    if not rows:
        return
    skip = set()
    for i, cell in enumerate(header or []):
        if "序号" in _clean(cell):
            skip.add(i)
    last = [""] * col_count
    for idx, row in enumerate(rows):
        cells = list(row.get("cells") or [])
        cells = (cells + [None] * col_count)[:col_count]
        for i in range(col_count):
            if cells[i] is None:
                cells[i] = "" if (idx == 0 or i in skip) else last[i]
            else:
                last[i] = cells[i]
        row["cells"] = cells


def _raw_tables(pdf_path) -> list[dict]:
    """每页的原始表：{page, bbox, rows, cols, page_height}。

    单元格文本用 `page.get_textbox(cell_bbox)` 取，**不用 `t.extract()`**：
    extract 按内部写入顺序拼 span，中文标点会被甩到相邻位置
    （实测「其价格分为满分。」变成「其价格分为满分其他投，。标人」）。
    get_textbox 按版面坐标取，顺序正确。
    """
    doc = pymupdf.open(pdf_path)
    out = []
    try:
        for page in doc:
            try:
                found = page.find_tables().tables
            except Exception:
                continue
            for t in found:
                rows = []
                for row in t.rows:
                    # 保留 None：它是「被合并单元格覆盖」，和真的空 "" 不是一回事
                    cells = [_clean(page.get_textbox(cell)) if cell is not None else None
                             for cell in row.cells]
                    # 行级定位：跨页表里后半部分的行属于下一页，页号不能抄首页
                    rows.append({
                        "cells": cells,
                        "page": page.number + 1,
                        "bbox": [round(float(x), 1) for x in row.bbox] if row.bbox else [],
                    })
                if not rows or not any(any(c for c in r["cells"] if c) for r in rows):
                    continue
                out.append({
                    "page": page.number + 1,
                    "bbox": [round(float(x), 1) for x in t.bbox],
                    "rows": rows,
                    "cols": t.col_count,
                    "page_height": float(page.rect.height),
                })
    finally:
        doc.close()
    return out


def _can_merge(upper: dict, lower: dict) -> bool:
    if upper["cols"] != lower["cols"] or upper["cols"] < 2:
        return False
    if lower["page"] != upper["page"] + 1:
        return False
    if upper["bbox"][3] < upper["page_height"] * EDGE_BOTTOM:
        return False
    if lower["bbox"][1] > lower["page_height"] * EDGE_TOP:
        return False
    return (abs(upper["bbox"][0] - lower["bbox"][0]) <= X_TOLERANCE
            and abs(upper["bbox"][2] - lower["bbox"][2]) <= X_TOLERANCE)


def extract_tables(pdf_path, stem: str | None = None) -> list[Table]:
    """抽表并合并跨页。返回按页序的 `Table` 列表。

    行级 `page` / `bbox` 保留每一行自己的位置——跨页表的上半部分在 p46、
    下半部分在 p47，不能都记成首页。
    """
    stem = stem or Path(pdf_path).stem
    raw = _raw_tables(pdf_path)
    if not raw:
        return []

    merged: list[dict] = []
    for item in raw:
        prev = merged[-1] if merged else None
        if prev is not None and _can_merge(prev, item):
            prev["rows"].extend(item["rows"])
            prev["pages"].append(item["page"])
            prev["page_bboxes"][item["page"]] = item["bbox"]
            prev["continued"] = True
            continue
        item = dict(item)
        item["pages"] = [item["page"]]
        item["page_bboxes"] = {item["page"]: item["bbox"]}
        item["continued"] = False
        merged.append(item)

    tables: list[Table] = []
    for i, item in enumerate(merged):
        rows = item["rows"]
        # 合并单元格填充必须在跨页合并之后做：续页第一行不是表头，
        # 按页各自填充会把「序号」列当成普通列继承下去（实测 4 个评分项全变成 2）
        _fill_merged(rows, item["cols"], header=rows[0]["cells"] if rows else None)
        header = rows[0]["cells"] if rows else []
        body = rows[1:] if rows else []
        kind = "pingfen" if (_looks_pingfen(header)
                             or any(_looks_pingfen(r["cells"]) for r in rows[:2])) else "other"
        tables.append(Table(
            id=f"{stem}:t{i:03d}",
            kind=kind,
            header=header,
            rows=[TableRow(cells=r["cells"], page=r["page"], bbox=r["bbox"]) for r in body],
            pages=item["pages"],
            bbox=item["page_bboxes"][item["page"]],
            page_bboxes=item["page_bboxes"],
            continued=item["continued"],
            col_count=item["cols"],
        ))
    return tables


def pingfen_tables(tables: list[Table]) -> list[Table]:
    return [t for t in tables if t.kind == "pingfen"]


def dump(tables: list[Table], folder: Path, stem: str) -> Path:
    import json
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{stem}.json"
    path.write_text(json.dumps([asdict(t) for t in tables], ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return path
