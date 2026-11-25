"""MapReduce 探针：只对废标表跑一次全扫，量化「比检索多抽多少」。

回答三个问题：
1. 全扫能多抽多少条（覆盖率涨多少）
2. 重复率多少（验证 Reduce 去重是否必要）
3. 成本（包数 = LLM 调用次数）

做法：
- 按条款打包（≤8000 字，相邻包重叠 2 条，不切断条款）
- 每包一次调用，只问「会导致投标无效/否决/资格不合格的条件」
- 模型只返回条件原文 → 代码匹配回条款（不信模型给的页码）
- 按 clause_id 去重（同一条被两个包抽到，id 一定相同）

用法：
    python eval/mapreduce_probe.py --doc beijing_road
    python eval/mapreduce_probe.py --doc beijing_road --dry-run   # 只看打包，不调模型
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from pydantic import BaseModel, Field  # noqa: E402

from tender.clauses import build_clauses  # noqa: E402
from tender.frontmatter import is_frontmatter  # noqa: E402
from tender.parse import parse_pdf, pdf_toc  # noqa: E402
from tender.sections import split_sections  # noqa: E402
from tender.tabular import extract_tables  # noqa: E402
from tender.tiers import MUST_FAIL  # noqa: E402

PACK_CHARS = 8000
OVERLAP = 2
#: 废标相关章。格式章刻意不扫（投标书模板里的「中标后提交履约保证金」会被误判）
FEIBIAO_TYPES = {"invite", "notice", "qualify", "score", "demand"}

_WS = re.compile(r"[\s\u3000]+")


def norm(s: str) -> str:
    return _WS.sub("", s or "")


class PackResult(BaseModel):
    conditions: list[str] = Field(
        default_factory=list,
        description="会导致投标无效、被否决、或资格不合格的条件原文（逐字摘录关键句）",
    )


PROMPT = """你是招标文件核对员。下面是同一份招标文件的第 {p0}–{p1} 页原文（已按条款切好）。

只抽一类：**会导致投标无效、被否决、或资格不合格的条件**。

