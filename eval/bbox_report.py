"""bbox 口径统计：每条 ref 的框是真框还是整页框，以及「条款级」占比。

用法:
  python eval/bbox_report.py eval/engine/v2                 # 单份目录（<doc>/tables.json）
  python eval/bbox_report.py /tmp/before_bbox eval/engine/v2 # 前后对比

三档口径（互斥，按严重程度排）:
  none  无 bbox
  page  有 bbox 但高度 > 60% 页高（整页框，指不到具体位置）
  block 块级框（可定位到页内某一处）
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TABLES = ("feibiao", "pingfen", "cailiao", "kongwei", "shijian", "hetong")
PAGE_HEIGHT = 842.0  # A4；真实页高由索引带出，这里只作判定阈值


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
    per_table, total = {}, {"none": 0, "page": 0, "block": 0}
    for t in TABLES:
        refs = [r for row in (payload.get("tables") or {}).get(t) or [] for r in (row.get("refs") or [])]
        c = classify(refs, page_heights)
        per_table[t] = {**c, "refs": sum(c.values())}
        for k in total:
            total[k] += c[k]
    refs = sum(total.values())
    total["refs"] = refs
    total["clause_level"] = round(total["block"] / refs, 3) if refs else 0.0
    return {"doc": payload.get("doc"), "total": total, "per_table": per_table}


def load_dir(folder: Path) -> dict[str, dict]:
    out = {}
    for f in sorted(folder.glob("*/tables.json")):
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
    lines = [f"{'文档':<16}{'refs':>6}{'无框':>7}{'整页框':>8}{'块级':>7}{'条款级占比':>10}"]
    allrefs = {"none": 0, "page": 0, "block": 0, "refs": 0}
    for doc, s in stats.items():
        t = s["total"]
        lines.append(f"{doc:<16}{t['refs']:>6}{t['none']:>7}{t['page']:>8}{t['block']:>7}"
                     f"{t['clause_level']:>10.0%}")
        for k in allrefs:
            allrefs[k] += t[k]
    if len(stats) > 1:
        lines.append(f"{'合计':<16}{allrefs['refs']:>6}{allrefs['none']:>7}{allrefs['page']:>8}"
                     f"{allrefs['block']:>7}{allrefs['block'] / max(allrefs['refs'], 1):>10.0%}")
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
