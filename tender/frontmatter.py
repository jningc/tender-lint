"""前附表（投标人须知前附表 / 资料表）直读：三列键值表，零模型。

为什么单独做这一层：招标文件里最关键的一批参数**不在正文，而在前附表**——
投标有效期、保证金金额、是否接受联合体、解密时限、各类文件的组成清单。
正文只写「见前附表」，前附表才是权威值。

形态是三列表：`条款号 | 项目内容 | 编列内容`（崇左）或 `序号 | 项目 | 内容`（温岭）。
结构规整，`tabular.py` 已经能抽出来，这里只负责：

1. 判断哪张表是前附表（表头 + 章类型 + 行数）
2. 把行变成 `{条款号, 项目, 值}` 三元组
3. 给出按关键词查找的接口（`find("投标有效期")`）

**不调模型。** 已由 `parse_stage` 排除出条款树，并经 `to_chunks` 并入索引。
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

#: 第 1 列（条款号/序号）
NO_KEYS = ("条款号", "序号", "编号", "项号", "条款")
#: 第 2 列（项目名）
NAME_KEYS = ("项目内容", "项目名称", "项目", "名称", "条款名称", "内容摘要", "条目")
#: 第 3 列（编列内容）
VALUE_KEYS = ("编列内容", "内容", "要求", "编列内容/要求", "约定内容")

#: 前附表至少这么多行，否则是别的小表
MIN_ROWS = 5

#: 值里要保留的勾选符号（☑ 选中 / □ 未选），它们本身是语义
_WS = re.compile(r"[ \t\u3000]+")


def _head(table, i: int) -> str:
    """取表头第 i 列（两级表头时取最细一级）。"""
    levels = getattr(table, "header_levels", None) or [getattr(table, "header", []) or []]
    for level in reversed(levels):
        if i < len(level) and str(level[i]).strip():
            return str(level[i]).strip()
    return ""


def _match(header: str, keys: tuple[str, ...]) -> bool:
    """表头是否命中。用 startsWith 或包含，但排除过长的表头（那是正文误当表头）。"""
    h = header.replace("☑", "").replace("□", "").strip()
    if not h or len(h) > 12:
        return False
    return any(k in h for k in keys)


def is_frontmatter(table, section_type: str | None = None) -> bool:
    """判断一张表是不是前附表。

    判据（都要满足）：
    - 3 列（温岭、崇左都是 3 列；个别版本 4 列）
    - 表头三列分别命中「条款号 / 项目 / 编列内容」三组词
    - 行数 ≥ 5
    - 所在章是 notice（前附表都在投标人须知里）；章类型未知时放宽
    """
    if getattr(table, "col_count", 0) not in (3, 4):
        return False
    if len(getattr(table, "rows", []) or []) < MIN_ROWS:
        return False
    h0, h1, h2 = _head(table, 0), _head(table, 1), _head(table, 2)
    if not (_match(h0, NO_KEYS) and _match(h1, NAME_KEYS) and _match(h2, VALUE_KEYS)):
        return False
    if section_type and section_type != "notice":
        return False
    return True


@dataclass
class FrontItem:
    """前附表的一行。"""

    no: str          # 条款号 / 序号（可能合并成 "11.311.4"）
    name: str        # 项目（「投标有效期」）
    value: str       # 编列内容（「自投标截止之日起90日。」）
    page: int
    bbox: list[float] = field(default_factory=list)

    def as_line(self) -> str:
        return f"{self.name}：{self.value}"


@dataclass
class FrontMatter:
    """一张前附表的解析结果。"""

    table_id: str
    pages: list[int]
    items: list[FrontItem] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.items)

    def to_dict(self) -> dict[str, str]:
        """名称 → 值。重名时用条款号区分（「13.1 资格证明文件组成」等四行同号）。"""
        out: dict[str, str] = {}
        for it in self.items:
            key = it.name
            if key in out:
                key = f"{key}#{it.no}" if it.no else f"{key}#{len(out)}"
            out[key] = it.value
        return out

    def find(self, *keywords: str) -> list[FrontItem]:
        """按名称包含关键词查（多个关键词是「都要包含」）。"""
        hits = []
        for it in self.items:
            if all(k in it.name for k in keywords):
                hits.append(it)
        return hits

    def first(self, *keywords: str) -> FrontItem | None:
        hits = self.find(*keywords)
        return hits[0] if hits else None

    def dump(self) -> dict:
        return {
            "table_id": self.table_id,
            "pages": self.pages,
            "items": [asdict(it) for it in self.items],
        }

    @classmethod
    def load(cls, data: dict) -> "FrontMatter":
        return cls(
            table_id=data.get("table_id") or "",
            pages=data.get("pages") or [],
            items=[FrontItem(**it) for it in data.get("items") or []],
        )


def _clean(s: str) -> str:
    s = (s or "").replace("\n", " ").strip()
    return _WS.sub(" ", s)


def from_table(table) -> FrontMatter:
    """把一张已识别的三列表转成 FrontMatter。

    值列如果被拆成多列（4 列表），用空格拼回去——**不丢字**。
    """
    items: list[FrontItem] = []
    last_no = ""
    for row in getattr(table, "rows", []) or []:
        cells = [_clean(c) for c in (row.cells or [])]
        if len(cells) < 3:
            continue
        no = cells[0]
        name = cells[1]
        value = " ".join(c for c in cells[2:] if c).strip()
        if not name:
            continue                       # 没有项目名，不是有效行
        if not no:
            no = last_no                   # 合并单元格：沿用上一行条款号
        else:
            last_no = no
        items.append(FrontItem(no=no, name=name, value=value,
                               page=row.page, bbox=list(row.bbox or [])))
    return FrontMatter(table_id=getattr(table, "id", ""),
                       pages=list(getattr(table, "pages", []) or []),
                       items=items)


def to_chunks(fms: list[FrontMatter], sections: list[dict] | None = None,
              source: str = "doc.pdf", heights: dict[int, float] | None = None) -> list[dict]:
    """把前附表条目转成索引 chunk，和条款 chunk 进同一个库。

    为什么需要这个：前附表被排除出条款树（它是稀疏引用表，进树会让编号乱序），
    但排除的代价是**内容从索引里消失**——北京「01包：8000元」只在 p8 附表现，
    实测 chunk 里搜不到 `8000`，等于关键金额丢了。

    所以前附表走独立的 chunk：一条一行，`kind="frontmatter"`，不参与建树。
    表级事实（总条数）每条都带一份——和评分表 small chunk 同一个道理：
    问「保证金多少」时，单条里没有「这是一张 17 条的对照表」这个信息。
    """
    heights = heights or {}
    sec_by_page = {}
    for s in sections or []:
        for pno in range(int(s.get("start_page") or 0), int(s.get("end_page") or 0) + 1):
            sec_by_page.setdefault(pno, s)

    chunks: list[dict] = []
    for fm in fms:
        total = len(fm.items)
        for it in fm.items:
            sec = sec_by_page.get(it.page) or {}
            # 值里常自带名称前缀（`项目属性：■服务 □货物`），再拼一次就成
            # 「项目属性：项目属性：■服务」了
            val = (it.value or "").strip()
            body = val if val.startswith(it.name) else f"{it.name}：{val}"
            text = f"【前附表】{it.no} {it.name}\n{body}" if it.no \
                else f"【前附表】{it.name}\n{body}"
            chunks.append({
                "text": text,
                # context 带表级事实：这条属于一张多少条的对照表
                "context": f"（本表 {total} 条，列的是对本项目须知的补充、修改与取值）\n{text}",
                "page": it.page,
                "page_height": heights.get(it.page) or 842.0,
                "source": source,
                "section_type": sec.get("type") or "notice",
                "section_types": sec.get("types") or [sec.get("type") or "notice"],
                "section_title": sec.get("title") or "",
                "bbox": list(it.bbox),
                "kind": "frontmatter",
                "clause_id": f"{source}:fm:{it.no or len(chunks)}",
                "number": it.no,
            })
    return chunks


def extract(tables: list, section_types: dict[int, str] | None = None) -> list[FrontMatter]:
    """从一批表里找出所有前附表。

    `section_types`：页码 → 章类型（`sections.section_of_page` 的结果）。
    """
    out = []
    for t in tables or []:
        st = None
        if section_types:
            st = section_types.get((getattr(t, "pages", []) or [None])[0])
        if is_frontmatter(t, st):
            out.append(from_table(t))
    return out


def dump(fronts: list[FrontMatter], folder: Path | str, stem: str) -> Path:
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{stem}.frontmatter.json"
    path.write_text(json.dumps([f.dump() for f in fronts], ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return path


def load(folder: Path | str, stem: str) -> list[FrontMatter]:
    path = Path(folder) / f"{stem}.frontmatter.json"
    if not path.is_file():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return [FrontMatter.load(d) for d in data]
