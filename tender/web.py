"""
薄 HTTP 接口。核对在后台线程里跑，页面只负责显示。

  python main.py web
  浏览器打开 http://127.0.0.1:8765
"""

from __future__ import annotations

import copy
import json
import threading
import uuid
from pathlib import Path

import yaml
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from tender.ask_box import ask_question
from tender.engine import check as engine_check
from tender.parse import render_page_png
from tender.tools import set_context

ROOT = Path(__file__).resolve().parent.parent
# 页面上的三家公司。点哪一家，就用这一对招标 PDF 和 company.yaml，从头跑完整核对。
CASES = (
    {
        "id": "beijing_road",
        "label": "示例监理有限公司",
        "pdf": ROOT / "sample" / "beijing_road.pdf",
        "company": ROOT / "sample" / "beijing_road_company.yaml",
    },
    {
        "id": "chongzuo_green",
        "label": "示例崇左绿林工程有限公司",
        "pdf": ROOT / "sample" / "chongzuo_green.pdf",
        "company": ROOT / "sample" / "chongzuo_green_company.yaml",
    },
    {
        "id": "wenling_video",
        "label": "示例温岭视联技术有限公司",
        "pdf": ROOT / "sample" / "wenling_video.pdf",
        "company": ROOT / "sample" / "wenling_video_company.yaml",
    },
)
CASE_BY_ID = {item["id"]: item for item in CASES}
# 空页面编辑框仍先放第一家的公司情况。
SAMPLE_COMPANY = CASES[0]["company"]
PAGE = ROOT / "static" / "index.html"
VENDOR = ROOT / "static" / "vendor"
UPLOAD_ROOT = ROOT / "data" / "web_jobs"

# 页面顶部进度。顺序跟当前默认流水线一致：
# 解析段（页、章、表、条款、索引）→ 抽取六表 → 比对归表 → 校验回查。
STAGE_DEFS = [
    ("parse", "解析", "按页抽文字和块坐标"),
    ("sections", "切章", "按标题标成章节类型"),
    ("tables", "表格", "评分表和前附表"),
    ("clauses", "条款", "按编号切成条款"),
    ("index", "建索引", "条款和表行按章建库"),
    ("extract", "抽取", "对照公司情况抽出六张表"),
    ("compare", "比对", "每条结论归到对应的表"),
    ("verify", "校验", "回查页码、数字和交叉引用"),
]

TABLE_KEYS = ("feibiao", "pingfen", "cailiao", "kongwei", "shijian", "hetong")

_lock = threading.Lock()
_ask_lock = threading.Lock()
_jobs: dict[str, dict] = {}


class Json(JSONResponse):
    """中文按 UTF-8 原样输出。"""

    def render(self, content) -> bytes:
        return json.dumps(content, ensure_ascii=False).encode("utf-8")


def _blank_stages() -> list[dict]:
    return [
        {"id": sid, "name": name, "scope": scope, "state": "wait", "detail": ""}
        for sid, name, scope in STAGE_DEFS
    ]


def _blank_tables() -> dict:
    return {key: [] for key in TABLE_KEYS}


def _parse_company(text: str) -> dict:
    if not (text or "").strip():
        raise ValueError("company.yaml 是空的")
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"company.yaml 无法解析: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("company.yaml 须是一份映射")
    return data


def _public(job: dict) -> dict:
    return {
        "id": job["id"],
        "status": job["status"],
        "error": job["error"],
        "file_name": job["file_name"],
        "company_name": job["company_name"],
        "page_count": job["page_count"],
        "sections": copy.deepcopy(job["sections"]),
        "stages": copy.deepcopy(job["stages"]),
        "tables": copy.deepcopy(job["tables"]),
        "missing": list(job["missing"]),
    }


