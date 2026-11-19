"""表格 → 可检索文本。

为什么单独一层：表格的检索单位不是「行」，也不是「整表」，而是
**「带表头的那一行 + 表级事实」**。`4分` 单独成块不知道是谁的分；
`10分` 不属于任何单项，是三项之和。两者都得进 chunk，才答得出
「商务部分一共多少分」和「类似项目业绩每个得几分」。

生成方式是**代码模板**，不经过任何模型，理由见 `docs/计划/表格检索方案.md`：
文本层已经抽对了，让模型再读一遍图重写，等于把正确的数据丢进会重新出错的
环节，而且同一行跑两次结果不一样，回归测不了。
"""

from __future__ import annotations

import re

from tender.tabular import Table, TableRow, _clean

#: 长文本列，单独占一行（其余字段用 ｜ 串成一行）
_STANDARD_NAMES = ("评审标准", "评分标准", "评标标准", "说明", "评分内容")
#: 表级事实列，不重复进每行字段
_TOTAL_NAMES = ("合计", "总分")
_ITEM_NAMES = ("分项", "分值", "得分", "分数")
_NUM = re.compile(r"\d+(?:\.\d+)?")


def _col_index(table: Table, *names: str) -> int | None:
    """按列名找第几列（用最细一级表头）。

    **必须精确优先**：`'分项' in '评分项目'` 是 True——第一版用 `in` 判，
    于是「评分项目」被当成「分项」列，把「投标报价（30分）」整个抓出来
    算出 `Σ分项30分` 这种假事实。
    """
    level = table.header_levels[-1] if table.header_levels else table.header
    heads = [_clean(h) for h in level]
    for i, head in enumerate(heads):
        if head in names:
            return i
    # 退一步只认「以该词开头」，不再允许出现在中间
    for i, head in enumerate(heads):
        for name in names:
            if name and head.startswith(name):
                return i
    return None


def _numbers(table: Table, col: int | None) -> list[float]:
    """某列里**本行自己声明**的数字（继承来的不算，否则合计会被加好几遍）。"""
    if col is None:
        return []
    out = []
    for row in table.rows:
        m = _NUM.search(row.own(col))
        if m:
            out.append(float(m.group()))
    return out

#: 括号里的分值：「质量控制措施(8分)」「实施方案（12分）」
_PAREN_SCORE = re.compile(r"[（(][^）)]{0,6}?(\d+(?:\.\d+)?)\s*分[）)]")
#: 合计/小计行，累加时要跳过（否则 50 分的小计会被加进分项）
_SUBTOTAL = re.compile(r"合计|小计|总分|总得分")
#: 这些列名承载「评审项」，分值没独立列时从这里抓
_LABEL_NAMES = ("评审因素", "评审内容", "评分因素", "评分项目", "评分项", "内容")


def _score_parts(table: Table) -> tuple[list[float], float | None]:
    """拆出（各分项的分值, 原文自带的合计）。

    两个来源，按优先级：
    1. 有「分项/分值/得分」列 → 直接读列
    2. 分值写在评审项名字里（`质量控制措施(8分)`）→ 从括号抓

    合计也有两个来源：
    1. **行**形式的合计（wenling 末行 `['8','总分','',50]` → 表头叫「总分」不叫「合计」）
    2. **列**形式的合计（p33:2 第 5 列）
    两种都是原文写着的，不该标「推定」。
    """
    score_col = _col_index(table, *_ITEM_NAMES)
    label_col = _col_index(table, *_LABEL_NAMES)
    items: list[float] = []
    subtotal = None
    for row in table.rows:
        label = row.own(label_col) if label_col is not None else ""
        value = None
        if score_col is not None:
            m = _NUM.search(row.own(score_col))
            if m:
                value = float(m.group())
        subtotal_hit = _SUBTOTAL.search(label or "")
        if score_col is not None:
            subtotal_hit = subtotal_hit or _SUBTOTAL.search(row.own(score_col) or "")
        if subtotal_hit:
            if value is not None:
                subtotal = value
            continue
        if value is None and label:
            m = _PAREN_SCORE.search(label)
            if m:
                value = float(m.group(1))
        if value is not None:
            items.append(value)
    return items, subtotal