要求：
1. condition 逐字摘录原文的关键句，**不要改写、不要总结**
2. 只抽真正的「后果条件」；「本项目不收取保证金」这类取值**不要抽**
3. 这一段没有就返回空数组，不要硬凑
4. 一条条件只输出一次
"""


def pack_nodes(nodes: list, max_chars: int = PACK_CHARS, overlap: int = OVERLAP) -> list[list]:
    """按字数累加打包，不切断条款，相邻包重叠 overlap 条。"""
    packs, cur, size = [], [], 0
    for n in nodes:
        t = n.text or ""
        if cur and size + len(t) > max_chars:
            packs.append(cur)
            cur = cur[-overlap:] if overlap else []
            size = sum(len(x.text or "") for x in cur)
        cur.append(n)
        size += len(t)
    if cur:
        packs.append(cur)
    return packs


def match_back(cond: str, pack: list):
    """把模型返回的条件原文匹配回条款（前缀互含 40 字）。"""
    a = norm(cond)
    if len(a) < 6:
        return None
    for n in pack:
        b = norm(n.text)
        if len(b) < 6:
            continue
        k = min(len(a), len(b), 40)
        if a[:k] in b or b[:k] in a:
            return n
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--doc", default="beijing_road")
    ap.add_argument("--dry-run", action="store_true", help="只打包不调模型")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    pdf = str(ROOT / "sample" / f"{args.doc}.pdf")
    pages = parse_pdf(pdf)
    toc = pdf_toc(pdf)
    sections = split_sections(pages, toc=toc)
    tabs = extract_tables(pdf, stem=args.doc)
    boxes: dict[int, list] = {}
    for t in [x for x in tabs if x.kind == "pingfen" or is_frontmatter(x)]:
        for pno, bb in (t.page_bboxes or {}).items():
            boxes.setdefault(int(pno), []).append(list(bb))
        for r in t.rows or []:
            boxes.setdefault(r.page, []).append(list(r.bbox))

    nodes = build_clauses(pages, sections, stem=args.doc, toc=toc, table_boxes=boxes)
    scope = [n for n in nodes
             if n.kind != "block" and n.section in FEIBIAO_TYPES and (n.text or "").strip()]
    packs = pack_nodes(scope)

    print(f"{args.doc}: 条款 {len(scope)} 条 → {len(packs)} 包"
          f"（每包 ≤{PACK_CHARS} 字，重叠 {OVERLAP} 条）")
    for i, p in enumerate(packs):
        chars = sum(len(n.text or "") for n in p)
        pages_span = f"p{p[0].page_start}-{p[-1].page_end}"
        print(f"  包{i+1}: {len(p)} 条 · {chars} 字 · {pages_span}")
    if args.dry_run:
        return 0

    from tender.llm import get_llm
    # DeepSeek 不支持 response_format=json_schema（会报 "This response_format type
    # is unavailable now"），必须走 function_calling——和 workflow._extract 一致。
    llm = get_llm().with_structured_output(PackResult, method="function_calling")

    t0 = time.time()
    cands: list[dict] = []
    dup = 0
    for i, p in enumerate(packs):
        text = "\n\n".join(f"[{n.number or '无编号'}] {n.text}" for n in p)
        prompt = PROMPT.format(p0=p[0].page_start, p1=p[-1].page_end) + "\n\n" + text
        try:
            res = llm.invoke(prompt)
            conds = (res or PackResult()).conditions or []
        except Exception as e:                     # noqa: BLE001
            print(f"  包{i+1} 失败: {e}")
            conds = []
        hit = 0
        for c in conds:
            n = match_back(c, p)
            if n is None:
                continue
            hit += 1
            if any(x["id"] == n.id for x in cands):
                dup += 1
                continue
            cands.append({"id": n.id, "page": n.page_start, "number": n.number,
                          "text": (n.text or "").replace("\n", " ")[:90]})
        print(f"  包{i+1}: 模型给 {len(conds)} 条 → 匹配回 {hit} 条")

    elapsed = time.time() - t0

    # 候选条款（含后果词），用于算覆盖率——和 coverage.py 同一套定义
    words = MUST_FAIL        # 只用强后果词，和 coverage.py 同一套定义
    candidates = [n for n in scope if any(w in (n.text or "") for w in words)]
    covered = [c for c in candidates if any(c.id == cc["id"] for cc in cands)]
    cov = len(covered) / len(candidates) if candidates else None

    # 检索模式的对照：同一份文档门禁结果里废标表覆盖了多少候选
    base_cov = None
    base_rows = 0
    files = sorted((ROOT / "data" / "regression").glob(f"*/{args.doc}/r1.json"))
    if files:
        from eval.metrics.coverage import _matches  # 复用同一套匹配
        result = json.loads(files[-1].read_text(encoding="utf-8"))
        rows = (result.get("tables") or {}).get("feibiao") or []
        base_rows = len(rows)
        refs = [ref for r in rows for ref in (r.get("refs") or [])]
        base_hit = [c for c in candidates
                    if any(_matches(ref.get("text") or "", c.text or "") for ref in refs)]
        base_cov = len(base_hit) / len(candidates) if candidates else None

    out = {
        "doc": args.doc,
        "packs": len(packs),
        "llm_calls": len(packs),
        "elapsed_s": round(elapsed, 1),
        "extracted": len(cands),
        "duplicates": dup,
        "candidates": len(candidates),
        "covered": len(covered),
        "coverage": round(cov, 3) if cov is not None else None,
        "retrieval_rows": base_rows,
        "retrieval_coverage": round(base_cov, 3) if base_cov is not None else None,
        "sample": cands[:8],
    }
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(f"\n=== 结果（{args.doc}）")
        print(f"  调用        {out['llm_calls']} 次 / {out['elapsed_s']}s")
        print(f"  抽出        去重后 {out['extracted']} 条（重复 {dup} 条被合并）")
        print(f"  候选条款    {out['candidates']} 条（含后果词）")
        print(f"  覆盖率      全扫 {out['coverage']*100:.0f}%  vs  检索 {out['retrieval_coverage']*100:.0f}%"
              if out["coverage"] is not None and out["retrieval_coverage"] is not None else "  覆盖率 —")
        print(f"  行数        全扫 {out['extracted']} 行  vs  检索 {out['retrieval_rows']} 行")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
