"""回归门禁：同一版本重复跑 N 次，取多数，标 flaky，和上次比，掉了就 exit 1。

为什么不是跑一次：同一份 wenling、同一引擎、同一天，三次跑出过 5/5、4/5、3/5。
单跑比分对比会把「没改行为」判成退化，也会把「真改坏」碰巧放过。所以：

- 每题按**多数**定结论；三次不一致的题标 `flaky`，另算一组「稳定题」分数用于判红；
- ★/▲ 对账看**覆盖的标记页**（中位数），不看 star 行数——行数同一份文件都能 5↔1；
- 与上次的 `eval/regression/last.json` 比，稳定题正例/负例/标记页覆盖任一下降即退出码 1。

用法:
  python eval/run_regression.py --engine v2                    # 跑 3 次并判红
  python eval/run_regression.py --engine v2 --repeats 5
  python eval/run_regression.py --engine v2 --update-baseline   # 认下当前结果为基线

产物:
  data/regression/<时间戳>/<doc>/r<N>.json   每次的六表（不进 Git，排查用）
  eval/regression/last.json                  最近一次的多数结果 + 基线比较
  eval/regression/run-<时间戳>.json          历史留档
"""

import argparse
import json
import statistics
import subprocess
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tender.engine import check  # noqa: E402

TABLES = ("feibiao", "pingfen", "cailiao", "kongwei", "shijian", "hetong")
COMPANIES = {
    "beijing_road": "sample/beijing_road_company.yaml",
    "chongzuo_green": "sample/chongzuo_green_company.yaml",
    "wenling_video": "sample/wenling_video_company.yaml",
}
RUNS = ROOT / "data" / "regression"
OUT = ROOT / "eval" / "regression"
LAST = OUT / "last.json"


def _question_kinds() -> dict:
    """题目 id → {kind, doc}。doc 必须带上：每份文档只该算自己那几题。"""
    data = json.loads((ROOT / "eval" / "questions.json").read_text(encoding="utf-8"))
    return {q["id"]: {"kind": q["kind"], "doc": q["doc"]} for q in data["questions"]}


KINDS = _question_kinds()


# ---------- 单次运行 ----------

def run_once(engine: str, doc: str, folder: Path, n: int) -> dict:
    """跑一次核对 + 打分，返回 counts / detail / markers。"""
    result = check(f"sample/{doc}.pdf", COMPANIES[doc], engine=engine, reuse_index=True)
    folder.mkdir(parents=True, exist_ok=True)
    payload = {"doc": doc, "engine": engine,
               "tables": {t: result.get(t) or [] for t in TABLES}}
    path = folder / f"r{n}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    out = subprocess.run(
        [sys.executable, str(ROOT / "eval" / "compare.py"), str(path), "--doc", doc, "--json", "-"],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise RuntimeError(f"compare.py 失败：{out.stderr[-400:]}")
    cmp = json.loads(out.stdout)
    return {
        "counts": cmp["counts"],
        "detail": {d["id"]: d["status"] for d in cmp["detail"]},
        "markers": cmp.get("markers") or {},
        "rows": sum(len(payload["tables"][t]) for t in TABLES),
    }


def run_doc(engine: str, doc: str, repeats: int, folder: Path) -> dict:
    runs = [run_once(engine, doc, folder / doc, n) for n in range(1, repeats + 1)]
    return aggregate(doc, runs)


# ---------- 聚合 ----------

def majority(statuses: list[str]) -> tuple[str, bool]:
    """返回（多数结论, 是否 flaky）。三次平票（如 hit/miss/absent 各一）算 unstable。"""
    counts = Counter(statuses)
    if len(counts) == 1:
        return statuses[0], False
    top, n = counts.most_common(1)[0]
    if n * 2 > len(statuses):
        return top, True
    return "unstable", True


def aggregate(doc: str, runs: list[dict]) -> dict:
    """把 N 次的逐题结果合成多数结论，并分「全部题」与「稳定题」两组。"""
    per_q, flaky = {}, []
    for qid in sorted(runs[0]["detail"]):
        status, unstable = majority([r["detail"].get(qid, "absent") for r in runs])
        per_q[qid] = status
        if unstable:
            flaky.append(qid)

    def _score(kind: str, only_stable: bool) -> tuple[int, int]:
        qs = [q for q, meta in KINDS.items()
              if meta["kind"] == kind and meta["doc"] == doc
              and (not only_stable or q not in flaky)]
        return sum(1 for q in qs if per_q.get(q) == "hit"), len(qs)

    pos_all, pos_all_n = _score("positive", only_stable=False)
    pos_stable, pos_stable_n = _score("positive", only_stable=True)
    neg_all, neg_all_n = _score("negative", only_stable=False)
    neg_stable, neg_stable_n = _score("negative", only_stable=True)

    return {
        "doc": doc,
        "runs": len(runs),
        "questions": per_q,
        "flaky": flaky,
        "positive": {"hit": pos_all, "total": pos_all_n},
        "positive_stable": {"hit": pos_stable, "total": pos_stable_n},
        "negative": {"hit": neg_all, "total": neg_all_n},
        "negative_stable": {"hit": neg_stable, "total": neg_stable_n},
        "violated": sum(1 for q, s in per_q.items() if s == "violated" and q not in flaky),
        "markers": {
            "markers_total": runs[0]["markers"].get("markers_total", 0),
            "covered_pages_median": int(statistics.median(
                [len(r["markers"].get("covered_pages") or []) for r in runs])),
            "star_rows_median": int(statistics.median(
                [r["markers"].get("star_rows", 0) for r in runs])),
        },
        "rows_median": int(statistics.median([r["rows"] for r in runs])),
    }


