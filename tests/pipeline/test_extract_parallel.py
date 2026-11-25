"""表内并发：六个作业并发跑出来的结果，必须和串行一模一样。

用假节点（睡一小会儿、返回固定行）测调度本身，不碰模型。
"""

import copy
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

import tender.stages.extract_stage as ex  # noqa: E402


def _fake_node(name, delay=0.15):
    def node(state):
        time.sleep(delay)
        rows = [{name: f"{name}-row", "refs": [{"page": 3, "bbox": [10, 10, 20, 20], "text": "t"}]}]
        out = {name: rows, "dropped": {name: []}, "degraded": {name: []}}
        if name == "feibiao":
            out["missing"] = []
        return copy.deepcopy(out)

    return node


def _patch(monkeypatch):
    jobs = tuple((name, _fake_node(name)) for name, _ in ex.JOBS)
    monkeypatch.setattr(ex, "JOBS", jobs)


def test_parallel_matches_serial(monkeypatch):
    _patch(monkeypatch)
    doc = type("D", (), {"pdf": "sample/beijing_road.pdf"})()
    serial = ex.run(doc, index=None, company={}, workers=1)
    parallel = ex.run(doc, index=None, company={}, workers=6)

    assert serial.rows == parallel.rows
    assert serial.timings["workers"] == 1
    assert parallel.timings["workers"] == 6


def test_parallel_is_faster(monkeypatch):
    _patch(monkeypatch)
    doc = type("D", (), {"pdf": "sample/beijing_road.pdf"})()

    t0 = time.time()
    ex.run(doc, index=None, company={}, workers=1)
    serial_s = time.time() - t0

    t0 = time.time()
    ex.run(doc, index=None, company={}, workers=6)
    parallel_s = time.time() - t0

    # 六个 0.15s 的作业：串行 ~0.9s，并发 ~0.15s；给足余量只要求小于一半
    assert parallel_s < serial_s / 2, (serial_s, parallel_s)


def test_workers_env(monkeypatch):
    monkeypatch.setenv("TENDER_MAX_WORKERS", "1")
    assert ex._default_workers() == 1
    monkeypatch.setenv("TENDER_MAX_WORKERS", "abc")
    assert ex._default_workers() == 6


def test_llm_concurrency_gate(monkeypatch):
    """LLM 并发默认 4，可用环境变量调；没设或写错都回 4。"""
    import tender.workflow as wf

    monkeypatch.setattr(wf, "_llm_sem", None)
    monkeypatch.delenv("TENDER_LLM_CONCURRENCY", raising=False)
    assert wf._semaphore()._value == 4

    monkeypatch.setattr(wf, "_llm_sem", None)
    monkeypatch.setenv("TENDER_LLM_CONCURRENCY", "2")
    assert wf._semaphore()._value == 2

    monkeypatch.setattr(wf, "_llm_sem", None)
    monkeypatch.setenv("TENDER_LLM_CONCURRENCY", "abc")
    assert wf._semaphore()._value == 4
