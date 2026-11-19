"""表格结构：表头层级 + 行 + 合并单元格 + 跨页接回。

为什么单独一层：正文的抽取单位是句子/条款，表格的抽取单位是**行**——一个评分项
= 一行，单元格不单独成条款。混在一起，评分表就会变成一堆散句（实测 chongzuo
的评分表被 p46/p47 截成两段，第二段没有表头，行全错位）。

为什么还要表头层级和合并信息：评分表普遍是「两级表头 + 纵向合并」——
「标准分」拆成「分项 / 合计」，「商务部分（10分）」跨 3 行。只读扁平列名，
读出来是 `分类|序号|评审因素|分项|合计|评审标准`，「合计 10 分」属于哪一项
就说不清了。而核对最要紧的一条恰恰是 **Σ分项 = 合计**，没有这层关系就验不了。

三个字段各管一件事：
- `header_levels`：从粗到细的表头，跨列标题已填充到子列（`标准分` → 两列都是它）
- `header`：最细一级，等于 `header_levels[-1]`，给不需要层级的调用方用
- `TableRow.spans`：这一行里哪几格是合并的，`{col, rowspan, colspan}` 相对本行

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
#: 二级表头的文字长度上限（「分项」「合计」「分值」这种）
SUBHEADER_MAX_LEN = 8
#: 像数据不像表头：带数字或「X分 / 满分」
_DATA_HINT = re.compile(r"\d|[一二三四五六七八九十]分|满分")
#: 「技术部分（60分）」「商务部分（10分）」这种是**分类列的值**，不是表头
_CATEGORY_RE = re.compile(r"^(技术|商务|价格|资信|综合|其他|服务|报价).{0,8}(部分|分)")


@dataclass
class TableRow:
    cells: list[str]
    page: int
    bbox: list[float]
    #: 本行在表中的绝对行号（正文从 0 起，表头不计、跨页连续）
    row_index: int = 0
    #: 合并单元格：`{col, rowspan, colspan}`，`col` 是绝对列号；没合并就是空列表
    spans: list[dict] = field(default_factory=list)
    #: 每格是否「从上方继承来的」——`cells` 是稠密的（每行都填满，方便下游取数），
    #: 代价是 `商务部分（10分）` / `10分` 会在 3 行里各出现一次。求和、去重、
    #: 按分类分组时必须看这个标记，否则合计会被加 3 遍。
    inherited: list[bool] = field(default_factory=list)

    def own(self, col: int) -> str:
        """本行**自己声明**的值；继承来的返回空串。求和/分组一律用这个。"""
        if not (0 <= col < len(self.cells)):
            return ""
        if col < len(self.inherited) and self.inherited[col]:
            return ""
        return self.cells[col] or ""


@dataclass
class Table:
    id: str
    kind: str                 # pingfen | other
    header: list[str]         # 最细一级表头 = header_levels[-1]
    rows: list[TableRow] = field(default_factory=list)
    #: 从粗到细的表头；单级表头时只有一组
    header_levels: list[list[str]] = field(default_factory=list)
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

    # ---- 三种视图，都从 rows / row_index / spans 派生，不改存储 ----

    def score_col(self) -> int | None:
        """分值列在第几列（表头叫「分值 / 得分 / 分」）。"""
        for i, head in enumerate(self.header):
            h = _clean(head)
            if h in ("分值", "得分", "分", "满分") or "分值" in h:
                return i
        return None

    def score_of(self, row: "TableRow") -> str:
        """这一行的分值，**只保留数字**（可能是 `2-3` 这样的区间）。

        分值列通常最窄（实测 wenling 只有 39pt），左邻列句末的标点会被
        `get_textbox` 一起抓进来（`。2`、`，8`）。这类噪音没法靠内缩边界除掉
        ——标点比字窄——只能按列语义清洗：分值列除了数字不该有别的东西。
        """
        col = self.score_col()
        if col is None or col >= len(row.cells):
            return ""
        nums = re.findall(r"\d+(?:\.\d+)?", row.cells[col] or "")
        return "-".join(dict.fromkeys(nums))

    def grid(self) -> list[list[str]]:
        """稠密二维（`grid[row][col]`，含继承值）。对齐 Docling 的 `TableData.grid`。"""
        return [[row.cells[j] if j < len(row.cells) else "" for j in range(self.col_count)]
                for row in self.rows]

    def cells(self) -> list[dict]:
        """稀疏单元格列表，每格带**绝对** row/col。对齐 AWS / Azure 的 cell 数组。

        只输出「源格」——被合并覆盖的位置不单独出现，靠源格的 rowspan/colspan 表达。
        这就是转 HTML 需要的东西：`inherited=False` 的格才写 `<td>`，其余省略，
        否则浏览器会把 6 列渲染成 8 列。
        """
        covered = set()
        for i, row in enumerate(self.rows):
            for span in row.spans:
                for dr in range(span.get("rowspan", 1)):
                    for dc in range(span.get("colspan", 1)):
                        if dr or dc:
                            covered.add((i + dr, span["col"] + dc))
        out = []
        for i, row in enumerate(self.rows):
            span_at = {s["col"]: s for s in row.spans}
            for j in range(self.col_count):
                if (i, j) in covered:
                    continue
                span = span_at.get(j) or {}
                out.append({
                    "row": row.row_index if row.row_index or i == 0 else i,
                    "col": j,
                    "rowspan": span.get("rowspan", 1),
                    "colspan": span.get("colspan", 1),
                    "text": row.cells[j] if j < len(row.cells) else "",
                    "page": row.page,
                })
        return out

    def to_html(self) -> str:
        """HTML `<table>`：合并用 `rowspan/colspan` 属性，被覆盖的位置不写 `<td>`。

        为什么要这个出口：人工核对时最想看的就是浏览器里的一张表，
        读 JSON 要自己在脑子里拼合并关系。Unstructured 和 Docling 都留了这个口。
        """
        import html as _html
        esc = _html.escape
        levels = self.header_levels or ([self.header] if self.header else [])
        parts = ["<table>"]
        if levels:
            parts.append("  <thead>")
            for line in _header_html(levels, esc):
                parts.append(f"    <tr>{line}</tr>")
            parts.append("  </thead>")
        parts.append("  <tbody>")
        by_row: dict[int, list[dict]] = {}
        for cell in self.cells():
            by_row.setdefault(cell["row"], []).append(cell)
        for i in range(len(self.rows)):
            tds = []
            for cell in sorted(by_row.get(i, []), key=lambda c: c["col"]):
                attrs = ""
                if cell["rowspan"] > 1:
                    attrs += f' rowspan="{cell["rowspan"]}"'
                if cell["colspan"] > 1:
                    attrs += f' colspan="{cell["colspan"]}"'
                tds.append(f"<td{attrs}>{esc(cell['text'])}</td>")
            parts.append("    <tr>" + "".join(tds) + "</tr>")
        parts.append("  </tbody>")
        parts.append("</table>")
        return "\n".join(parts)


def _header_html(levels: list[list[str]], esc) -> list[str]:
    """表头两行：粗一级里跨列的格写 colspan，不跨的写 rowspan 盖住第二行。

    不这么写列数就超了——「标准分」占两列，如果两层都逐列输出，
    6 列的表现在会有 8 个 `<th>`。
    """
    top = levels[0]
    group = []      # [(起始列, 结束列, 文本)]
    i = 0
    while i < len(top):
        j = i
        while j + 1 < len(top) and top[j + 1] == top[i]:
            j += 1
        group.append((i, j, top[i]))
        i = j + 1

    first = []
    for start, end, text in group:
        attrs = ""
        if end > start:
            # 跨列的格不能 rowspan：它下面要留给第二级子列（分项/合计）
            attrs += f' colspan="{end - start + 1}"'
        elif len(levels) > 1:
            attrs += ' rowspan="2"'
        first.append(f"<th{attrs}>{esc(text)}</th>")

    lines = ["".join(first)]
    if len(levels) > 1:
        second = []
        for start, end, _ in group:
            if end > start:
                for k in range(start, end + 1):
                    sub = levels[1][k] if k < len(levels[1]) else ""
                    second.append(f"<th>{esc(sub)}</th>")
        if second:
            lines.append("".join(second))
    return lines


def _clean(value) -> str:
    return _WS.sub("", str(value)) if value is not None else ""


def _looks_pingfen(cells: list) -> bool:
    text = "".join(_clean(c) for c in cells)
    if any(k in text for k in NEGATIVE_KEYS):
        return False
    return any(k in text for k in PINGFEN_KEYS)


def _col_lefts(rows: list[dict], col_count: int) -> list[float | None]:
    """每列的真实左边界：取该列所有非 `None` 单元格 x0 的中位数。

    平均列宽在这个表上不成立——「评审标准」列占了页面一半宽，
    用平均宽会把单格算成 `colspan=2`（第一版实测就是这样）。
    """
    import statistics
    xs: list[list[float]] = [[] for _ in range(col_count)]
    for row in rows:
        for j, box in enumerate(row.get("bboxes") or []):
            if box and j < col_count:
                xs[j].append(box[0])
    return [statistics.median(v) if v else None for v in xs]


def _spans(rows: list[dict], col_count: int) -> None:
    """算合并单元格，返回 `{col, rowspan, colspan}`。

    **rowspan 用 `None` 连续性**：被纵向合并覆盖的格子 PyMuPDF 给 `None`，
    向下数连续多少个就够了（实测「商务部分（10分）」「10分」都是 3 行）。

    **colspan 用列边界几何**：本格右边界跨过了哪几列的左边界就是几列。
    `标准分` 的右边界盖住「合计」列左边界 → colspan=2。

    必须在 `_fill_merged` 之前跑——填充之后 `None` 全变成值，分不出「合并」和「真空白」。
    """
    lefts = _col_lefts(rows, col_count)
    for i, row in enumerate(rows):
        cells = row.get("cells") or []
        bboxes = row.get("bboxes") or []
        found = []
        for j in range(col_count):
            if j >= len(cells) or cells[j] is None:
                continue
            rowspan = 1
            while (i + rowspan < len(rows)
                   and j < len(rows[i + rowspan]["cells"])
                   and rows[i + rowspan]["cells"][j] is None
                   # 跨页表的下半部分在下一页，rowspan 不跨页
                   and rows[i + rowspan]["page"] == row["page"]):
                rowspan += 1
            colspan = 1
            box = bboxes[j] if j < len(bboxes) else None
            if box:
                for k in range(j + 1, col_count):
                    if lefts[k] is not None and lefts[k] < box[2] - 2:
                        colspan = k - j + 1
                    else:
                        break
            if rowspan > 1 or colspan > 1:
                found.append({"col": j, "rowspan": rowspan, "colspan": colspan})
        row["spans"] = found


def _expand_col_spans(cells: list, spans: list[dict], col_count: int) -> list[str]:
    """把跨列标题填到它覆盖的每一列（`标准分` 占两列 → 两列都是 `标准分`）。"""
    out = [_clean(c) for c in cells] + [""] * (col_count - len(cells or []))
    out = out[:col_count]
    for span in spans:
        col, width = span["col"], span.get("colspan", 1)
        if width > 1 and col < len(out):
            for k in range(col + 1, min(col + width, col_count)):
                out[k] = out[col]
    return out


def _split_header(rows: list[dict], col_count: int) -> tuple[list[list[str]], int]:
    """把表头拆成若干级，返回 `(header_levels, 正文起始行号)`。

    判据是「第 2 行在第一行的空位上填了短词，而自己不像数据」——
    评分表的粗表头 `标准分` 跨两列，PyMuPDF 只把第一个格判成表头，
    第二个格是 `None`；子表头 `分项 / 合计` 落在下一行。**不是**按行数
    或字号猜的，只认这个结构特征，判断错了也就是少一级表头，不会把数据行吃掉。
    """
    if not rows:
        return [], 0
    first = rows[0]
    raw_first = [_clean(c) for c in (first.get("cells") or [])]
    raw_first += [""] * (col_count - len(raw_first))

    # 「技术部分（60分）」占着表头第一格、下面是空的 → 它是**分类列的值**，
    # 不是列名。实测 beijing t008 因此把「技术部分」当成了表头，
    # 真正的列名「评审内容 / 评审标准」被挤到数据行里。
    if (col_count >= 3 and _CATEGORY_RE.match(raw_first[0])
            and len(rows) > 1
            and not _clean((rows[1].get("cells") or [""])[0] if (rows[1].get("cells") or []) else "")):
        category = raw_first[0]
        level1 = ["分类"] + raw_first[1:]
        # 分类值回填到每一行，并把它记成跨全部行的合并
        for r in rows[1:]:
            cells = list(r.get("cells") or [])
            if cells and not _clean(cells[0]):
                cells[0] = category
                r["cells"] = cells
        spans = first.get("spans") or []
        first["spans"] = [s for s in spans if s["col"] != 0] + [
            {"col": 0, "rowspan": len(rows) - 1, "colspan": 1}]
        return [level1], 1

    level1 = _expand_col_spans(first.get("cells") or [], first.get("spans") or [], col_count)
    if len(rows) < 2:
        return [level1], 1

    second = [_clean(c) for c in (rows[1].get("cells") or [])]
    second += [""] * (col_count - len(second))
    # 空位要拿**展开前**的那行找：展开会把「标准分」填进右侧空列，
    # 填完之后再看就没有洞了（第一版就这么错的，两级表头一个也没认出来）
    holes = [j for j in range(col_count) if not raw_first[j]]
    filled = [j for j in holes
              if second[j] and len(second[j]) <= SUBHEADER_MAX_LEN
              and not _DATA_HINT.search(second[j])]
    if not filled:
        return [level1], 1

    level2 = [second[j] or level1[j] for j in range(col_count)]
    return [level1, level2], 2


def _fill_merged(rows: list[dict], col_count: int, header: list | None = None,
                 start: int = 1) -> None:
    """合并单元格向下继承：`None` 取上一行同列的值，`""` 保持空。

    不补的后果（实测 chongzuo）：`技术分（满分60分）` 是跨 4 行的合并单元格，
    只有第一行有值，后 3 行的「评审因素/分类」是 `None` → 抽出来后缺 key 字段
    → 被 `tables.py` 判成「人工看」，等于白抽。

    三条例外：
    - 表头行（`start` 之前）不动——两级表头里第 2 行是子表头，不能继承第 1 行；
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
        if idx < start:
            row["cells"] = ["" if c is None else c for c in cells]
            row["inherited"] = [False] * col_count
            continue
        marks = [False] * col_count
        for i in range(col_count):
            if cells[i] is None:
                cells[i] = "" if (idx == 0 or i in skip) else last[i]
                marks[i] = True   # 这一格是继承来的，不是本行自己声明的
            else:
                last[i] = cells[i]
        row["cells"] = cells
        row["inherited"] = marks


