"""事项卡：把一个事项散在各章的副本拼成一张卡（B4-b 的演示版，只做投标保证金）。

为什么需要它：同一个事项在招标文件里出现 1–6 章，**每章那个副本填的字段不同**。
北京投标保证金散在 4 章——公告/前附表给金额、须知正文给形式与到账要求、评标符合性给
无效后果、格式章给凭证模板。按「一行一条来源」落表，永远拼不出一张完整的卡。

这一版只做两件事：
1. **半穷尽**：在声明的章里正则命中「保证金 ∧ (投标无效|否决)」的条款**全进候选**，
   不截 top-8。这样「无效后果」这一格是扫过的，不是碰运气捞的。
2. **拼卡**：每个字段标来源（页码 + 角色），字段名固定，缺就标 `待人工`。

不调模型。字段值全部来自原文（前附表单元格 / 条款原文），不做改写。
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

#: 字段 → 期望的来源章（用于算「多源覆盖率」）
EXPECTED_LAYERS = ("invite", "notice", "score", "format")

#: 保证金相关
MUST = ("保证金",)
#: 无效后果词
CONSEQUENCE = ("投标无效", "否决", "无效投标", "不予", "废标", "拒绝")

_AMOUNT = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(万元|元|万)")
_DEPOSIT_NAMES = ("投标保证金金额", "投标保证金")

#: 不是投标保证金的近义串（履约/质量保证金常在同一章出现，串进来就是错）
_OTHER_BONDS = ("履约保证金", "质量保证金", "质保金", "农民工工资保证金")

#: 前附表里表示「本项目不要这笔钱」的说法
_NO_REQUIRE = ("不收取", "免收", "无需", "不需", "不要求", "不缴纳", "不交纳")


def _is_deposit(text: str) -> bool:
    """这一段说的是**投标**保证金，不是别的保证金。

    只看「保证金」三个字会把履约保证金捞进来——历史上 bj-neg-perf-bond 就是这么违规的。
    规则：出现「投标保证金」即算；只出现别的保证金名目则不算；两者都有时看语境。
    """
    if not text:
        return False
    if "投标保证金" in text:
        return True
    if any(w in text for w in _OTHER_BONDS):
        return False
    return "保证金" in text


@dataclass
class Field:
    """卡里的一个字段。"""

    value: str = ""
    status: str = "待人工"          # 有值 / 原文未写明 / 待人工
    refs: list[dict] = field(default_factory=list)   # [{page, bbox, role, text}]

    def add(self, value: str, page: int, bbox=None, role: str = "",
            text: str = "", layer: str = "") -> None:
        self.value = value
        self.status = "有值"
        self.refs.append({"page": page, "bbox": list(bbox or []), "role": role,
                          "text": text[:200], "layer": layer})


@dataclass
class MatterCard:
    """一个事项的完整卡片。"""

    name: str
    fields: dict[str, Field] = field(default_factory=dict)
    scanned: dict[str, int] = field(default_factory=dict)   # 章 → 扫过的条款数

    def coverage(self) -> str:
        """多源覆盖率：refs 覆盖了几章 / 期望几章。"""
        seen = set()
        for f in self.fields.values():
            for r in f.refs:
                if r.get("layer"):
                    seen.add(r["layer"])
        return f"{len(seen & set(EXPECTED_LAYERS))}/{len(EXPECTED_LAYERS)}"

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "coverage": self.coverage(),
            "scanned": self.scanned,
            "fields": {k: asdict(v) for k, v in self.fields.items()},
        }


def scan_clauses(index, section_types: list[str], must=MUST,
                 any_of=CONSEQUENCE) -> tuple[list[dict], dict[str, int]]:
    """半穷尽：把声明章里满足「全含 must ∧ 任一 any_of」的条款**全部**返回。

    和检索的区别：检索取 top-k（丢掉其余），这里是**过滤**——扫到的都留着。
    返回 (hits, 每章扫过的条款数)。每章扫过多少条是「可证明覆盖」的依据。
    """
    hits: list[dict] = []
    scanned: dict[str, int] = {}
    for typ in section_types:
        store = index.stores.get(typ)
        if store is None:
            continue
        chunks = getattr(store, "chunks", []) or []
        scanned[typ] = len(chunks)
        for c in chunks:
            text = c.get("text") or ""
            if not _is_deposit(text):
                continue
            if not all(k in text for k in must):
                continue
            if not any(k in text for k in any_of):
                continue
            # 「…投标保证金除外」是排除条款，不是无效后果。只有带「否则/不予」的才算。
            if "除外" in text and "否则" not in text and "不予" not in text:
                continue
            hits.append({
                "text": text,
                "page": c.get("page"),
                "bbox": c.get("bbox") or [],
                "clause_id": c.get("clause_id") or "",
                "layer": c.get("section_type") or typ,
            })
    return hits, scanned


def _front_value(fronts, names: tuple[str, ...]) -> tuple[str, dict] | tuple[None, None]:
    """从前附表里取第一个名称命中的值。"""
    for fm in fronts or []:
        for name in names:
            it = fm.first(name)
            if it is not None and it.value:
                return it.value, {"page": it.page, "bbox": it.bbox, "role": "前附表"}
    return None, None


def build_deposit_card(index, fronts) -> MatterCard:
    """拼「投标保证金」卡。

    四个字段各自的权威来源不同：
    - amount   前附表「投标保证金金额」（没有就退正则扫公告）
    - method   须知正文里含「形式/交纳/缴纳」的条款
    - verdict  半穷尽：保证金 ∧ 后果词
    - evidence 格式章里的担保函/凭证模板
    """
    card = MatterCard(name="投标保证金", fields={
        "amount": Field(), "method": Field(), "verdict": Field(), "evidence": Field(),
    })

    # ---- amount：前附表优先
    val, ref = _front_value(fronts, _DEPOSIT_NAMES)
    if val:
        card.fields["amount"].add(val, ref["page"], ref["bbox"], "前附表", layer="notice")
    else:
        # 「不收取」也算金额信息——温岭写在公告里：「（二）投标保证金：本项目不收取投标保证金。」
        hits, scanned = scan_clauses(index, ["invite", "notice"], must=MUST,
                                     any_of=("金额", "元", "万") + _NO_REQUIRE)
        card.scanned.update({f"amount:{k}": v for k, v in scanned.items()})
        for h in hits:
            m = _AMOUNT.search(h["text"])
            if m:
                card.fields["amount"].add(m.group(0), h["page"], h["bbox"], "正则",
                                          layer=h.get("layer") or "")
                break
            # 「不收取」这类没有数字，但**它本身就是金额字段的结论**，不能当没找到
            if any(w in h["text"] for w in _NO_REQUIRE):
                card.fields["amount"].add(h["text"][:60], h["page"], h["bbox"], "原文",
                                          layer=h.get("layer") or "")
                break

    # 前附表写明「不收取」时，其余字段不是「没抽到」，而是「原文不要求」——
    # 这两件事必须分开标，否则「待人工」会让人以为我们漏了。
    amount_val = card.fields["amount"].value or ""
    if any(w in amount_val for w in _NO_REQUIRE):
        for k in ("method", "verdict", "evidence"):
            card.fields[k].status = "原文未要求"
            card.fields[k].value = amount_val
        return card

    # ---- method：正文里的交纳方式
    method_hits, scanned = scan_clauses(
        index, ["notice", "invite"], must=MUST,
        any_of=("形式", "交纳", "缴纳", "提交", "到账", "递交"),
    )
    card.scanned.update({f"method:{k}": v for k, v in scanned.items()})
    for h in method_hits:
        card.fields["method"].add(h["text"], h["page"], h["bbox"], "条款", h["text"],
                                  layer=h.get("layer") or "")

    # ---- verdict：半穷尽，后果句全进
    verdict_hits, scanned = scan_clauses(index, ["notice", "score", "qualify"], must=MUST)
    card.scanned.update({f"verdict:{k}": v for k, v in scanned.items()})
    for h in verdict_hits:
        card.fields["verdict"].add(h["text"], h["page"], h["bbox"], "无效句", h["text"],
                                   layer=h.get("layer") or "")

    # ---- evidence：格式章的凭证模板
    ev_hits, scanned = scan_clauses(
        index, ["format"], must=("保证金",),
        any_of=("担保函", "凭证", "保函", "格式"),
    )
    card.scanned.update({f"evidence:{k}": v for k, v in scanned.items()})
    for h in ev_hits:
        card.fields["evidence"].add(h["text"], h["page"], h["bbox"], "格式", h["text"],
                                    layer=h.get("layer") or "")

    return card


def dump(cards, folder: Path | str, stem: str) -> Path:
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{stem}.matters.json"
    path.write_text(json.dumps([c.to_dict() for c in cards], ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return path