def totals(docs: list[dict]) -> dict:
    return {
        "pos_hit": sum(d["positive"]["hit"] for d in docs),
        "pos_total": sum(d["positive"]["total"] for d in docs),
        "pos_stable_hit": sum(d["positive_stable"]["hit"] for d in docs),
        "pos_stable_total": sum(d["positive_stable"]["total"] for d in docs),
        "neg_hit": sum(d["negative"]["hit"] for d in docs),
        "neg_total": sum(d["negative"]["total"] for d in docs),
        "neg_stable_hit": sum(d["negative_stable"]["hit"] for d in docs),
        "neg_stable_total": sum(d["negative_stable"]["total"] for d in docs),
        "violated": sum(d["violated"] for d in docs),
        "flaky": sum(len(d["flaky"]) for d in docs),
    }


# ---------- 与上次比较 ----------

def gate(current: dict, baseline: dict | None) -> list[str]:
    """返回退化原因；空列表 = 通过。判红只看**稳定题**与标记页覆盖。"""
    if not baseline:
        return []
    reasons = []
    cur, base = current["totals"], baseline["totals"]
    if cur["pos_stable_hit"] < base.get("pos_stable_hit", 0):
        reasons.append(f"稳定正例 {cur['pos_stable_hit']} < 上次 {base.get('pos_stable_hit')}")
    if cur["neg_stable_hit"] < base.get("neg_stable_hit", 0):
        reasons.append(f"稳定负例 {cur['neg_stable_hit']} < 上次 {base.get('neg_stable_hit')}")
    if current["markers"]["covered_pages"] < baseline["markers"]["covered_pages"]:
        reasons.append(f"★ 覆盖页 {current['markers']['covered_pages']} < 上次 "
                       f"{baseline['markers']['covered_pages']}")
    if cur.get("violated", 0) > base.get("violated", 0):
        reasons.append(f"负例违规 {cur['violated']} > 上次 {base.get('violated', 0)}"
                       f"（串表变多了）")
    return reasons


def main() -> int:
    ap = argparse.ArgumentParser(description="回归门禁：重复跑取多数，和上次比")
    ap.add_argument("--engine", choices=["v1", "v2"], default="v2")
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--docs", nargs="*", choices=list(COMPANIES), default=list(COMPANIES))
    ap.add_argument("--workers", type=int, default=2, help="文档级并发")
    ap.add_argument("--update-baseline", action="store_true", help="把这次结果认成基线")
    args = ap.parse_args()

    stamp = time.strftime("%Y%m%d-%H%M%S")
    folder = RUNS / stamp
    started = time.time()

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(run_doc, args.engine, doc, args.repeats, folder): doc
                   for doc in args.docs}
        collected = {futures[f]: f.result() for f in futures}
    docs = [collected[d] for d in args.docs]

    for d in docs:
        print(f"{d['doc']}: 正例 {d['positive']['hit']}/{d['positive']['total']}"
              f"（稳定 {d['positive_stable']['hit']}/{d['positive_stable']['total']}）"
              f" 负例 {d['negative']['hit']}/{d['negative']['total']}"
              f" 违规 {d['violated']} flaky {len(d['flaky'])}"
              f" | 行 {d['rows_median']} | ★ 覆盖页 {d['markers']['covered_pages_median']}",
              flush=True)

    t = totals(docs)
    current = {
        "engine": args.engine, "repeats": args.repeats, "at": stamp,
        "duration_s": round(time.time() - started, 1),
        "docs": docs, "totals": t,
        "markers": {"covered_pages": sum(d["markers"]["covered_pages_median"] for d in docs)},
    }
    flaky_all = [(d["doc"], q) for d in docs for q in d["flaky"]]

    baseline = None if args.update_baseline else (
        json.loads(LAST.read_text(encoding="utf-8")) if LAST.exists() else None)
    reasons = gate(current, baseline)

    OUT.mkdir(parents=True, exist_ok=True)
    LAST.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / f"run-{stamp}.json").write_text(
        json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n{args.engine} × {args.repeats} 次，{current['duration_s']}s")
    print(f"多数结果：正例 {t['pos_hit']}/{t['pos_total']} · 负例 {t['neg_hit']}/{t['neg_total']} "
          f"· 违规 {t['violated']} · flaky {t['flaky']} 题")
    print(f"判红口径（稳定题）：正例 {t['pos_stable_hit']}/{t['pos_stable_total']} · "
          f"负例 {t['neg_stable_hit']}/{t['neg_stable_total']}")
    print(f"★ 覆盖标记页合计 {current['markers']['covered_pages']}"
          + (f"（上次 {baseline['markers']['covered_pages']}）" if baseline else "（无上次记录）"))
    if flaky_all:
        print("flaky（不参与判红）：" + "、".join(f"{d}/{q}" for d, q in flaky_all))
    if args.update_baseline:
        print("\n门禁：已把这次认成新基线")
        return 0
    if reasons:
        print("\n门禁：**退化**")
        for r in reasons:
            print(f"  - {r}")
        return 1
    print("\n门禁：通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
