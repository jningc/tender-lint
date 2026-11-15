"""v2 四段流水线的入口：解析 → 抽取 → 比对 → 校验。

返回与 v1 同形状的 dict（六表 + dropped/degraded + sections），所以
`render_tables`、`app.py`、`main.py`、`eval/compare.py` 都不用改。
四段的中间产物落盘到 `data/step_output/<stem>/v2/`，这是「哪一步错了」的唯一来源。
"""

import json
import os
from pathlib import Path

from tender.parse import page_texts
from tender.stages import compare_stage, extract_stage, parse_stage, verify_stage
from tender.workflow import _progress_cb, load_company

ROOT = Path(__file__).resolve().parent.parent
TABLES = extract_stage.TABLES


def _enforce() -> bool:
    """校验段是否改表。默认开（阶段 4）；TENDER_VERIFY_ENFORCE=0 可关回「只记录」。"""
    return os.environ.get("TENDER_VERIFY_ENFORCE", "1").strip().lower() not in ("0", "false", "off")


ENFORCE = _enforce()


def run_check_v2(pdf_path, company_path, on_progress=None, reuse_index: bool = True) -> dict:
    """跑一次 v2 核对。"""
    token = _progress_cb.set(on_progress) if on_progress is not None else None
    try:
        return _run(pdf_path, company_path, reuse_index)
    finally:
        if token is not None:
            _progress_cb.reset(token)


def _artifact_dir(pdf_path) -> Path:
    return ROOT / "data" / "step_output" / Path(pdf_path).stem / "v2"


def _run(pdf_path, company_path, reuse_index: bool) -> dict:
    from tender.workflow import _emit

    folder = _artifact_dir(pdf_path)

    # 1 解析：章节树 + 坐标；建索引或复用
    doc, index = parse_stage.run(pdf_path, reuse_index=reuse_index)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "parse.json").write_text(
        json.dumps(doc.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8")

    # 2 抽取：检索 + 每表一次模型；产出六表行与条款清单
    company = load_company(company_path)
    ex = extract_stage.run(doc, index, company)
    extract_stage.dump(ex, folder)

    # 3 比对：结论按契约归位（落表、类型一致）
    _emit("compare", "running", "把结论按表归位")
    cmp = compare_stage.run(ex)
    compare_stage.dump(cmp, folder)
    _emit("compare", "done", f"{len(cmp.findings)} 条结论")

    # 4 校验：机械回查（页码/数值/交叉引用/串表），默认开；enforce 时改表
    _emit("verify", "running", "回查页码、数值、交叉引用")
    texts = page_texts(pdf_path)
    verified = verify_stage.run(cmp, ex, texts=texts, page_count=doc.page_count,
                                sections=doc.sections, enforce=ENFORCE)
    verify_stage.dump(verified, folder)
    changes = verify_stage.apply(verified, ex, enforce=ENFORCE)
    verify_stage.dump_changes(changes, folder)
    vsum = verify_stage.summary(verified)
    _emit("verify", "done",
          f"{vsum['passed']}/{vsum['checked']} 通过；降级 {len(changes['downgraded'])} 条、"
          f"移出 {len(changes['moved_out'])} 条")

    result = {
        "pdf_path": str(Path(pdf_path).resolve()),
        "company": company,
        "sections": [s.model_dump() for s in doc.sections],
        "missing": ex.missing,
        **{table: ex.rows.get(table) or [] for table in TABLES},
        "dropped": ex.dropped,
        "degraded": ex.degraded,
        "index": index,
        "pages": [],
        "page_count": doc.page_count,
        "engine": "v2",
        "timings": ex.timings,
        "verify": {"summary": vsum, "changes": changes},
        "contracts": {
            "clauses": len(ex.clauses),
            "findings": len(cmp.findings),
            "rejects": len(ex.rejects) + len(cmp.rejects),
            "verified": vsum,
        },
    }
    return result
