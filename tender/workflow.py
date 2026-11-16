"""
固定四步核对：废标 / 评分 / 材料 / 空位，外加时间和合同。

检索范围写死在节点里，模型不能改去搜哪一章。
"""

import os
import re
import threading
import time
from contextvars import ContextVar
from pathlib import Path
from typing import Annotated, TypedDict

import yaml
from langgraph.graph import END, START, StateGraph

from tender.index import TenderIndex, build_tender_index, load_tender_index
from tender.llm import get_llm
from tender.parse import page_count, parse_pdf, pdf_toc
from tender.sections import TYPE_LABEL, required_types_present, split_sections
from tender.tables import (
    CailiaoTable,
    FeibiaoRow,
    FeibiaoTable,
    HetongTable,
    KongweiTable,
    PingfenTable,
    Ref,
    ShijianTable,
    force_manual_if_no_page,
)

_REF_HINT = re.compile(r"(前附表|资料表|详见第|见第.{0,8}章|按第.{0,8}章|见本招标文件)")
_STAR_RE = re.compile(r"[★▲＊*]")
# 网页核对时由 run_check 设上；命令行不设，_emit 直接返回
_progress_cb: ContextVar = ContextVar("tender_progress", default=None)

# 进程级 LLM 并发闸：表内并发 × 文档级并发会叠乘，超了服务商的并发额度就整份失败。
# deepseek 的额度跟余额挂钩（曾报「concurrency is 5, limit 5」），默认压到 4，可用
# TENDER_LLM_CONCURRENCY 调。
_llm_sem: threading.Semaphore | None = None
_llm_sem_lock = threading.Lock()


def _semaphore() -> threading.Semaphore:
    global _llm_sem
    if _llm_sem is None:
        with _llm_sem_lock:
            if _llm_sem is None:
                try:
                    limit = max(1, int(os.environ.get("TENDER_LLM_CONCURRENCY", "4")))
                except ValueError:
                    limit = 4
                _llm_sem = threading.Semaphore(limit)
    return _llm_sem


def _emit(stage: str, state: str, detail: str = "", **payload) -> None:
    """通知当前阶段。没有回调时不做任何事。"""
    cb = _progress_cb.get()
    if cb is None:
        return
    event = {"stage": stage, "state": state, "detail": detail}
    event.update(payload)
    cb(event)


def _merge_dict(left: dict, right: dict) -> dict:
    """丢弃/降级记录按表名合并，后面的节点不覆盖前面的。"""
    return {**(left or {}), **(right or {})}


class CheckState(TypedDict, total=False):
    pdf_path: str
    company: dict
    index: TenderIndex
    missing: list
    feibiao: list
    pingfen: list
    cailiao: list
    kongwei: list
    shijian: list
    hetong: list
    #: 整行空被丢掉的行：{表名: [{missing, raw}, ...]}
    dropped: Annotated[dict, _merge_dict]
    #: 缺关键字段、降级成人工看的行：{表名: [{missing, raw}, ...]}
    degraded: Annotated[dict, _merge_dict]


def load_company(path) -> dict:
    """读取 company.yaml。"""
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _topk(default: int) -> int:
    """检索条数。段落单位校准出来的默认值是 4–6；换成条款单位后每条更短，
    必须调大才能覆盖同样多的信息，用 `TENDER_TOPK` 覆盖。"""
    try:
        return max(1, int(os.environ.get("TENDER_TOPK") or default))
    except ValueError:
        return default


#: 命中素材的总字数上限。k 调大以后 12 条 × 每条几百字会直接压垮结构化输出
#: （实测 k=12 时模型连续 3 次返回空结果）
HIT_BLOB_MAX_CHARS = 8000


def _hit_blob(hits: list[dict]) -> str:
    """命中拼成提示词。条款模式优先用 `context`（父标题链 + 本条），
    证据仍取 `text`——模型看全链，但 refs 只引命中那一条。

    总长度封顶，超出就丢掉靠后的命中（top-k 已经按相关度排过）。
    """
    parts = []
    used = 0
    for i, h in enumerate(hits, start=1):
        body = h.get("context") or h.get("text") or ""
        block = (
            f"[{i}] 第{h.get('page')}页 {h.get('section_title')}\n"
            f"bbox={h.get('bbox')}\n{body}"
        )
        if parts and used + len(block) > HIT_BLOB_MAX_CHARS:
            break
        parts.append(block)
        used += len(block)
    return "\n\n".join(parts) if parts else "（本章无检索结果）"