def _apply(job_id: str, event: dict) -> None:
    """把 run_check 的进度写进任务。索引对象只留在内存里。"""
    with _lock:
        job = _jobs[job_id]
        stage_id = event["stage"]
        for stage in job["stages"]:
            if stage["id"] != stage_id:
                continue
            stage["state"] = event["state"]
            if event.get("detail"):
                stage["detail"] = event["detail"]
        if "page_count" in event:
            job["page_count"] = event["page_count"]
        if "sections" in event:
            job["sections"] = event["sections"]
        if "rows" in event:
            job["tables"][stage_id] = event["rows"]
        if "missing" in event:
            job["missing"] = event["missing"]
        if "shijian" in event:
            job["tables"]["shijian"] = event["shijian"]
        if "hetong" in event:
            job["tables"]["hetong"] = event["hetong"]
        if event.get("index") is not None:
            job["index"] = event["index"]
        if event.get("company") is not None:
            job["company"] = event["company"]
        if event.get("pdf_path"):
            job["pdf_path"] = event["pdf_path"]
        # 只接受 index 阶段带的 page_texts（索引完才对前端可见），忽略 parse 早期误发
        if "page_texts" in event and stage_id == "index":
            job["page_texts"] = event["page_texts"]
        # 复用已有库时不再抽表、切条款，这两步标成完成，避免一直停在等待
        if stage_id == "index" and event.get("state") == "done" and "复用" in (event.get("detail") or ""):
            for skipped in job["stages"]:
                if skipped["id"] in ("tables", "clauses") and skipped["state"] == "wait":
                    skipped["state"] = "done"
                    skipped["detail"] = "随索引复用"


def _worker(job_id: str, pdf_path: str, company_path: str, use_index: bool = False) -> None:
    try:
        result = engine_check(pdf_path, company_path, on_progress=lambda event: _apply(job_id, event),
                              reuse_index=use_index)
    except Exception as exc:
        with _lock:
            job = _jobs[job_id]
            job["status"] = "error"
            job["error"] = str(exc)
            for stage in job["stages"]:
                if stage["state"] == "running":
                    stage["state"] = "error"
                    stage["detail"] = str(exc)
        return
    with _lock:
        job = _jobs[job_id]
        job["status"] = "done"
        job["index"] = result["index"]
        job["company"] = result["company"]
        job["pdf_path"] = result["pdf_path"]
        job["sections"] = result["sections"]
        job["missing"] = result["missing"]
        job["page_count"] = result.get("page_count") or len(result["pages"])
        if result.get("page_texts") is not None:
            job["page_texts"] = result["page_texts"]
        for key in TABLE_KEYS:
            job["tables"][key] = result[key]


def _launch(job_id: str, file_name: str, company: dict, pdf_path: Path, company_path: Path, use_index: bool = False) -> None:
    job = {
        "id": job_id,
        "status": "running",
        "error": "",
        "file_name": file_name,
        "company_name": str(company.get("name") or "company.yaml"),
        "page_count": 0,
        "sections": [],
        "stages": _blank_stages(),
        "tables": _blank_tables(),
        "missing": [],
        "pdf_path": str(pdf_path),
        "company": company,
        "index": None,
        "page_texts": [],
    }
    with _lock:
        _jobs[job_id] = job
    threading.Thread(
        target=_worker,
        args=(job_id, str(pdf_path), str(company_path), use_index),
        daemon=True,
    ).start()


async def page(request: Request):
    """演示页。"""
    return FileResponse(PAGE, media_type="text/html; charset=utf-8")


async def meta(request: Request):
    """空页面用的阶段列表和默认 company.yaml。"""
    text = SAMPLE_COMPANY.read_text(encoding="utf-8")
    return Json({"stages": _blank_stages(), "company": text})


def _case(doc_id: str) -> dict:
    """按公司编号取出成对的招标 PDF 和 company.yaml。"""
    item = CASE_BY_ID.get(doc_id or "")
    if item is None:
        raise ValueError(f"未知公司: {doc_id or '空'}")
    if not item["pdf"].exists():
        raise ValueError(f"招标文件不存在: {item['pdf'].name}")
    if not item["company"].exists():
        raise ValueError(f"公司情况不存在: {item['company'].name}")
    return item


