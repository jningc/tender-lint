"""校验段：数值归一、四项检查、三个已知幻觉必须被拦、误退率别太高。不调模型。"""

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from tender.contracts import Clause, Finding, Section  # noqa: E402
from tender.stages import verify_stage as vs  # noqa: E402
from tender.stages.compare_stage import CompareResult  # noqa: E402
from tender.stages.extract_stage import ExtractResult  # noqa: E402

CASES = json.loads((PROJECT_ROOT / "eval" / "metrics" / "hallucination_cases.json").read_text(encoding="utf-8"))["cases"]
SECTIONS = [Section(type="notice", title=f"第{n}章 测试章", start_page=n, end_page=n + 1)
            for n in range(1, 8)]
PAGES = {
    8: "12.1 投标保证金 投标保证金金额：01 包：8000 元；13.1 投标有效期 自提交投标文件的截止之日起算90 日历天。",
    17: "12.1 投标人应按《投标人须知资料表》中规定的金额及要求交纳投标保证金。",
    26: "35 履约保证金金额 履约保证金金额为中标金额的5 %（注：履约保证金不超过5%）",
}


def _payload(case):
    c = case["clause"]
    clause = Clause.model_validate(c)
    finding = Finding(clause_id=clause.id, table=case["finding"]["table"],
                      verdict=case["finding"]["verdict"], evidence=[clause.evidence])
    row = {"verdict": case["finding"]["verdict"], "condition": c["text"],
           "refs": [c["evidence"]]}
    ex = ExtractResult(rows={"feibiao": [row]}, row_of={clause.id: row}, clauses=[clause])
    return clause, CompareResult(findings=[finding]), ex


# ---------- 数值归一 ----------

def test_cn_numbers():
    assert vs.cn_to_number("捌仟") == 8000
    assert vs.cn_to_number("壹万") == 10000
    assert vs.cn_to_number("壹仟贰佰") == 1200


def test_has_number_variants():
    assert vs.has_number("保证金 8,000 元", 8000)
    assert vs.has_number("保证金 0.8 万元", 8000)
    assert vs.has_number("人民币捌仟元整", 8000)
    assert not vs.has_number("按资料表规定的金额", 8000)


# ---------- 三个已知幻觉 ----------

def test_hallucination_cases_are_rejected():
    for case in CASES:
        _, cmp, ex = _payload(case)
        verified = vs.run(cmp, ex, texts=PAGES, page_count=92, sections=SECTIONS, enforce=False)
        v = verified[0]
        assert v.ok is False, case["id"]
        failed = [c.name for c in v.checks if not c.ok]
        assert case["expect_failed"] in failed, (case["id"], failed)


def test_normal_row_passes():
    clause = Clause.model_validate({
        "id": "beijing_road:feibiao:001", "type": "废标",
        "text": "投标保证金金额：01 包 8000 元，未按要求缴纳的投标无效",
        "evidence": {"page": 8, "bbox": [47.0, 37.3, 548.2, 200.0],
                     "text": "12.1 投标保证金 投标保证金金额：01 包：8000 元"},
    })
    finding = Finding(clause_id=clause.id, table="废标对照", verdict="不满足", evidence=[clause.evidence])
    ex = ExtractResult(rows={}, row_of={}, clauses=[clause])
    verified = vs.run(CompareResult(findings=[finding]), ex, texts=PAGES, page_count=92,
                      sections=SECTIONS, enforce=False)
    assert verified[0].ok is True, [c.dict() for c in verified[0].checks]


# ---------- apply：降级与移表 ----------

def test_apply_downgrades_and_moves(monkeypatch):
    bad_evidence = _payload(CASES[1])          # 页码对不上金额 → 降级
    move = _payload(CASES[0])                  # 履约保证金串表 → 移出

    ex = ExtractResult(
        rows={"feibiao": [bad_evidence[2].rows["feibiao"][0], move[2].rows["feibiao"][0]]},
        row_of={**bad_evidence[2].row_of, **move[2].row_of},
        clauses=[bad_evidence[0], move[0]],
    )
    cmp = CompareResult(findings=[bad_evidence[1].findings[0], move[1].findings[0]])
    verified = vs.run(cmp, ex, texts=PAGES, page_count=92, sections=SECTIONS, enforce=True)
    changes = vs.apply(verified, ex, enforce=True)

    assert len(changes["downgraded"]) == 1
    assert len(changes["moved_out"]) == 1
    assert len(ex.rows["feibiao"]) == 1                       # 串表那条被移出
    assert ex.dropped["feibiao"][0]["reason"] == "table_exclusive"
    assert ex.rows["feibiao"][0]["verdict"] == "人工看"       # 证据不足那条降级
    assert "校验退回" in ex.rows["feibiao"][0]["note"]


def test_apply_off_changes_nothing():
    clause, cmp, ex = _payload(CASES[1])
    verified = vs.run(cmp, ex, texts=PAGES, page_count=92, sections=SECTIONS, enforce=False)
    changes = vs.apply(verified, ex, enforce=False)
    assert changes["downgraded"] == [] and changes["moved_out"] == []
    assert ex.rows["feibiao"][0]["verdict"] == "不满足"
