"""表格检索评测：small chunk 召回率 + big 里有没有答案。

两个指标，都可自动判：

1. **表级召回**：top-k 里有没有出现期望的表
2. **行级召回**：top-k 里有没有出现期望的那一行
3. **答案在场**：把命中表整表 HTML + 全部 small chunk 拼起来，`expect_answer` 在不在

第 3 条不算「LLM 答对了」，只算「材料给足了」——整表给过去以后
模型答不出来是提示词问题，不是检索问题，两件事要分开判。

用法:
  python eval/table_recall.py          # 汇总
  python eval/table_recall.py -v       # 逐条
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tender.tabular import extract_tables  # noqa: E402
from tender.table_chunks import TableIndex, build_chunks  # noqa: E402

QUERIES = ROOT / "eval" / "table_queries.json"
SAMPLE = ROOT / "sample"
TOP_K = 5


def load():
    tables, by_id = [], {}
    for doc in ("beijing_road", "chongzuo_green", "wenling_video"):
        pdf = SAMPLE / f"{doc}.pdf"
        if not pdf.exists():
            continue
        for t in extract_tables(pdf, stem=doc):
            if t.kind == "pingfen" and t.rows:
                tables.append(t)
                by_id[t.id] = t
    return tables, by_id


def run(verbose: bool = False) -> dict:
    payload = json.loads(QUERIES.read_text(encoding="utf-8"))
    tables, by_id = load()
    chunks = build_chunks(tables)
    index = TableIndex(chunks)

    # 每张表的全量文本：small chunk（含表级事实）+ 整表 HTML
    corpus = {}
    for c in chunks:
        corpus.setdefault(c["table_id"], []).append(c["text"])
    for tid, t in by_id.items():
        corpus[tid] = "\n".join(corpus.get(tid, [])) + "\n" + t.to_html()

    total = table_hit = row_hit = answer_hit = 0
    misses = []
    for case in payload.get("queries") or []:
        total += 1
        hits = index.search(case["q"], k=TOP_K)
        got_tables = [h["table_id"] for h in hits]
        t_ok = case["expect_table"] in got_tables
        want_row = case.get("expect_row")
        r_ok = (not t_ok) is False and any(
            h["table_id"] == case["expect_table"] and h["row_index"] == want_row
            for h in hits
        ) if want_row is not None else t_ok
        a_ok = case["expect_answer"] in corpus.get(case["expect_table"], "")
        table_hit += int(t_ok)
        row_hit += int(bool(r_ok))
        answer_hit += int(a_ok)
        if not (t_ok and r_ok and a_ok):
            misses.append((case["id"], case["q"], t_ok, r_ok, a_ok, got_tables))
        if verbose:
            flag = "✓" if (t_ok and r_ok and a_ok) else "✗"
            print(f"  {flag} {case['id']} 表{'✓' if t_ok else '✗'} 行{'✓' if r_ok else '✗'} "
                  f"答案{'✓' if a_ok else '✗'}  {case['q']}  → {got_tables}")

    return {
        "total": total,
        "table": (table_hit, total),
        "row": (row_hit, total),
        "answer": (answer_hit, total),
        "misses": misses,
    }


def main(argv: list[str]) -> int:
    res = run(verbose="-v" in argv)
    t, n = res["table"]
    r, _ = res["row"]
    a, _ = res["answer"]
    print(f"\n表级召回 {t}/{n}　行级召回 {r}/{n}　答案在场 {a}/{n}（top-{TOP_K}）")
    for cid, q, tk, rk, ak, got in res["misses"]:
        print(f"  漏 {cid}: {q} → 表{'✓' if tk else '✗'} 行{'✓' if rk else '✗'} "
              f"答案{'✓' if ak else '✗'} {got}")
    # 行级召回是这张表的主指标：达 16/20 才算可用
    return 0 if r >= 16 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
