"""把解析层已经拿到的「结构化成果」接到出表文本里。

背景：前附表（65 条）和保证金事项卡（4 字段）做完了，但**只有 eval 脚本能跑**，
主流程和界面不知道它们存在。这个模块负责把它们摊到 `render_tables()` 的输出末尾。

不调模型、不改任何现有表的逻辑——只是追加两块。
"""

from __future__ import annotations

from tender.frontmatter import extract as extract_frontmatter
from tender.matters import build_deposit_card
from tender.sections import section_of_page


def collect(index) -> dict:
    """从已建好的索引里收集「额外成果」。

    输入只需要 `index`：它已经带 `sections` 和 `tables`（解析阶段落的）。
    """
    if index is None:
        return {}
    secs = getattr(index, "sections", []) or []
    tables = getattr(index, "tables", []) or []

    types: dict[int, str | None] = {}
    for t in tables:
        pages = getattr(t, "pages", None) or []
        if pages:
            types[pages[0]] = (section_of_page(secs, pages[0]) or {}).get("type")

    fronts = extract_frontmatter(tables, types)
    card = build_deposit_card(index, fronts) if hasattr(index, "stores") else None
    return {
        "frontmatter": [f.dump() for f in fronts],
        "matter": card.to_dict() if card is not None else None,
    }


def render(extras: dict, limit: int = 12) -> list[str]:
    """渲染成纯文本行。`limit` 只截前附表明细（总数照报）。"""
    lines: list[str] = []
    fronts = extras.get("frontmatter") or []
    if fronts:
        total = sum(len(f.get("items") or []) for f in fronts)
        lines.append(f"\n== 前附表 {total} 条（零模型直读） ==")
        shown = 0
        for f in fronts:
            for it in f.get("items") or []:
                if shown >= limit:
                    break
                val = str(it.get("value") or "").replace("\n", " ")
                if len(val) > 70:
                    val = val[:70] + "…"
                lines.append(f"  p{it.get('page')}  {it.get('name')} | {val}")
                shown += 1
        if total > shown:
            lines.append(f"  …另有 {total - shown} 条")

    matter = extras.get("matter")
    if matter:
        lines.append(f"\n== 事项卡：{matter.get('name')}（多源覆盖 {matter.get('coverage')} 章） ==")
        for key, label in (("amount", "金额"), ("method", "交纳方式/到账"),
                           ("verdict", "无效后果"), ("evidence", "凭证/担保函")):
            f = (matter.get("fields") or {}).get(key) or {}
            status = f.get("status") or ""
            value = str(f.get("value") or "").replace("\n", " ")
            if len(value) > 68:
                value = value[:68] + "…"
            pages = sorted({r.get("page") for r in (f.get("refs") or []) if r.get("page")})
            tail = "  [p" + ",".join(str(x) for x in pages) + "]" if pages else ""
            flag = "✓" if status == "有值" else ("·" if status == "原文未要求" else "?")
            lines.append(f"  {flag} {label}：{value or '（待人工）'}{tail}"
                         + (f"  ← {status}" if status and status != "有值" else ""))
    return lines