def _ref_from_hit(h: dict, clause: str = "") -> Ref:
    return Ref(
        page=h.get("page"),
        chapter=h.get("section_title") or "",
        clause=clause,
        text=(h.get("text") or "")[:240],
        bbox=list(h.get("bbox") or []),
    )


def _extract(schema, system: str, hits: list[dict], extra: str = ""):
    """用结构化输出从检索段落抽表。失败重试两次——并发上来以后 429 会变常见。"""
    llm = get_llm().with_structured_output(schema, method="function_calling")
    prompt = (
        system
        + "\n\n只使用下面的参考资料。每条 refs 必须能对上资料中的页码。"
        + "资料不够或页码不确定时，结论用「人工看」。不要编造条款。\n\n"
        + extra
        + "\n参考资料：\n"
        + _hit_blob(hits)
    )
    last = None
    for attempt in range(5):
        try:
            with _semaphore():  # 卡片级限流：并发到顶就排队，不直接 429
                result = llm.invoke(prompt)
            if result is None:
                # 模型偶发返回空结构化结果：当失败重试，别把 None 递给下游。
                # 命中条目多（k 调大）时更容易出现，所以重试给到 5 次。
                raise ValueError("模型返回空结构化结果")
            return result
        except Exception as exc:  # 网络/限流/结构化输出偶发失败
            last = exc
            if attempt == 4:
                break
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"模型调用连续失败 5 次：{last}")


def _follow(index: TenderIndex, rows: list, types: list[str]) -> None:
    """抽到「见前附表 / 详见第X章」时再搜一次，补第二条出处。"""
    for row in rows:
        blob = ""
        if hasattr(row, "condition"):
            blob += row.condition
        if hasattr(row, "item"):
            blob += str(row.item)
        for r in row.refs:
            blob += r.text + r.clause
        if not _REF_HINT.search(blob):
            continue
        q = _REF_HINT.search(blob).group(0)
        extra = index.search(q + " " + blob[:40], types, k=_topk(2))
        have = {(r.page, r.text[:20]) for r in row.refs}
        for h in extra:
            key = (h.get("page"), (h.get("text") or "")[:20])
            if key in have:
                continue
            row.refs.append(_ref_from_hit(h, "follow_reference"))
            have.add(key)


def _stamp_pages(rows: list) -> None:
    """没页码的行改成人工看。"""
    for row in rows:
        if hasattr(row, "verdict") and row.verdict:
            row.verdict = force_manual_if_no_page(row.refs, row.verdict)


def node_feibiao(state: CheckState) -> dict:
    """废标：搜邀请/须知/资格/评标办法/采购需求/格式六章，用后果词检索。"""
    print("  [废标] 检索六章（邀请/须知/资格/评标/需求/格式）", flush=True)
    _emit("feibiao", "running", "检索六章废标线索")
    types = ["invite", "notice", "qualify"]
    miss = [TYPE_LABEL[t] for t in required_types_present(state["index"].sections, types)]
    hits = state["index"].search(
        "投标无效 废标 资格 保证金 有效期 联合体 最高限价 实质性",
        types,
        k=_topk(5),
    )
    extra = "公司情况：\n" + yaml.safe_dump(state["company"], allow_unicode=True)
    extra += "\n抽出会导致投标无效或资格不合格的条件，对照公司情况给结论。"
    extra += "带 ★ 或 ▲ 的条款 star=true，放在列表前面。"
    table = _extract(FeibiaoTable, "你在抽取废标/资格条件。", hits, extra)
    _follow(state["index"], table.rows, types)
    already_star = {ref.page for row in table.rows if row.star for ref in row.refs}
    for h in hits:
        if _STAR_RE.search(h.get("text") or "") and h.get("page") not in already_star:
            table.rows.insert(0, FeibiaoRow(
                condition="带 ★ 的实质性条款（正则命中，待确认是否不满足即无效）",
                company="需逐条人工看",
                verdict="人工看",
                star=True,
                refs=[_ref_from_hit(h, "★")],
            ))
            break
    table.rows.sort(key=lambda r: (not r.star, r.verdict != "不满足"))
    _stamp_pages(table.rows)
    rows = [r.model_dump() for r in table.rows]
    print(f"  [废标] {len(rows)} 条", flush=True)
    _emit("feibiao", "done", f"{len(rows)} 条", rows=rows, missing=miss)
    return {
        "feibiao": rows,
        "missing": miss,
        "dropped": {"feibiao": table.dropped_rows},
        "degraded": {"feibiao": table.degraded_rows},
    }


