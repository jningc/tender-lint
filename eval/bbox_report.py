"""bbox 口径统计：每条 ref 的框是真框还是整页框，以及「条款级」占比。

用法:
  python eval/bbox_report.py eval/engine/v2                 # 单份目录（<doc>/tables.json）
  python eval/bbox_report.py /tmp/before_bbox eval/engine/v2 # 前后对比

三档口径（互斥，按严重程度排）:
  none  无 bbox
  page  有 bbox 但高度 > 60% 页高（整页框，指不到具体位置）
  block 块级框（可定位到页内某一处）

另有两个**覆盖指标**（不互斥，只对 block 打分）:
  clause     框是否被条款树里某条真条款的 bbox 包住
  table_row  评分表的 ref 是否落在已识别表格的某一行里
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TABLES = ("feibiao", "pingfen", "cailiao", "kongwei", "shijian", "hetong")
PAGE_HEIGHT = 842.0  # A4；真实页高由索引带出，这里只作判定阈值
CLAUSE_TREES = ROOT / "data" / "clause_tree"
INDEX_ROOT = ROOT / "data" / "tender_index"
#: bbox 包含判定的容差（pt）：ref 和条款的框来自不同抽取层，边缘会差一两像素
CONTAIN_TOL = 2.0
#: v2 的 findings 用中文表名
CN2EN = {"废标对照": "feibiao", "评分对照": "pingfen", "材料对照": "cailiao",
         "空位对照": "kongwei", "时间节点": "shijian", "合同要点": "hetong"}


def _contains(outer: list, inner: list) -> bool:
    if len(outer) != 4 or len(inner) != 4:
        return False
    return (outer[0] - CONTAIN_TOL <= inner[0] and outer[1] - CONTAIN_TOL <= inner[1]
            and outer[2] + CONTAIN_TOL >= inner[2] and outer[3] + CONTAIN_TOL >= inner[3])


def load_clause_boxes(doc: str) -> dict[int, list[list]]:
    """{页: [条款 bbox, ...]}，只收 `kind=clause` 的真条款。

    跨页条款的 bbox 是整条并集（首页坐标），拿去判下几页的 ref 会覆盖不到——
    这是保守方向：漏判成「没覆盖」，不会把没覆盖的算成覆盖。
    """
    path = CLAUSE_TREES / f"{doc}.json"
    if not path.is_file():
        return {}
    out: dict[int, list[list]] = {}
    for node in json.loads(path.read_text(encoding="utf-8")):
        if node.get("kind") != "clause":
            continue
        box = node.get("bbox")
        if not box or len(box) != 4:
            continue
        for page in range(node.get("page_start") or 0, (node.get("page_end") or 0) + 1):
            out.setdefault(page, []).append(box)
    return out


def load_row_boxes(doc: str) -> list[dict]:
    """评分表每一行的 bbox（`{page, bbox}`），判 ref 有没有落到结构化行里。"""
    path = INDEX_ROOT / doc / f"{doc}.json"
    if not path.is_file():
        return []
    out = []
    for table in json.loads(path.read_text(encoding="utf-8")):
        if table.get("kind") != "pingfen":
            continue
        for row in table.get("rows") or []:
            box = row.get("bbox")
            if box and len(box) == 4:
                out.append({"page": row.get("page"), "bbox": box, "table": table.get("id")})
    return out


def covered_by(ref: dict, boxes_by_page: dict[int, list[list]]) -> bool:
    """ref 的框有没有被某条真条款的框包住（纯几何判定）。

    **不看 evidence 自带的 `clause` 字段**：那个字段是抽取时绑的条款号，
    有它就必然 100%，量不出「框指没指到条款里」。这里要的是几何包含。
    """
    box = ref.get("bbox") or []
    for outer in boxes_by_page.get(ref.get("page")) or []:
        if _contains(outer, box):
            return True
    return False


def row_level_stats(doc: str) -> dict:
    """评分表的行级 bbox 是否成立：每行有没有自己的框、框是不是行级（不是整页）。

    v2 的 findings 只覆盖废标/材料/合同（那三张有「符合/不符合」判定），
    评分表不产 finding，所以没有 refs 可以往行上套。这里直接量结构本身：
    行数、有多少行带有效 bbox、最大行高占页高多少。
    """
    path = INDEX_ROOT / doc / f"{doc}.json"
    if not path.is_file():
        return {"rows": 0, "with_bbox": 0, "max_h_ratio": 0.0}
    rows = []
    for table in json.loads(path.read_text(encoding="utf-8")):
        if table.get("kind") == "pingfen":
            rows += table.get("rows") or []
    if not rows:
        return {"rows": 0, "with_bbox": 0, "max_h_ratio": 0.0}
    with_bbox = sum(1 for r in rows if len(r.get("bbox") or []) == 4)
    max_ratio = max((r["bbox"][3] - r["bbox"][1]) / PAGE_HEIGHT
                    for r in rows if len(r.get("bbox") or []) == 4) if with_bbox else 0.0
    return {"rows": len(rows), "with_bbox": with_bbox, "max_h_ratio": round(max_ratio, 3)}


def in_row(ref: dict, rows: list[dict]) -> bool:
    box = ref.get("bbox") or []
    for row in rows:
        if row["page"] == ref.get("page") and _contains(row["bbox"], box):
            return True
    return False


def classify(refs: list[dict], page_heights: dict[int, float] | None = None) -> dict:
    """逐条 ref 归档：none / page / block。"""
    out = {"none": 0, "page": 0, "block": 0}
    heights = page_heights or {}
    for ref in refs or []:
        box = ref.get("bbox") or []
        if len(box) != 4:
            out["none"] += 1
            continue
        h = box[3] - box[1]
        out["page" if h > (heights.get(ref.get("page")) or PAGE_HEIGHT) * 0.6 else "block"] += 1
    return out


def doc_stats(payload: dict, page_heights: dict[int, float] | None = None) -> dict:
    doc = payload.get("doc") or ""
    clause_boxes = load_clause_boxes(doc)
    rows_struct = row_level_stats(doc)
    per_table = {}
    total = {"none": 0, "page": 0, "block": 0, "clause": 0, "refs": 0}
    for t in TABLES:
        refs = [r for row in (payload.get("tables") or {}).get(t) or []
                for r in (row.get("refs") or [])]
        c = classify(refs, page_heights)
        clause_hit = sum(1 for r in refs if covered_by(r, clause_boxes))
        n = sum(c.values())
        per_table[t] = {**c, "clause": clause_hit, "refs": n}
        for k in ("none", "page", "block", "clause"):
            total[k] += per_table[t][k]
        total["refs"] += n
    refs = total["refs"]
    total["clause_level"] = round(total["clause"] / refs, 3) if refs else 0.0
    total["table_rows"] = rows_struct
    return {"doc": doc, "total": total, "per_table": per_table}


def load_dir(folder: Path) -> dict[str, dict]:
    """读六张表的行 + refs。

    优先 v2 的 `compare.findings.json`（`<doc>/v2/`）：它的 `evidence` 就是 refs，
    而且自带 `clause` 字段（v2 已经做了条款关联），比事后拿 bbox 去套条款树准。
    没有再退回 v1 的 `tables.json`。
    """
    out = {}
    for f in sorted(folder.glob("*/v2/compare.findings.json"), key=lambda p: p.stat().st_mtime):
        doc = f.parent.parent.name
        tables: dict[str, list] = {}
        for item in json.loads(f.read_text(encoding="utf-8")):
            key = CN2EN.get(item.get("table") or "")
            if key:
                tables.setdefault(key, []).append({"refs": item.get("evidence") or []})
        out[doc] = {"doc": doc, "tables": tables, "source": "v2"}
    if out:
        return out
    for f in sorted(folder.glob("**/tables.json"), key=lambda p: p.stat().st_mtime):
        payload = json.loads(f.read_text(encoding="utf-8"))
        out[payload.get("doc") or f.parent.name] = payload
    return out


def page_heights_of(doc: str) -> dict[int, float]:
    """从 PDF 抽真实页高；抽不到就用 A4 默认。"""
    try:
        sys.path.insert(0, str(ROOT))
        from tender.parse import parse_pdf  # noqa: E402
        return {p["page"]: p.get("height") or PAGE_HEIGHT for p in parse_pdf(f"sample/{doc}.pdf")}
    except Exception:
        return {}


def render(stats: dict[str, dict]) -> str:
    lines = [f"{'文档':<16}{'refs':>6}{'无框':>6}{'整页':>6}{'块级':>6}{'条款级':>8}"
             f"{'评分表行':>12}"]
    allrefs = {"none": 0, "page": 0, "block": 0, "clause": 0, "refs": 0}
    rows_total = rows_ok = 0
    for doc, s in stats.items():
        t = s["total"]
        rs = t["table_rows"]
        rows_total += rs["rows"]
        rows_ok += rs["with_bbox"]
        row_cell = (f"{rs['with_bbox']}/{rs['rows']} 行" if rs["rows"] else "—")
        lines.append(f"{doc:<16}{t['refs']:>6}{t['none']:>6}{t['page']:>6}{t['block']:>6}"
                     f"{t['clause_level']:>8.0%}{row_cell:>12}")
        for k in allrefs:
            allrefs[k] += t[k]
    if len(stats) > 1:
        lvl = allrefs["clause"] / max(allrefs["refs"], 1)
        lines.append(f"{'合计':<16}{allrefs['refs']:>6}{allrefs['none']:>6}{allrefs['page']:>6}"
                     f"{allrefs['block']:>6}{lvl:>8.0%}{f'{rows_ok}/{rows_total} 行':>12}")
    lines.append("\n条款级 = ref 的 bbox 被真条款 bbox 包住的占比（纯几何）；"
                 "评分表行 = 评分表里带行级 bbox 的行数")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    dirs = [Path(a) for a in argv[:2]]
    results = []
    for d in dirs:
        payloads = load_dir(d)
        results.append({doc: doc_stats(p, page_heights_of(doc)) for doc, p in payloads.items()})

    if len(results) == 1:
        print(render(results[0]))
        return 0

    before, after = results
    print("=== 改动前：" + str(dirs[0]))
    print(render(before))
    print("\n=== 改动后：" + str(dirs[1]))
    print(render(after))
    print("\n=== 差异（条款级占比）")
    for doc in after:
        b = before.get(doc, {}).get("total", {})
        a = after[doc]["total"]
        print(f"{doc:<16}{b.get('clause_level', 0):>10.0%} → {a['clause_level']:>6.0%}"
              f"   （无框 {b.get('none', 0)} → {a['none']}，整页框 {b.get('page', 0)} → {a['page']}）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
