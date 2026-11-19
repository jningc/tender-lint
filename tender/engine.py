"""引擎开关：v1（五节点直排）/ v2（四段流水线）。

默认 v2。写错的值回退 v2，不抛异常——别让一个笔误把整条链路带崩。
切换方式两处：环境变量 `TENDER_ENGINE`，或命令行 `main.py check --engine v1`。
"""

import os

ENGINES = ("v1", "v2")
DEFAULT_ENGINE = "v2"


def current() -> str:
    """当前引擎。环境变量写错就回 v2。"""
    value = (os.environ.get("TENDER_ENGINE") or DEFAULT_ENGINE).strip().lower()
    return value if value in ENGINES else DEFAULT_ENGINE


def check(pdf_path, company_path, on_progress=None, engine=None, reuse_index: bool = False) -> dict:
    """按引擎跑一次核对。两条引擎的建库都走 parse_stage。"""
    eng = (engine or current()).strip().lower()
    if eng == "v2":
        from tender.workflow_v2 import run_check_v2

        return run_check_v2(pdf_path, company_path, on_progress=on_progress, reuse_index=reuse_index)

    from tender.workflow import run_check, run_check_saved

    if reuse_index:
        return run_check_saved(pdf_path, company_path, on_progress=on_progress)
    return run_check(pdf_path, company_path, on_progress=on_progress)