def node_pingfen(state: CheckState) -> dict:
    """评分：只搜评标办法。"""
    print("  [评分] 检索评标办法，并调用模型", flush=True)
    _emit("pingfen", "running", "检索评标办法")
    types = ["score"]
    if required_types_present(state["index"].sections, types):
        print("  [评分] 未切到评标章", flush=True)
        rows = [{
            "item": "人工看：未切到评标章",
            "score": "",
            "standard": "",
            "company": "",
            "estimate": "",
            "refs": [],
        }]
        _emit("pingfen", "done", "未切到评标章", rows=rows)
        return {"pingfen": rows}
    hits = state["index"].search("评标标准 分值 价格分 商务 技术 业绩", types, k=_topk(6))
    extra = "公司情况：\n" + yaml.safe_dump(state["company"], allow_unicode=True)
    extra += "\n抽出评分项、分值、标准；能对照公司材料的写上预估，主观分写「主观分，不预估」。"
    table = _extract(PingfenTable, "你在抽取评分项。", hits, extra)
    _follow(state["index"], table.rows, types)
    rows = [r.model_dump() for r in table.rows]
    print(f"  [评分] {len(rows)} 条", flush=True)
    _emit("pingfen", "done", f"{len(rows)} 条", rows=rows)
    return {
        "pingfen": rows,
        "dropped": {"pingfen": table.dropped_rows},
        "degraded": {"pingfen": table.degraded_rows},
    }


def node_cailiao(state: CheckState) -> dict:
    """材料：须知 + 资格 + 格式，对照 YAML。"""
    print("  [材料] 检索须知、资格、格式，并调用模型", flush=True)
    _emit("cailiao", "running", "检索须知、资格、格式")
    types = ["notice", "qualify", "format"]
    hits = state["index"].search("应提供 证明 证书 营业执照 保证金 授权委托 声明函", types, k=_topk(6))
    extra = "公司情况：\n" + yaml.safe_dump(state["company"], allow_unicode=True)
    extra += "\n列出必须提交的证明材料，对照公司有没有。代理机构代查的标「无须提供」。"
    table = _extract(CailiaoTable, "你在抽取投标须提交的材料。", hits, extra)
    _follow(state["index"], table.rows, types)
    _stamp_pages(table.rows)
    rows = [r.model_dump() for r in table.rows]
    print(f"  [材料] {len(rows)} 条", flush=True)
    _emit("cailiao", "done", f"{len(rows)} 条", rows=rows)
    return {
        "cailiao": rows,
        "dropped": {"cailiao": table.dropped_rows},
        "degraded": {"cailiao": table.degraded_rows},
    }


def node_kongwei(state: CheckState) -> dict:
    """空位：只搜格式章，只列要填的格，不代填。"""
    print("  [空位] 检索投标文件格式，并调用模型", flush=True)
    _emit("kongwei", "running", "检索投标文件格式")
    types = ["format"]
    if required_types_present(state["index"].sections, types):
        print("  [空位] 未切到投标文件格式章", flush=True)
        rows = [{
            "form": "人工看：未切到投标文件格式章",
            "field": "",
            "note": "",
            "refs": [],
        }]
        _emit("kongwei", "done", "未切到投标文件格式章", rows=rows)
        return {"kongwei": rows}
    hits = state["index"].search("投标函 开标一览表 偏离表 声明函 空白 实质性格式", types, k=_topk(6))
    extra = "只列出表格名称和要填的空位，不要替用户填写。"
    table = _extract(KongweiTable, "你在抽取投标文件格式里的空位。", hits, extra)
    _follow(state["index"], table.rows, types)
    rows = [r.model_dump() for r in table.rows]
    print(f"  [空位] {len(rows)} 条", flush=True)
    _emit("kongwei", "done", f"{len(rows)} 条", rows=rows)
    return {
        "kongwei": rows,
        "dropped": {"kongwei": table.dropped_rows},
        "degraded": {"kongwei": table.degraded_rows},
    }


def node_shijian(state: CheckState) -> dict:
    """时间节点。单独成节点，v2 才能和合同并发跑。"""
    print("  [时间] 调用模型", flush=True)
    _emit("shijian", "running", "抽取时间节点")
    hits = state["index"].search(
        "截止时间 开标 获取招标文件 有效期 质疑",
        ["invite", "notice"],
        k=_topk(4),
    )
    shijian = _extract(ShijianTable, "你在抽取时间节点。", hits)
    _follow(state["index"], shijian.rows, ["invite", "notice"])
    rows = [r.model_dump() for r in shijian.rows]
    print(f"  [时间] {len(rows)} 条", flush=True)
    _emit("shijian", "done", f"{len(rows)} 条", shijian=rows)
    return {
        "shijian": rows,
        "dropped": {"shijian": shijian.dropped_rows},
        "degraded": {"shijian": shijian.degraded_rows},
    }