async def start_sample(request: Request):
    """按公司载入成对的招标 PDF 和 company.yaml，从头跑完整核对。"""
    body = await request.json()
    doc_id = body.get("doc") if isinstance(body, dict) else ""
    if not isinstance(doc_id, str):
        return Json({"error": "缺少公司"}, status_code=400)
    try:
        item = _case(doc_id)
        company_text = item["company"].read_text(encoding="utf-8")
        company = _parse_company(company_text)
    except ValueError as exc:
        return Json({"error": str(exc)}, status_code=400)
    job_id = uuid.uuid4().hex[:12]
    folder = UPLOAD_ROOT / job_id
    folder.mkdir(parents=True, exist_ok=True)
    company_path = folder / "company.yaml"
    company_path.write_text(company_text, encoding="utf-8")
    _launch(job_id, item["pdf"].name, company, item["pdf"], company_path, False)
    return Json({
        "job_id": job_id,
        "company": company_text,
        "file_name": item["pdf"].name,
        "company_name": str(company.get("name") or item["label"]),
    })


async def job_state(request: Request):
    """当前进度和已经出的表。"""
    job_id = request.path_params["job_id"]
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return Json({"error": "任务不存在"}, status_code=404)
        payload = _public(job)
    return Json(payload)


async def job_pdf(request: Request):
    """把当前任务的招标 PDF 原件交给浏览器渲染。"""
    job_id = request.path_params["job_id"]
    with _lock:
        job = _jobs.get(job_id)
        pdf_path = job["pdf_path"] if job else ""
        file_name = job["file_name"] if job else "tender.pdf"
    if not pdf_path:
        return Json({"error": "任务不存在"}, status_code=404)
    return FileResponse(
        pdf_path,
        media_type="application/pdf",
        filename=file_name,
        content_disposition_type="inline",
    )


async def job_parsed_text(request: Request):
    """返回索引完成后的按页正文；网页只读这份，不再现场抽 PDF。"""
    job_id = request.path_params["job_id"]
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return Json({"error": "任务不存在"}, status_code=404)
        pages = list(job.get("page_texts") or [])
    if not pages:
        return Json({"error": "索引尚未完成，解析页文本未就绪"}, status_code=409)
    return Json({"page_count": len(pages), "pages": pages})


async def page_png(request: Request):
    """按页渲 PNG。查询参数可带 bbox（服务端可画框）；当前前端只跳页不画框。"""
    job_id = request.path_params["job_id"]
    page_num = int(request.path_params["page"])
    with _lock:
        job = _jobs.get(job_id)
        pdf_path = job["pdf_path"] if job else ""
    if not pdf_path:
        return Json({"error": "任务不存在"}, status_code=404)
    boxes = []
    for raw in request.query_params.getlist("bbox"):
        parts = raw.split(",")
        if len(parts) != 4:
            return Json({"error": "bbox 须是 x0,y0,x1,y1"}, status_code=400)
        boxes.append([float(part) for part in parts])
    try:
        png = await run_in_threadpool(render_page_png, pdf_path, page_num, boxes or None)
    except ValueError as exc:
        return Json({"error": str(exc)}, status_code=400)
    return Response(png, media_type="image/png")


def _ask_sync(index, company, question: str) -> dict:
    with _ask_lock:
        set_context(index, company)
        return ask_question(question)


async def ask(request: Request):
    """对这份招标文件提问。索引建完后即可用，不必等六张表都出完。"""
    job_id = request.path_params["job_id"]
    body = await request.json()
    question = (body.get("question") if isinstance(body, dict) else "") or ""
    question = question.strip()
    if not question:
        return Json({"error": "问题不能为空"}, status_code=400)
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return Json({"error": "任务不存在"}, status_code=404)
        index = job.get("index")
        company = job.get("company")
    if index is None:
        return Json({"error": "索引还没建好"}, status_code=409)
    out = await run_in_threadpool(_ask_sync, index, company, question)
    return Json({"answer": out["answer"], "trace": out["trace"]})


app = Starlette(
    routes=[
        Route("/", page, methods=["GET"]),
        Route("/api/meta", meta, methods=["GET"]),
        Route("/api/check/sample", start_sample, methods=["POST"]),
        Route("/api/jobs/{job_id}", job_state, methods=["GET"]),
        Route("/api/jobs/{job_id}/file", job_pdf, methods=["GET"]),
        Route("/api/jobs/{job_id}/parsed-text", job_parsed_text, methods=["GET"]),
        Route("/api/jobs/{job_id}/pages/{page:int}", page_png, methods=["GET"]),
        Route("/api/jobs/{job_id}/ask", ask, methods=["POST"]),
        Mount("/vendor", app=StaticFiles(directory=str(VENDOR))),
    ]
)