def table_fact(table: Table) -> str:
    """表级事实：合计多少、共几项。

    按证据强度降级，**不发明原文里没有的数字**：

    1. 原文写了合计（合计行 or 合计列）→ 引用（`本表合计10分（3项）`）
    2. 没有合计，但各分项可读 → 累加并标「推定」
    3. 连分项都读不出 → 返回空，宁可不写
    """
    items, subtotal = _score_parts(table)
    total_col = _col_index(table, *_TOTAL_NAMES)

    # 行的形式：wenling 末行「总分 50」
    if subtotal is not None:
        if items and abs(sum(items) - subtotal) > 1e-6:
            return (f"本表合计{subtotal:g}分（但分项之和为{sum(items):g}分，"
                    f"原文不一致，需人工核对）")
        return f"本表合计{subtotal:g}分（{len(items) or len(table.rows)}项）"

    # 列的形式：p33:2 的「合计」列
    if total_col is not None:
        total_text = ""
        for row in table.rows:
            value = row.own(total_col)
            if _NUM.search(value):
                total_text = value
                break
        if total_text:
            total_num = float(_NUM.search(total_text).group())
            if items and abs(sum(items) - total_num) > 1e-6:
                return (f"本表合计{total_text}（但分项之和为{sum(items):g}分，"
                        f"原文不一致，需人工核对）")
            return f"本表合计{total_text}（{len(table.rows)}项）"

    # 累加只有在「每一项都抓到了」时才可信：漏一项少算、重复一项多算，
    # 数字错比没有更糟。实测 chongzuo 9 行只抓到 8 项（实施方案那行没写分值、
    # 业绩分被跨页拆成两行各抓一次），累加出 100 分，而实际是 80 分。
    if items and len(items) == len(table.rows):
        return f"Σ分项{sum(items):g}分（{len(items)}项，推定；原文未声明合计）"
    return ""


def small_chunk(table: Table, row: TableRow, section: str | None = None) -> str:
    """一行 → 一段可检索文本。字段名 + 原文值，不改写一个字。"""
    level = table.header_levels[-1] if table.header_levels else table.header
    standard_col = _col_index(table, *_STANDARD_NAMES)
    total_col = _col_index(table, *_TOTAL_NAMES)

    head = f"[评分表 {table.id} 第{row.row_index + 1}/{len(table.rows)}行"
    first = _clean(row.cells[0]) if row.cells else ""
    if first:
        head += f"｜{first}"
    if section:
        head += f"｜{section}"
    head += "]"

    # 列名去重：chongzuo 的表头是 ['序号','序号','评审因素','评标标准']——
    # 第二列其实是「分类」（值是「价格分（20分）」），原表把表头写成了两个「序号」。
    # 不去重的话，chunk 里只有「序号：商务分（满分20分）」，问「商务分多少」检索不到。
    seen: dict[str, int] = {}
    labels = []
    for name in level:
        clean = _clean(name)
        if clean in seen:
            seen[clean] += 1
            clean = "分类" if clean == "序号" else f"{clean}{seen[clean]}"
        else:
            seen[clean] = 1
        labels.append(clean)

    fields = []
    for i, name in enumerate(labels):
        if i == standard_col or i == total_col:
            continue
        value = _clean(row.cells[i]) if i < len(row.cells) else ""
        if value:
            fields.append(f"{name}：{value}")

    fact = table_fact(table)
    if fact:
        fields.append(fact)

    lines = [head]
    if fields:
        lines.append("｜".join(fields))
    if standard_col is not None and standard_col < len(row.cells):
        standard = _clean(row.cells[standard_col])
        if standard:
            label = _clean(level[standard_col]) if standard_col < len(level) else "评审标准"
            lines.append(f"{label}：{standard}")
    return "\n".join(lines)


def build_chunks(tables: list[Table], section_of=None) -> list[dict]:
    """所有评分表 → (text, table_id, row_index, page, row) 列表。

    `section_of(table)` 可选，返回表所属章节标题，拼进 chunk 头。
    """
    out = []
    for table in tables:
        if table.kind != "pingfen" or not table.rows:
            continue
        section = section_of(table) if section_of else None
        for row in table.rows:
            out.append({
                "text": small_chunk(table, row, section),
                "table_id": table.id,
                "row_index": row.row_index,
                "page": row.page,
                "bbox": row.bbox,
                "section": section,
            })
    return out


class TableIndex:
    """small chunk 的检索：BM25（和条款库同一套分词），命中后由调用方取整表。"""

    def __init__(self, chunks: list[dict]):
        from rank_bm25 import BM25Okapi

        from tender.index import _tokenize

        self._tokenize = _tokenize
        self.chunks = chunks
        self.bm25 = BM25Okapi([_tokenize(c["text"]) for c in chunks]) if chunks else None

    def search(self, query: str, k: int = 3) -> list[dict]:
        if not self.bm25:
            return []
        scores = self.bm25.get_scores(self._tokenize(query))
        order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:k]
        return [{**self.chunks[i], "score": round(float(scores[i]), 2)}
                for i in order if scores[i] > 0]