def node_hetong(state: CheckState) -> dict:
    """合同要点。合同与废标分开，履约保证金不许进废标表。"""
    print("  [合同] 调用模型", flush=True)
    _emit("hetong", "running", "抽取合同要点")
    hits = state["index"].search(
        "预付款 履约保证金 支付 违约 验收 更换总监",
        ["contract", "format"],
        k=_topk(4),
    )
    hetong = _extract(
        HetongTable,
        "你在抽取合同要点。履约保证金不是投标保证金，不要和废标表混。",
        hits,
    )
    _follow(state["index"], hetong.rows, ["contract", "format"])
    rows = [r.model_dump() for r in hetong.rows]
    print(f"  [合同] {len(rows)} 条", flush=True)
    _emit("hetong", "done", f"{len(rows)} 条", hetong=rows)
    return {
        "hetong": rows,
        "dropped": {"hetong": hetong.dropped_rows},
        "degraded": {"hetong": hetong.degraded_rows},
    }


def node_aux(state: CheckState) -> dict:
    """辅助表：时间 + 合同。v1 走这里，顺序与拆分前一致。"""
    _emit("aux", "running", "抽取时间节点")
    a = node_shijian(state)
    _emit("aux", "running", "抽取合同要点")
    b = node_hetong(state)
    _emit(
        "aux",
        "done",
        f"时间 {len(a['shijian'])} 条，合同 {len(b['hetong'])} 条",
        shijian=a["shijian"],
        hetong=b["hetong"],
    )
    return {
        "shijian": a["shijian"],
        "hetong": b["hetong"],
        "dropped": _merge_dict(a.get("dropped") or {}, b.get("dropped") or {}),
        "degraded": _merge_dict(a.get("degraded") or {}, b.get("degraded") or {}),
    }


def build_graph():
    """四个固定节点加辅助表，顺序执行。"""
    g = StateGraph(CheckState)
    g.add_node("feibiao", node_feibiao)
    g.add_node("pingfen", node_pingfen)
    g.add_node("cailiao", node_cailiao)
    g.add_node("kongwei", node_kongwei)
    g.add_node("aux", node_aux)
    g.add_edge(START, "feibiao")
    g.add_edge("feibiao", "pingfen")
    g.add_edge("pingfen", "cailiao")
    g.add_edge("cailiao", "kongwei")
    g.add_edge("kongwei", "aux")
    g.add_edge("aux", END)
    return g.compile()


def run_check(pdf_path, company_path, on_progress=None) -> dict:
    """解析、切章、建库、跑四步，返回表数据。"""
    token = _progress_cb.set(on_progress) if on_progress is not None else None
    try:
        return _run_check(pdf_path, company_path)
    finally:
        if token is not None:
            _progress_cb.reset(token)


def _run_check(pdf_path, company_path) -> dict:
    """原有核对顺序：解析、切章、建库、四步出表。"""
    print(f"1/4 解析 {pdf_path}", flush=True)
    _emit("parse", "running", "按页抽文字和块坐标")
    pages = parse_pdf(pdf_path)
    print(f"    {len(pages)} 页", flush=True)
    _emit("parse", "done", f"{len(pages)} 页", page_count=len(pages))
    print("2/4 切章", flush=True)
    _emit("sections", "running", "按标题切章（书签优先）")
    sections = split_sections(pages, toc=pdf_toc(pdf_path))
    for sec in sections:
        label = TYPE_LABEL.get(sec["type"], sec["type"])
        print(f"    {label}  {sec['title']}  p.{sec['start_page']}-{sec['end_page']}", flush=True)
    _emit("sections", "done", f"{len(sections)} 章", sections=sections)
    print("3/4 建索引（向量化，要几分钟）", flush=True)
    _emit("index", "running", "开始按章建向量")

    def _on_chapter(label, count):
        _emit("index", "running", f"正在建库 {label} {count} 段")

    index = build_tender_index(pages, sections, pdf_path, on_chapter=_on_chapter)
    company = load_company(company_path)
    _emit(
        "index",
        "done",
        "索引建完",
        index=index,
        company=company,
        pdf_path=str(Path(pdf_path).resolve()),
    )
    print("4/4 调用模型出表", flush=True)
    graph = build_graph()
    out = graph.invoke({
        "pdf_path": str(pdf_path),
        "company": company,
        "index": index,
    })
    return {
        "pdf_path": str(Path(pdf_path).resolve()),
        "company": company,
        "sections": sections,
        "missing": out.get("missing") or [],
        "feibiao": out.get("feibiao") or [],
        "pingfen": out.get("pingfen") or [],
        "cailiao": out.get("cailiao") or [],
        "kongwei": out.get("kongwei") or [],
        "shijian": out.get("shijian") or [],
        "hetong": out.get("hetong") or [],
        "dropped": out.get("dropped") or {},
        "degraded": out.get("degraded") or {},
        "index": index,
        "pages": pages,
        "page_count": len(pages),
    }


