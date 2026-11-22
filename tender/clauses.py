"""条款树：把「块 ≈ 行」的解析产物聚成条款，给 bbox 一个语义边界。

口径见 `docs/规则/条款口径.md`（三条已裁决）：

- **起点档先探测再套优先级**：三份样本用了三套编号（`X.Y.Z` 主力 / `1.` 主力 /
  几乎不用 `X.Y`），写死一档必错。取命中率最高的两档当起点档，一档不到 2% 就
  退回字号规则。
- **跨页条款 bbox 取整条并集**，`page_start/page_end` 分开记，`continued=true`。
- **`（1）` 只在评标办法章算独立条款**，其他章是子项，归上一款。
- 没有起点的块 `kind=block`，照旧保留——宁可粗，不可丢。

这一层不调模型、不建索引，纯几何 + 编号。
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from tender.parse import body_size
from tender.sections import section_of_page

#: 优先顺序固定，探测只决定「哪些档算起点」，不改这个次序
BAND_ORDER = ("第X条", "X.Y", "（一）", "①", "1.", "一、", "（1）", "字号")

#: 一档编号至少要占多少块才算「主力」
MIN_BAND_SHARE = 0.02
#: 或者绝对命中达到这个数——稀疏但层级高的档（如「第X条」只出现 24 次）
#: 必须纳入，否则低档条款会把整章吸进自己肚子里
MIN_BAND_ABS = 5

_RE = {
    "第X条": re.compile(r"^第[一二三四五六七八九十百零0-9]{1,4}条(?![款项])"),
    # 段号每段限 1–2 位：`2263.76` 这种金额进不来；编号后是空白/中文，或**独占一行**
    # （招标文件常态：「11.1」换行后才是正文，漏掉行尾会丢掉一大半起点）
    "X.Y": re.compile(
        r"^(\d{1,2}(?:\.\d{1,2}){1,3})"
        r"(?=[\s\u4e00-\u9fa5（(【]|$)"
        r"(?!\s*(?:元|万|亿|[%％]|个|年|月|日|天|倍|人|份|页|分|米|吨))"
    ),
    "（一）": re.compile(r"^[（(][一二三四五六七八九十]{1,3}[）)]"),
    "①": re.compile(r"^[①-⑳]"),
    "1.": re.compile(r"^(\d{1,2}[.、])(?![\d%％元万亿个年月日天倍人份页])"),
    "一、": re.compile(r"^[一二三四五六七八九十]{1,3}[、]"),
    "（1）": re.compile(r"^[（(]\d{1,2}[）)]"),
}

#: 字号档：独立成行的短块，字号明显大于正文
TITLE_MAX_LEN = 40
TITLE_SIZE_SLACK = 1.1

#: 一条条款最多跨几页、吃几个块。超了就降级成 block 并断开。
#: 实测必要性：没有上限时，北京「3.项目联系方式」一路吞到第二章，442 块 11715 字。
MAX_CLAUSE_PAGES = 2
MAX_CLAUSE_BLOCKS = 60


@dataclass
class ClauseNode:
    """一条条款（或一段没起点的块）。契约之外的类型，只在本层流动。"""

    id: str
    kind: str          # clause | block
    band: str          # 命中的档位名，block 为空
    number: str        # 编号原文，block 为空
    section: str
    page_start: int
    page_end: int
    continued: bool
    bbox: list[float]
    text: str
    blocks: int = 1
    depth: int = 0
    parent_id: str | None = None   # 栈顶标题（父），由 heading_depth 建树得到

    @property
    def is_clause(self) -> bool:
        return self.kind == "clause"


#: 章标题
_CHAP = re.compile(r"^第[一二三四五六七八九十百零0-9]{1,4}章[\s]*[^\s，,。）)】、《]")


def _toc_depth(entry: dict) -> int | None:
    """书签层级 → 深度。质量闸在这里，不然坏书签会把整棵树带歪。

    - L1 且形如「第X章」→ 章（0）
    - L1 但不是章（`（一）适用范围`、`目 录`，Word 导出的书签常见）→ 降成节（1）
    - 其余 L 级 → level - 1
    - 纯数字、太短、没有汉字 → 丢弃
    """
    title = (entry.get("title") or "").strip()
    if len(_norm(title)) < 3 or not re.search(r"[\u4e00-\u9fa5]", title):
        return None
    level = int(entry.get("level", 1))
    if level == 1:
        return 0 if _CHAP.match(_norm(title)) else 1
    return max(1, level - 1)
#: 裸数字标题：`12 投标保证金`、`27 代理费`、`4 样品` —— 没有点号，但它是父
_BARE = re.compile(r"^(\d{1,2})\s*[\u4e00-\u9fa5][^\n]{0,18}$")
#: 固定深度的档位（X.Y 系按点号数动态算）
DEPTH_FIXED = {"1.": 1, "一、": 1, "（一）": 4, "（1）": 5, "①": 6}


def _norm(text: str) -> str:
    return re.sub(r"[\s\u3000]+", "", text or "")


def match_toc(toc: list[dict], pages: list[dict]) -> dict[tuple[int, int], tuple[int, str]]:
    """把书签匹配到具体文本块：`{(页码, 块序号): (depth, 标题)}`。

    书签是现成的层级树（章 → 一、二、三、），但书签里的文字和正文块常有空格、
    编号写法的差异，所以按「规范化后前缀匹配」认领。匹配不到就不硬塞——
    宁可退回编号推断，也不要凭空造一个父。
    """
    hits: dict[tuple[int, int], tuple[int, str]] = {}
    by_page = {p["page"]: p for p in pages}
    for entry in toc or []:
        target = _norm(entry.get("title"))
        depth = _toc_depth(entry)
        if not target or depth is None:
            continue
        base_page = int(entry.get("page") or 0)
        for pno in (base_page, base_page + 1):
            page = by_page.get(pno)
            if page is None:
                continue
            claimed = False
            for i, block in enumerate(page.get("blocks") or []):
                if (pno, i) in hits:
                    continue
                text = _norm(block.get("text"))
                # 只认「块以书签标题开头」：书签标题自带编号，正文块一般也是。
                # 放开成反向包含会误配——须知正文里提一句「投标文件格式」，
                # 就把第七章的书签挂到第 17 页上，后面整章的父链全错。
                if text and text.startswith(target):
                    hits[(pno, i)] = (depth, entry.get("title") or "")
                    claimed = True
                    break
            if claimed:
                break
    return hits


def heading_depth(block: dict, bands: list[str], body: float | None,
                  section_type: str | None = None) -> int | None:
    """块的层级深度；`None` 表示正文而不是标题。

    深度直接来自编号形式——标题从大到小本身就是父子关系，跟「它是不是一条
    条款」无关。裸数字标题（`12 投标保证金`）是深度 1，常常是 `12.1` 的父，
    早先版本把它当正文吞掉，父链就断了。
    """
    line = (block.get("text") or "").replace("\n", " ").strip()
    if not line:
        return None
    # 章标题：不能是「第五章《采购需求》），否则…」这种句首引用
    if _CHAP.match(line) and len(line) <= 30:
        return 0
    for band in BAND_ORDER:
        if band == "字号" or band not in bands:
            continue
        if band == "（1）" and section_type != "score":
            continue
        m = _RE[band].match(line)
        if not m:
            continue
        number = m.group(0).strip()
        if band == "X.Y":
            return 1 + number.count(".")          # 12.1 → 2，11.2.1 → 3
        return DEPTH_FIXED.get(band, 1)
    # 裸数字标题：短、独立成行、字号不小于正文
    if _BARE.match(line) and len(line) <= 20 and (not body or (block.get("max_size") or 0) >= body * 0.99):
        return 1
    return None


def _first_line(block: dict) -> str:
    return (block.get("text") or "").split("\n")[0].strip()


def _block_total(pages: list[dict]) -> int:
    return sum(len(p.get("blocks") or []) for p in pages)


def detect_numbering(pages: list[dict]) -> dict[str, int]:
    """逐块只看第一行，统计八档编号命中数。块内多行不重复计。"""
    counts = {band: 0 for band in BAND_ORDER if band != "字号"}
    counts["字号"] = 0
    body = body_size(pages)
    for page in pages:
        for block in page.get("blocks") or []:
            line = _first_line(block)
            if not line:
                continue
            hit = False
            for band, pattern in _RE.items():
                if pattern.match(line):
                    counts[band] += 1
                    hit = True
                    break
            if not hit and body and block.get("max_size", 0) >= body * TITLE_SIZE_SLACK:
                if "\n" not in (block.get("text") or "") and len(line) <= TITLE_MAX_LEN:
                    counts["字号"] += 1
    return counts


def pick_start_bands(counts: dict[str, int], total_blocks: int,
                     min_share: float = MIN_BAND_SHARE,
                     min_abs: int = MIN_BAND_ABS) -> list[str]:
    """纳入所有「够密」或「够多」的编号档；一档都没有时退回字号。

    实测教训：只取前两档会把「第X条」筛掉（北京只出现 24 次 = 0.8%），
    而它是层级最高的一档——不纳入的话，`12.3` 会把后面整章「第十三条 保密」
    吸成同一条（34 块、733 字）。所以绝对次数够了就必须纳入，不设档数上限。
    """
    total = max(total_blocks, 1)
    eligible = [(band, c) for band, c in counts.items()
                if band != "字号" and (c / total >= min_share or c >= min_abs)]
    eligible.sort(key=lambda kv: kv[1], reverse=True)
    if eligible:
        return [band for band, _ in eligible]
    return ["字号"]


def is_start(block: dict, bands: list[str], body: float | None,
             section_type: str | None = None) -> tuple[str, str] | None:
    """这个块是不是条款起点。返回 (档位, 编号原文)。"""
    line = _first_line(block)
    if not line:
        return None
    for band in BAND_ORDER:
        if band not in bands or band == "字号":
            continue
        # （1）在评标办法里是独立评分项，在其他章是子项
        if band == "（1）" and section_type != "score":
            continue
        m = _RE[band].match(line)
        if m:
            # group(0) 就是编号本身（后瞻不消耗字符）
            return band, m.group(0).strip()
    if "字号" in bands and body and block.get("max_size", 0) >= body * TITLE_SIZE_SLACK:
        text = block.get("text") or ""
        if "\n" not in text and len(line) <= TITLE_MAX_LEN:
            return "字号", ""
    return None


def _inside_any(box: list[float], boxes: list[list[float]]) -> bool:
    cx = (box[0] + box[2]) / 2
    cy = (box[1] + box[3]) / 2
    return any(b[0] <= cx <= b[2] and b[1] <= cy <= b[3] for b in boxes or [])


def _union(box: list[float], other: list[float]) -> list[float]:
    return [min(box[0], other[0]), min(box[1], other[1]),
            max(box[2], other[2]), max(box[3], other[3])]


def _overflowed(node: "ClauseNode") -> bool:
    """条款太长说明切分失控，不是条款真的这么长。"""
    return (node.page_end - node.page_start + 1) > MAX_CLAUSE_PAGES \
        or node.blocks >= MAX_CLAUSE_BLOCKS


def _absorb(node: "ClauseNode", block: dict, pno: int) -> None:
    node.text = f"{node.text}\n{block['text']}"
    node.bbox = _union(node.bbox, block["bbox"])
    node.blocks += 1
    if pno != node.page_end:
        node.page_end = pno
        node.continued = node.page_start != node.page_end


def _demote(node: "ClauseNode") -> None:
    """超限的条款降级成 block，别让它冒充一条条款去拿证据。"""
    node.kind = "block"
    node.band = "溢出"


#: 多级编号，如 5.1.1
_DOTTED = re.compile(r"^\d+(?:\.\d+){1,3}$")


def prefix_of(number: str) -> str:
    """编号前缀：`5.1.1` → `5.1`，`12.1` → `12`，非多级编号返回空。"""
    number = (number or "").strip()
    if not _DOTTED.match(number):
        return ""
    return ".".join(number.split(".")[:-1])


def link_parents(nodes: list["ClauseNode"]) -> None:
    """填 `parent_id`：先按编号前缀找祖先，找不到退到上一条条款。

    不按「档位等级」硬编码——同一份文件里 `第X条` 和 `12.1` 的层级关系
    因文件而异，前缀匹配 + 顺序兜底比维护一张等级表稳。
    """
    by_number = {}
    for node in nodes:
        if node.is_clause and _DOTTED.match((node.number or "").strip()):
            # 按「章 + 编号」索引：不同章都有 3.1/3.2，不加章会跨章连错父
            by_number.setdefault((node.section, node.number.strip()), node)

    prev = None
    for node in nodes:
        if not node.is_clause:
            continue
        parent = None
        parts = (node.number or "").strip().split(".")
        if len(parts) > 1 and _DOTTED.match(node.number.strip()):
            for cut in range(len(parts) - 1, 0, -1):
                cand = by_number.get((node.section, ".".join(parts[:cut])))
                if cand is not None and cand is not node:
                    parent = cand
                    break
        if parent is None and prev is not None and prev.section == node.section:
            parent = prev
        node.parent_id = parent.id if parent is not None else None
        prev = node


def _stem_of(pages: list[dict]) -> str:
    source = (pages[0].get("source") if pages else "") or "doc"
    return Path(source).stem


def build_clauses(pages: list[dict], sections: list[dict],
                  table_boxes: dict[int, list[list[float]]] | None = None,
                  stem: str | None = None, toc: list[dict] | None = None) -> list[ClauseNode]:
    """按口径把块聚成条款。

    `toc`（`parse.pdf_toc` 的产物）提供现成骨架：书签层级就是父子关系，优先于
    正文里的编号推断。`table_boxes` 里的块不参与聚合（表格单独走）。
    """
    if not pages:
        return []
    bands = pick_start_bands(detect_numbering(pages), _block_total(pages))
    body = body_size(pages)
    stem = stem or _stem_of(pages)
    toc_hits = match_toc(toc or [], pages)
    # 同一页出现多个「第X章」开头的块，那是目录页或前附表的章节索引，不是真章标题
    chap_per_page: dict[int, int] = {}
    for page in pages:
        chap_per_page[page["page"]] = sum(
            1 for b in page.get("blocks") or []
            if _CHAP.match((b.get("text") or "").replace("\n", " ").strip())
            and len((b.get("text") or "").replace("\n", " ").strip()) <= 30
        )

    nodes: list[ClauseNode] = []
    cur: ClauseNode | None = None
    stack: list[ClauseNode] = []      # 标题栈：栈顶就是当前块的父
    seq = 0

    for page in pages:
        pno = page["page"]
        sec = section_of_page(sections, pno) or {}
        stype = sec.get("type") or "unknown"
        boxes = (table_boxes or {}).get(pno) or []

        for bi, block in enumerate(page.get("blocks") or []):
            if boxes and _inside_any(block["bbox"], boxes):
                continue
            toc_hit = toc_hits.get((pno, bi))
            depth = toc_hit[0] if toc_hit else heading_depth(block, bands, body, stype)
            if depth == 0 and not toc_hit and chap_per_page.get(pno, 0) > 1:
                depth = None      # 目录页 / 前附表索引，不是真章标题
            if depth is not None:
                while stack and stack[-1].depth >= depth:
                    stack.pop()
                parent = stack[-1] if stack else None
                seq += 1
                is_chapter = depth == 0
                hit = is_start(block, bands, body, stype)
                cur = ClauseNode(
                    id=f"{stem}:c{seq:04d}",
                    kind="heading" if is_chapter else "clause",
                    band="章" if is_chapter else (toc_hit[1] if toc_hit else (hit[0] if hit else "裸数字")),
                    number="" if is_chapter else (hit[1] if hit else _first_line(block)[:16]),
                    section=stype, page_start=pno, page_end=pno, continued=False,
                    bbox=list(block["bbox"]), text=block["text"], depth=depth,
                    parent_id=parent.id if parent is not None else None,
                )
                nodes.append(cur)
                stack.append(cur)
                continue
            if cur is None:
                seq += 1
                cur = ClauseNode(
                    id=f"{stem}:c{seq:04d}", kind="block", band="", number="",
                    section=stype, page_start=pno, page_end=pno, continued=False,
                    bbox=list(block["bbox"]), text=block["text"],
                    depth=(stack[-1].depth if stack else 0),
                )
                nodes.append(cur)
                continue
            _absorb(cur, block, pno)
            if cur.kind != "heading" and _overflowed(cur):
                if cur.kind == "clause":
                    _demote(cur)
                cur = None
    return nodes


def stats(nodes: list[ClauseNode]) -> dict:
    """条款数、块级段数、跨页条数、各档分布——验收就看这几个数。"""
    by_band: dict[str, int] = {}
    for n in nodes:
        by_band[n.band or "无编号"] = by_band.get(n.band or "无编号", 0) + 1
    return {
        "total": len(nodes),
        "clause": sum(1 for n in nodes if n.is_clause),
        "heading": sum(1 for n in nodes if n.kind == "heading"),
        "block": sum(1 for n in nodes if not n.is_clause and n.kind != "heading"),
        "overflow": sum(1 for n in nodes if n.band == "溢出"),
        "parented": sum(1 for n in nodes if n.is_clause and n.parent_id),
        "continued": sum(1 for n in nodes if n.continued),
        "blocks": sum(n.blocks for n in nodes),
        "chars_median": sorted(len(n.text) for n in nodes)[len(nodes) // 2] if nodes else 0,
        "by_band": dict(sorted(by_band.items(), key=lambda kv: kv[1], reverse=True)),
    }


def dump(nodes: list[ClauseNode], folder: Path, stem: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{stem}.json"
    path.write_text(json.dumps([asdict(n) for n in nodes], ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return path