def _shrink(box, pad: float = 2.0):
    """单元格取文本前水平内缩一点。

    `get_textbox` 的判据是「字符与框相交」——一个字压在列边界上时，
    左右两格都会抓到它。实测 wenling 的分值列（宽 39pt）抓出来是
    `以/2`、`理/个/2`、`需，外还8`，全是左邻列末尾的残字。内缩 2pt 就够了：
    中文正文宽 10–12pt，一个字不可能缩进 2pt 还留在框里。
    """
    rect = pymupdf.Rect(box)
    if rect.width > pad * 2 + 6:
        rect.x0 += pad
        rect.x1 -= pad
    return rect


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
                    cells = [_clean(page.get_textbox(_shrink(cell))) if cell is not None else None
                             for cell in row.cells]
                    # 行级定位：跨页表里后半部分的行属于下一页，页号不能抄首页
                    rows.append({
                        "cells": cells,
                        # 原始单元格 bbox：colspan 靠它的宽度算，不能用 None 猜
                        "bboxes": ([list(map(float, c)) if c is not None else None
                                    for c in row.cells] if row.cells else []),
                        "page": page.number + 1,
                        "bbox": [round(float(x), 1) for x in row.bbox] if row.bbox else [],
                    })
                if not rows or not any(any(c for c in r["cells"] if c) for r in rows):
                    continue
                _spans(rows, t.col_count)
                out.append({
                    "page": page.number + 1,
                    "page_seq": sum(1 for x in out if x["page"] == page.number + 1) + 1,
                    "bbox": [round(float(x), 1) for x in t.bbox],
                    "rows": rows,
                    "cols": t.col_count,
                    "page_height": float(page.rect.height),
                })
    finally:
        doc.close()
    return out