def run_check_saved(pdf_path, company_path, on_progress=None) -> dict:
    """载入已有向量库，只跑四步出表。不重新解析、不重新向量化。"""
    token = _progress_cb.set(on_progress) if on_progress is not None else None
    try:
        return _run_check_saved(pdf_path, company_path)
    finally:
        if token is not None:
            _progress_cb.reset(token)


def _run_check_saved(pdf_path, company_path) -> dict:
    """用 data/tender_index 里这份 PDF 的库，接上原来的四步。"""
    print(f"载入已有向量库 {pdf_path}", flush=True)
    index = load_tender_index(pdf_path)
    sections = index.sections
    n_pages = page_count(pdf_path)
    _emit("parse", "done", f"{n_pages} 页", page_count=n_pages)
    _emit("sections", "done", f"{len(sections)} 章", sections=sections)
    company = load_company(company_path)
    _emit(
        "index",
        "done",
        "已载入已有向量库",
        index=index,
        company=company,
        pdf_path=str(Path(pdf_path).resolve()),
    )
    print("调用模型出表", flush=True)
    graph = build_graph()
    out = graph.invoke({
        "pdf_path": str(pdf_path),
        "company": company,
        "index": index,
    })
    return {
        "pdf_path": str(Path(pdf_path).resolve()),
        "company": company,
        "sections": sections,
        "missing": out.get("missing") or [],
        "feibiao": out.get("feibiao") or [],
        "pingfen": out.get("pingfen") or [],
        "cailiao": out.get("cailiao") or [],
        "kongwei": out.get("kongwei") or [],
        "shijian": out.get("shijian") or [],
        "hetong": out.get("hetong") or [],
        "dropped": out.get("dropped") or {},
        "degraded": out.get("degraded") or {},
        "index": index,
        "pages": [],
        "page_count": n_pages,
    }


def render_tables(result: dict) -> str:
    """核对结果的纯文本，终端和文件用同一份。"""
    lines = ["章节："]
    for s in result["sections"]:
        lines.append(
            f"  {TYPE_LABEL.get(s['type'], s['type'])}  {s['title']}  p.{s['start_page']}-{s['end_page']}"
        )
    if result.get("missing"):
        lines.append("缺失章节： " + "、".join(result["missing"]))

    def _block(title, rows, fields):
        lines.append(f"\n== {title} {len(rows)} 条 ==")
        for i, row in enumerate(rows, start=1):
            bits = [str(row.get(k, "")) for k in fields]
            refs = row.get("refs") or []
            pages = ",".join(str(r.get("page")) for r in refs if r.get("page"))
            tail = f"  [{pages}]" if pages else "  [无页码]"
            note = str(row.get("note") or "").strip()
            if note:
                tail += f"  备注:{note}"
            lines.append(f"{i}. " + " | ".join(bits) + tail)

    _block("废标对照", result["feibiao"], ["verdict", "condition"])
    _block("评分对照", result["pingfen"], ["item", "score", "estimate"])
    _block("材料对照", result["cailiao"], ["verdict", "material"])
    _block("空位对照", result["kongwei"], ["form", "field"])
    _block("时间节点", result["shijian"], ["name", "when"])
    _block("合同要点", result["hetong"], ["item", "content"])

    dropped = result.get("dropped") or {}
    degraded = result.get("degraded") or {}
    for key in dropped:
        n_drop = len(dropped.get(key) or [])
        n_deg = len(degraded.get(key) or [])
        if n_drop or n_deg:
            lines.append(f"\n[{key}] 丢弃空行 {n_drop} 条，缺字段降级 {n_deg} 条（明细见 tables.json）")
    return "\n".join(lines)


def print_tables(result: dict) -> None:
    """把核对结果打到终端。"""
    print(render_tables(result))
