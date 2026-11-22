"""ask_box 评测：证明 tool-calling agent 真的在调工具、真的取证。

跑法:
    HF_HUB_OFFLINE=1 env -u PYTHONPATH ~/miniforge3/envs/report-rag/bin/python eval/ask_eval.py
    ... --verbose      # 打印每题的完整 trace（面试演示用）

判据两条，都要过：
1. 答案含 `expect_any` 里任意一个子串（不区分大小写）
2. `trace` 里出现了 `expect_tools` 的全部工具名

第二个条件才是重点：它把「模型恰好蒙对」和「真的查了」区分开。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from tender.ask_box import ask_question  # noqa: E402
from tender.index import load_tender_index  # noqa: E402
from tender.tools import set_context  # noqa: E402

CASES = json.load(open(ROOT / "eval/ask_cases.json", encoding="utf-8"))["cases"]


def run_one(case: dict, index, company: dict) -> dict:
    set_context(index, company)
    res = ask_question(case["q"])
    ans = (res.get("answer") or "").strip()
    trace = res.get("trace") or []
    used = [t["tool"] for t in trace]

    hit_value = any(s.lower() in ans.lower() for s in case["expect_any"])
    hit_tools = all(t in used for t in case["expect_tools"])
    return {
        "id": case["id"],
        "ok": hit_value and hit_tools,
        "hit_value": hit_value,
        "hit_tools": hit_tools,
        "steps": len(trace),
        "used": used,
        "answer": ans,
        "trace": trace,
    }


def main() -> int:
    verbose = "--verbose" in sys.argv
    by_doc: dict[str, list] = {}
    for c in CASES:
        by_doc.setdefault(c["doc"], []).append(c)

    results = []
    for doc, cases in by_doc.items():
        index = load_tender_index(ROOT / f"sample/{doc}.pdf")
        company = yaml.safe_load(open(ROOT / f"sample/{doc}_company.yaml", encoding="utf-8"))
        for c in cases:
            r = run_one(c, index, company)
            results.append(r)
            flag = "✓" if r["ok"] else "✗"
            why = "" if r["ok"] else (
                f"  [答案{'✓' if r['hit_value'] else '✗'} 工具{'✓' if r['hit_tools'] else '✗'}]")
            print(f"{flag} {r['id']:<18} {r['steps']} 步  {'+'.join(r['used']) or '无工具'}{why}")
            if verbose:
                print(f"    答案: {r['answer'][:200]}")
                for t in r["trace"]:
                    print(f"    · {t['tool']}({json.dumps(t['args'], ensure_ascii=False)[:90]})")

    total = len(results)
    passed = sum(1 for r in results if r["ok"])
    steps = [r["steps"] for r in results]
    with_tool = sum(1 for r in results if r["steps"] > 0)
    print()
    print(f"通过 {passed}/{total}  平均步数 {sum(steps)/total:.1f}  "
          f"调过工具 {with_tool}/{total}  最多步数 {max(steps)}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