def _is_continuation(row: dict, col_count: int) -> bool:
    """续页首行是不是「上一页最后一格被切断」的残余。

    实测 chongzuo p46→p47：`(5) 满足招标文件要求且…` 在 p46 末行最后一格，
    `(6) 价格分计算公式…` 在 p47 首行，前三列全空。这两段本来是**同一个单元格**，
    不拼回去就多出一个只有一格有值的假数据行。
    """
    cells = row.get("cells") or []
    if len(cells) != col_count or col_count < 2:
        return False
    return (not any(_clean(c) for c in cells[:-1])
            and bool(_clean(cells[-1])))


def _is_repeat_header(row: dict, header: list[str]) -> bool:
    """续页顶端重复印的表头行：除第一列（分类）外和表头完全一致。"""
    cells = [_clean(c) for c in (row.get("cells") or [])]
    want = [_clean(h) for h in header]
    if len(cells) != len(want) or len(want) < 2:
        return False
    return cells[1:] == want[1:] and bool(cells[0])


def _can_merge(upper: dict, lower: dict) -> bool:
    if upper["cols"] != lower["cols"] or upper["cols"] < 2:
        return False
    # 链式合并要用**最后一页**比：p46+p47 合成一张表后，接 p48 时
    # 拿 p46 去比会判「46+1 ≠ 48」而断掉（实测 chongzuo t010 少了一整页 5 行）
    last_page = upper.get("last_page", upper["page"])
    if lower["page"] != last_page + 1:
        return False
    upper_bbox = upper.get("last_bbox", upper["bbox"])
    upper_h = upper.get("last_page_height", upper["page_height"])
    lower_h = lower["page_height"]
    if upper_bbox[3] < upper_h * EDGE_BOTTOM:
        return False
    if lower["bbox"][1] > lower_h * EDGE_TOP:
        return False
    return (abs(upper_bbox[0] - lower["bbox"][0]) <= X_TOLERANCE
            and abs(upper_bbox[2] - lower["bbox"][2]) <= X_TOLERANCE)


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
    for orig_index, item in enumerate(raw):
        item = dict(item)
        # id 用**原始表序号**，不用合并后的下标：合并逻辑一改，合并后的列表长度就变，
        # 后面所有表的编号会前移，题集里的 id 全部错位（实测 chongzuo t010 变成了别页的表）
        item.setdefault("orig_index", orig_index)
        prev = merged[-1] if merged else None
        if prev is not None and _can_merge(prev, item):
            fresh = item["rows"]
            first = fresh[0] if fresh else None
            last = prev["rows"][-1] if prev["rows"] else None
            if first is not None and last is not None and _is_continuation(first, prev["cols"]):
                # 同一单元格跨页被切开 → 拼回上一行最后一列，不留假数据行
                last["cells"][-1] = (last["cells"][-1] or "") + (first["cells"][-1] or "")
                fresh = fresh[1:]
            prev["rows"].extend(fresh)
            prev["pages"].append(item["page"])
            prev["page_bboxes"][item["page"]] = item["bbox"]
            # 记录最后一页/最后一块，供下一条判断链式合并
            prev["last_page"] = item["page"]
            prev["last_bbox"] = item["bbox"]
            prev["last_page_height"] = item["page_height"]
            prev["continued"] = True
            continue
        item["pages"] = [item["page"]]
        item["page_bboxes"] = {item["page"]: item["bbox"]}
        item["last_page"] = item["page"]
        item["last_bbox"] = item["bbox"]
        item["last_page_height"] = item["page_height"]
        item["continued"] = False
        merged.append(item)

    tables: list[Table] = []
    for i, item in enumerate(merged):
        rows = item["rows"]
        cols = item["cols"]
        levels, start = _split_header(rows, cols)
        # 合并单元格填充必须在跨页合并之后做：续页第一行不是表头，
        # 按页各自填充会把「序号」列当成普通列继承下去（实测 4 个评分项全变成 2）
        _fill_merged(rows, cols, header=levels[-1] if levels else None, start=start)
        header = levels[-1] if levels else []
        # 续页顶端会把表头再印一遍，那不是数据行（实测 beijing t008 p34 首行）
        body = [r for r in rows[start:] if not _is_repeat_header(r, header)]
        kind = "pingfen" if (_looks_pingfen(header)
                             or any(_looks_pingfen(r["cells"]) for r in rows[:2])) else "other"
        tables.append(Table(
            # id 用「首页 + 页内第几张表」，不用任何全局下标：
            # 列表下标会随合并逻辑变化而整体前移（实测 t006 变成 t009），
            # 题集里记的 id 就全废了。页码方案不受合并影响。
            id=f"{stem}:p{item['page']}:{item.get('page_seq', 1)}",
            kind=kind,
            header=header,
            header_levels=levels,
            rows=[TableRow(cells=r["cells"], page=r["page"], bbox=r["bbox"],
                           row_index=idx,
                           spans=r.get("spans") or [],
                           inherited=r.get("inherited") or [])
                  for idx, r in enumerate(body)],
            pages=item["pages"],
            bbox=item["page_bboxes"][item["page"]],
            page_bboxes=item["page_bboxes"],
            continued=item["continued"],
            col_count=cols,
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


def load_tables(folder: Path, stem: str) -> list[Table]:
    """读回 `dump()` 写的表。没有文件就返回空表（旧索引没有这一层）。"""
    import json
    path = Path(folder) / f"{stem}.json"
    if not path.is_file():
        return []
    out = []
    for item in json.loads(path.read_text(encoding="utf-8")):
        rows = [TableRow(
            cells=r.get("cells") or [],
            page=r.get("page") or 0,
            bbox=r.get("bbox") or [],
            row_index=r.get("row_index") or 0,
            spans=r.get("spans") or [],
            inherited=r.get("inherited") or [],
        ) for r in item.get("rows") or []]
        out.append(Table(
            id=item.get("id") or "",
            kind=item.get("kind") or "other",
            header=item.get("header") or [],
            rows=rows,
            header_levels=item.get("header_levels") or [],
            pages=item.get("pages") or [],
            bbox=item.get("bbox") or [],
            page_bboxes=item.get("page_bboxes") or {},
            continued=bool(item.get("continued")),
            col_count=item.get("col_count") or 0,
        ))
    return out
