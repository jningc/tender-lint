"""覆盖率：表里的 refs 覆盖了多少「该被抽到的条款」。

## 为什么需要它

现在的自检是「pack 覆盖 = 100%」——那只证明**每段都喂过模型**，
不证明**模型抽全了**。给 8 条 hits 抽出 5 行，pack 覆盖仍然 100%。

真正该算的是**输出覆盖率**：

    覆盖率 = 表里 refs 命中的候选条款数 / 全文候选条款数

这个数字不需要人工金标，一次就能算出来。算出来之后，「怎么保证不遗漏」
就从「query 够不够全」变成「漏掉的那 N 条具体是什么」——可判的工程问题。

## 候选条款怎么定义（按表而定）

| 表 | 候选条款 = 含这些词的条款 |
|---|---|
| 废标对照 | MUST_FAIL（投标无效/否决/不予受理/取消资格…）+ 否定词（未/不得/否则） |
| 材料对照 | 必须提供 / 应提交 / 需附 / 未提供 |
| 时间节点 | 年月日 / 日内 / 截止 / 前 |

定义得宽 → 分母大 → 覆盖率虚低；定义得窄 → 分母小 → 覆盖率虚高。
所以这里的数字**只适合纵向对比**（同一定义下改前 vs 改后），不适合对外宣称。

## 用法

    python eval/coverage.py --doc beijing_road
    python eval/coverage.py --all

产物：每份文档一行，列出各表的覆盖率 + 漏掉的候选条款（前 10 条）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from tender.clauses import build_clauses  # noqa: E402
from tender.parse import parse_pdf, pdf_toc  # noqa: E402
from tender.sections import split_sections  # noqa: E402
from tender.tiers import MUST_FAIL  # noqa: E402

#: 结果文件里的表名（英文键）。**改这里就是改分母**，纵向对比时不要跟着改。
CANDIDATE_WORDS = {
    # 废标：只用 MUST_FAIL（强后果词）。之前加了 否则/未按/不得/不予 当召回补充，
    # 结果把「采购人不得索要赠品」「否则不得享受扶持政策」「监理不得身兼两职」
    # 全算成候选——分母虚大、覆盖率虚低（38 条里有 19 条是噪声）。
    "feibiao": MUST_FAIL,
    "cailiao": ("必须提供", "应提交", "需附", "须提供", "未提供", "格式"),
    "shijian": ("截止", "日内", "个工作日", "年月日", "前完成", "有效期"),
}

#: 报告里印中文名
LABELS = {"feibiao": "废标对照", "cailiao": "材料对照", "shijian": "时间节点"}

_WS = re.compile(r"[\s\u3000]+")


def _norm(s: str) -> str:
    return _WS.sub("", s or "")


def _matches(ref_text: str, clause_text: str) -> bool:
    """ref 和条款是不是同一处。

    只用「前缀互含」：ref 的抽取文本通常是条款原文的一段，
    开头 40 字能对上就认为是同一处。不做模糊匹配——宁可漏配，
    不要错配（错配会让覆盖率虚高，那是最坏的错误方向）。
    """
    a, b = _norm(ref_text), _norm(clause_text)
    if len(a) < 6 or len(b) < 6:
        return False
    n = min(len(a), len(b), 40)
    return a[:n] in b or b[:n] in a


def candidates(pages: list[dict], sections: list[dict], toc: list, words: tuple[str, ...]) -> list[dict]:
    """全文里「按词表该被抽到」的条款。"""
    nodes = build_clauses(pages, sections, toc=toc)
    out = []
    for n in nodes:
        if n.kind == "block" or not n.is_clause:
            continue
        text = n.text or ""
        if any(w in text for w in words):
            out.append({"page": n.page_start, "number": n.number, "text": text})
    return out


def table_rows(result: dict, table_name: str) -> list[dict]:
    """结果文件里某张表的行。兼容两种结构：{tables: {...}} 和直接 {...}。"""
    inner = result.get("tables") if isinstance(result, dict) else None
    tables = inner if isinstance(inner, dict) else result
    rows = tables.get(table_name) if isinstance(tables, dict) else None
    return rows or []


def coverage_for(doc: str, result_path: Path) -> dict:
    pdf = str(ROOT / "sample" / f"{doc}.pdf")
    pages = parse_pdf(pdf)
    toc = pdf_toc(pdf)
    sections = split_sections(pages, toc=toc)
    result = json.loads(result_path.read_text(encoding="utf-8"))

    report = {"doc": doc, "tables": {}}
    for table_name, words in CANDIDATE_WORDS.items():
        cands = candidates(pages, sections, toc, words)
        rows = table_rows(result, table_name)
        refs = [ref for r in rows for ref in (r.get("refs") or [])]

        hit, missed = [], []
        for c in cands:
            if any(_matches(ref.get("text") or "", c["text"]) for ref in refs):
                hit.append(c)
            else:
                missed.append(c)

        total = len(cands)
        report["tables"][table_name] = {
            "candidates": total,
            "covered": len(hit),
            "rows": len(rows),
            "refs": len(refs),
            "coverage": round(len(hit) / total, 3) if total else None,
            "missed_samples": [{"page": m["page"], "number": m["number"],
                                "text": (m["text"] or "").replace("\n", " ")[:80]}
                               for m in missed[:10]],
        }
    return report


def _latest_result(doc: str) -> Path | None:
    files = sorted((ROOT / "data" / "regression").glob(f"*/{doc}/r1.json"))
    return files[-1] if files else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--doc", help="beijing_road / chongzuo_green / wenling_video")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--result", help="指定表格结果 json；默认取 data/regression 最新一次")
    ap.add_argument("--json", action="store_true", help="输出 json 而不是文本")
    args = ap.parse_args()

    docs = ["beijing_road", "chongzuo_green", "wenling_video"] if args.all else [args.doc]
    docs = [d for d in docs if d]
    if not docs:
        ap.error("要么 --doc 要么 --all")

    out = []
    for doc in docs:
        rp = Path(args.result) if args.result else _latest_result(doc)
        if rp is None or not rp.exists():
            print(f"{doc}: 没有跑过门禁，找不到 data/regression/*/{doc}/r1.json", file=sys.stderr)
            continue
        rep = coverage_for(doc, rp)
        out.append(rep)
        if not args.json:
            print(f"\n=== {doc}  （结果取自 {rp.relative_to(ROOT)}）")
            for name, st in rep["tables"].items():
                cov = f"{st['coverage']*100:.0f}%" if st["coverage"] is not None else "—"
                print(f"  {LABELS.get(name, name):<8} 候选 {st['candidates']:>4} · 覆盖 {st['covered']:>3} "
                      f"({cov}) · 表 {st['rows']} 行 / {st['refs']} refs")
                for m in st["missed_samples"][:5]:
                    print(f"       漏 p{m['page']} [{m['number']}] {m['text'][:56]}")

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
