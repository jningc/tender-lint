"""条款树：把 parse 的文本块聚成标题/条款节点（检索 chunk 的边界）。

不调模型、不建索引。主逻辑：

- **起点档先探测**：按文档统计编号命中，够密或够多的档都算起点（否则退回字号）；
  写死单一档位会在三份样本上翻车。
- **层级 = 编号族 + 栈**：同族同级、新族作栈顶子级；书签可辅助，块自带编号时以族为准。
- **表/前附表 bbox 内的块跳过**（由调用方传入 `table_boxes`），避免稀疏条款号进树。
- **跨页**：`page_start/page_end` + bbox 并集，`continued=true`；过长只标 `truncated`，不删节点。
- **`（1）` 仅在评标办法章**可作起点；无起点的块 `kind=block`，宁可粗不可丢。

口径细节见 `docs/规则/条款口径.md`（其中「只取两档」已过时，以本模块为准）。
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from tender.parse import body_size
from tender.sections import section_of_page

#: 优先顺序固定，探测只决定「哪些档算起点」，不改这个次序
BAND_ORDER = ("第X条", "X.Y", "（一）", "①", "1.", "一、", "一 空格", "（1）", "字号")

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
        r"(?!\s*(?:元|万|亿|[%％]|个|年|月|日|天|倍|人|份|页|分|米|吨|"
        r"公里|千米|公顷|亩|平方米|立方米|株|棵|头|辆|套|件|台|座|处|次|户|家|名))"
    ),
    "（一）": re.compile(r"^[（(][一二三四五六七八九十]{1,3}[）)]"),
    "①": re.compile(r"^[①-⑳]"),
    "1.": re.compile(r"^(\d{1,2}[.、])(?![\d%％元万亿个年月日天倍人份页])"),
    "一、": re.compile(r"^[一二三四五六七八九十]{1,3}[、]"),
    # 空格写法：「一   说  明」「二   招标文件」——北京第二章整章都用这个，
    # 只认顿号会让第二章从「投标人须知资料表」直接跳到 2.2，中间 20 多条全丢。
    # 要求 ≥2 个空格，正文「一 般」只有一个，不会被误判。
    "一 空格": re.compile(r"^[一二三四五六七八九十]{1,3}[\s\u3000]{2,}[\u4e00-\u9fa5]"),
    "（1）": re.compile(r"^[（(]\d{1,2}[）)]"),
}

#: 字号档：独立成行的短块，字号明显大于正文
TITLE_MAX_LEN = 40
TITLE_SIZE_SLACK = 1.1

#: 一条条款最多跨几页、吃几个块。超了就断开并标 truncated（节点仍留在目录里）。
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
    truncated: bool = False        # 吃太多被断开（内容不全，但节点仍在目录里）
    depth: int = 0
    family: str = ""               # 编号族（cn/digit/decimal1/...），同族才同级
    parent_id: str | None = None   # 栈顶标题（父），由族 + 栈建树得到

    @property
    def is_clause(self) -> bool:
        return self.kind == "clause"


#: 章标题
_CHAP = re.compile(r"^第[一二三四五六七八九十百零0-9]{1,4}章[\s]*[^\s，,。）)】、《]")

#: 表/附件类标题——书签常把它们标成 L2，但它们不是结构层级。
#: 实测：崇左书签把「…项目分标表」标成 d1，把后续 `二、投标人的资格要求`
#: 挤到 d3，整章的层级全歪。宁可少一个节点，也不给它错层级。
_NON_STRUCTURAL = re.compile(r"(表|清单|附件)$")


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
# 汉字至少两个：「3 年」这类正文数字不是标题，「4 样品」「12 投标保证金」才是。
# 尾部放宽到 30 字：`2 资金来源、项目属性、科研仪器设备采购、核心产品` 有 24 字，
# 卡在 20 会让它掉到书签分支，书签的 L 级又把它排错一层（实测北京 2/3/4 缩进错位）。
_BARE = re.compile(r"^(\d{1,2})\s*[\u4e00-\u9fa5]{2,}[^\n]{0,30}$")

#: 编号族——决定「谁和谁同级」。写死深度（旧 DEPTH_FIXED）会把 `1.` 与 `一、`
#: 判成同级、把 `（一）` 跳到 d4。族 ≠ 深度：深度由 `_pop_to_family()` 推导。
FAMILY_OF_BAND = {
    "第X条": "article",
    "一、": "cn",            # 一、二、三、（含「一   说明」空格写法）
    "一 空格": "cn",
    "1.": "digit",           # 1. 2. 3.
    "（一）": "cn_paren",     # （一）
    "（1）": "digit_paren",   # （1）
    "①": "circled",
    "裸数字": "bare",
    "字号": "size",
}


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


def heading_family(block: dict, bands: list[str], body: float | None,
                   section_type: str | None = None) -> tuple[str, str] | None:
    """块的 (编号族, 编号原文)；正文返回 `None`。

    族只回答「这两个标题同类吗」，不回答「它多深」——因为同一个 `1.` 在不同
    位置深度不同：跟在 `一、` 后面时它是子级，单独出现时它是顶层。
    深度由 `_pop_to_family()` 用栈推导。
    """
    line = (block.get("text") or "").replace("\n", " ").strip()
    if not line:
        return None
    if _CHAP.match(line) and len(line) <= 30:
        return "chapter", ""
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
            # 点号数就是族内层级：`3.1` 与 `3.1.1` 是两个族，自动嵌套
            return f"decimal{number.count('.')}", number
        return FAMILY_OF_BAND[band], number
    m = _BARE.match(line)
    if m:
        # 长度只算到第一个冒号/句号——裸数字标题后面常紧跟内容
        # （`27 代理费 收费对象：□采购人 ■中标供应商`），全行长度没有意义
        head = re.split(r"[：:。；;]", line)[0]
        if len(head) <= 26 and (not body or (block.get("max_size") or 0) >= body * 0.99):
            return "bare", m.group(1)
    return None


#: family → 展示用的 band 名（族是内部概念，报告里还是印「一、」「X.Y」这些）
_BAND_OF_FAMILY = {v: k for k, v in FAMILY_OF_BAND.items()}


def _band_of(family: str) -> str:
    if family.startswith("decimal"):
        return "X.Y"
    if family.startswith("toc"):
        return "书签"
    return _BAND_OF_FAMILY.get(family, family)


def _pop_to_family(stack: list["ClauseNode"], family: str) -> int:
    """按族回溯，返回新标题的深度。

    - 栈里有同族 → 回到它那一层，深度相同（同级），把同族及更深的都弹掉
    - 栈里没有 → 这是新族，作栈顶的子级（深度 +1）

    实测效果：`一、` 后的 `1.` 深度从 1 变 2（北京不再同级错位）；
    温岭 `二、` 后的 `（一）` 从 4 变 2（不再跳级）。
    """
    for i in range(len(stack) - 1, -1, -1):
        if stack[i].family == family:
            depth = stack[i].depth
            del stack[i:]
            return depth
    return (stack[-1].depth + 1) if stack else 0


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


def _cut(node: "ClauseNode") -> None:
    """超限时断开并标记截断，**不降级**。

    旧实现把超限节点降成 `block`，副作用是结构节点从目录里凭空消失——崇左
    「一、项目基本情况」因为吸到 p3–p5（跨 3 页 > `MAX_CLAUSE_PAGES`）就被抹掉了，
    目录里根本看不到这一节。宁可标「内容截断」，也不能让它不见了。
    """
    node.truncated = True


def _stem_of(pages: list[dict]) -> str:
    source = (pages[0].get("source") if pages else "") or "doc"
    return Path(source).stem


def build_clauses(pages: list[dict], sections: list[dict],
                  table_boxes: dict[int, list[list[float]]] | None = None,
                  stem: str | None = None, toc: list[dict] | None = None) -> list[ClauseNode]:
    """按口径把块聚成条款。

    层级以编号族+栈为准；书签可辅助贴标题，但块自带编号时仍走族。
    `table_boxes` 内的块跳过（评分表/前附表等，表格单独走）。
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
            fam: str | None = None
            number = ""
            depth = 0

            if toc_hit:
                # 书签是权威信号（Docling 也是把它排第一）：深度照书签，栈调到那层。
                # 但表/附件类标题（「分标表」「投标人须知资料表」）不建层级节点——
                # 书签常把它们标成 L2，一旦入栈就会把后面的 `二、` 挤深两级。
                title = _norm(toc_hit[1] or "")
                if toc_hit[0] > 0 and _NON_STRUCTURAL.search(title) and not _RE["X.Y"].match(title):
                    fam = None
                else:
                    hf = heading_family(block, bands, body, stype)
                    if hf is not None and hf[0] != "size":
                        # 块自带编号时以编号族为准：Word 导出的书签常把
                        # 「一 说明」和「1采购人…」都标 L2，照抄就会把子级排成兄弟。
                        fam, number = hf
                        depth = _pop_to_family(stack, fam)
                    else:
                        depth = toc_hit[0]
                        fam = f"toc{depth}"
                        while stack and stack[-1].depth >= depth:
                            stack.pop()
            else:
                hf = heading_family(block, bands, body, stype)
                if hf is not None:
                    fam, number = hf
                    if fam == "chapter" and chap_per_page.get(pno, 0) > 1:
                        fam = None        # 目录页 / 前附表里的章节索引，不是真章标题
                    elif fam == "chapter":
                        depth = 0
                    else:
                        depth = _pop_to_family(stack, fam)

            if fam is not None:
                parent = stack[-1] if stack else None
                seq += 1
                is_chapter = fam == "chapter"
                cur = ClauseNode(
                    id=f"{stem}:c{seq:04d}",
                    kind="heading" if is_chapter else "clause",
                    band="章" if is_chapter else (toc_hit[1] if toc_hit else _band_of(fam)),
                    number="" if is_chapter else (number or _first_line(block)[:16]),
                    section=stype, page_start=pno, page_end=pno, continued=False,
                    bbox=list(block["bbox"]), text=block["text"],
                    depth=depth, family=fam,
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
                _cut(cur)       # 标记截断，节点留在目录里
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
        "overflow": sum(1 for n in nodes if n.truncated),
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
