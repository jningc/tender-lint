"""跑术语题集：同义归一率 + 互斥保持率，方向陷阱单列（C3 才解）。

用法:
  python eval/metrics/terms_score.py            # 打印分项
  python eval/metrics/terms_score.py -v         # 逐条
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tender.terms import load_terms, normalize  # noqa: E402

CASES = ROOT / "eval" / "metrics" / "terms_cases.json"
PASS_LINE = 0.80


def standards_in(text: str) -> set[str]:
    """文本里出现了哪些标准概念（先归一再取交集）。

    判据用「共享标准词」而不是「整句归一后相等」：实际用途是检索匹配
    （条文中出现「磋商保证金」要能被 query「投标保证金」命中），
    整句逐字相等会把句式差异算成失败，那不是术语表的职责。
    """
    normalized = normalize(text)
    return {std for std in load_terms() if std in normalized}


def score(verbose: bool = False) -> dict:
    payload = json.loads(CASES.read_text(encoding="utf-8"))
    buckets: dict[str, dict] = {}
    misses = []
    for case in payload.get("cases") or []:
        kind = case.get("kind", "synonym")
        bucket = buckets.setdefault(kind, {"hit": 0, "total": 0})
        if case.get("known_gap"):
            bucket["total"] += 1
            continue
        shared = standards_in(case["a"]) & standards_in(case["b"])
        got = bool(shared)
        bucket["total"] += 1
        hit = got == bool(case["same"])
        bucket["hit"] += int(hit)
        if not hit:
            misses.append((case["id"], case["a"], case["b"],
                           sorted(standards_in(case["a"])), sorted(standards_in(case["b"]))))
        if verbose:
            print(f"  {'✓' if hit else '✗'} [{case['id']}] {case['a']} | {case['b']} → 共享 {sorted(shared)}")
    for kind, b in buckets.items():
        b["rate"] = round(b["hit"] / b["total"], 3) if b["total"] else 0.0
    return {"buckets": buckets, "misses": misses}


def main(argv: list[str]) -> int:
    result = score(verbose="-v" in argv)
    for kind, b in result["buckets"].items():
        gap = "（known_gap，不计入）" if kind == "direction" else ""
        print(f"{kind:10s} {b['hit']}/{b['total']} = {b['rate']:.0%} {gap}")
    graded = [b for k, b in result["buckets"].items() if k != "direction"]
    hit = sum(b["hit"] for b in graded)
    total = sum(b["total"] for b in graded)
    rate = hit / total if total else 0.0
    print(f"\n同义 + 互斥合计 {hit}/{total} = {rate:.0%}（通过线 {PASS_LINE:.0%}）")
    for mid, a, b, na, nb in result["misses"]:
        print(f"  漏判 {mid}: {a!r} / {b!r} → 归一后 {na!r} / {nb!r}")
    return 0 if rate >= PASS_LINE else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
