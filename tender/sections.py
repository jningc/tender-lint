from __future__ import annotations

"""
按目录和标题把招标文件切成章，并标成固定类型。

类型：invite / notice / qualify / score / demand / contract / format / unknown

两件事和早先版本不同（实测两套政府采购标准文本得出）：

1. **一章可以有多个类型**。北京标准文本把「资格审查与评标办法」写成同一章，
   只认一个类型的话，资格审查整章消失——而 `qualify` 是废标条件最密集的来源。
   所以章节带 `types`，主类型 `type` 留给渲染层。
2. **同类型允许多章**。兵团 2026 示范文本把「评标方法及标准（综合评分法）」和
   「（最低评标价法）」都编成第五章，按类型去重会把第二套吞掉。

三份样本的章名不一样，靠别名表对齐，不写死某一份的目录。

切章策略：**默认正文「第X章」**；关键 type 缺失或章数明显偏少时，才用书签补洞。
"""

import re

# 章类型 -> 标题里会出现的词
TYPE_ALIASES = {
    "invite": ["投标邀请", "招标公告", "采购公告", "邀请书"],
    "notice": ["投标人须知", "供应商须知", "磋商须知", "须知资料表"],
    "qualify": ["资格审查", "资格要求", "资格预审"],
    "score": ["评标办法", "评标方法", "评标标准", "评分标准", "评标程序", "评审办法"],
    "demand": ["采购需求", "项目需求", "技术规格", "服务要求", "公开招标需求", "招标需求"],
    "contract": ["合同文本", "合同条款", "拟签订的合同", "合同草案", "合同主要条款", "合同格式"],
    "format": ["投标文件格式", "响应文件格式", "报价文件格式"],
}

TYPE_LABEL = {
    "invite": "投标邀请/招标公告",
    "notice": "投标人须知",
    "qualify": "资格审查",
    "score": "评标办法",
    "demand": "采购需求",
    "contract": "合同",
    "format": "投标文件格式",
    "unknown": "未分类",
}

#: 匹配顺序：更具体、更「重」的类型在前（一章多类型时主类型取第一个）
_MATCH_ORDER = ("format", "contract", "score", "qualify", "notice", "demand", "invite")

# 只认行首「第X章…」，不认节/部分/篇——「第二节投标人须知正文」会误切成独立章
_CHAPTER_LINE_RE = re.compile(
    r"^[\s　]*(第[一二三四五六七八九十百零0-9]+章)"
    r"[\s　]*([^\s　].{0,36})$"
)
#: 书签里认「第X章」（宽松：允许后面直接跟标题、没有空格）
_TOC_CHAP_RE = re.compile(r"^第[一二三四五六七八九十百零0-9]{1,4}章\s*\S")

# 检索分库常用类型；qualify 常并入须知，不算硬性缺失
_KEY_TYPES = ("invite", "notice", "score", "demand", "contract", "format")
# 正文章数比书签少至少这么多，视为「明显偏少」
_TOC_CHAPTER_GAP = 2


def _norm(text: str) -> str:
    return re.sub(r"[\s\u3000]+", "", text or "")


def _locate_toc_page(pages: list[dict], title: str, nominal: int, span: int = 3) -> int | None:
    """书签页码常是印刷页（比物理页小 1–2），用标题去正文里校准。

    实测北京标准文本：书签写「第一章 投标邀请 p4」，物理页是 p5。
    不校准的话每章都错位，切出来的边界全歪。
    """
    target = _norm(title)
    by_page = {p["page"]: p for p in pages}
    for delta in range(span + 1):
        for pno in ((nominal + delta, nominal - delta) if delta else (nominal,)):
            page = by_page.get(pno)
            if page is None:
                continue
            for block in page.get("blocks") or []:
                text = _norm(block.get("text"))
                if text and text.startswith(target):
                    return pno
    return None


def _sections_from_toc(pages: list[dict], toc: list[dict]) -> list[dict]:
    """从 PDF 书签切章（供正文不足时补洞）。形如「第X章」的条目才算章。

    书签不受排版位置影响，但有两个毛病要处理：
      - 页码可能是印刷页（不是 PDF 物理页）→ `_locate_toc_page` 校准；
      - 可能把 Word 里的随机层级导进来（`（一）适用范围` 也标成 L1）→ 只认「第X章」。
    默认路径仍是正文；见 `split_sections`。
    """
    entries = []
    seen = set()
    for entry in toc or []:
        title = (entry.get("title") or "").strip()
        if not _TOC_CHAP_RE.match(_norm(title)):
            continue
        key = _norm(title)
        if key in seen:
            continue
        seen.add(key)
        entries.append(entry)
    if len(entries) < 2:
        return []

    located = []
    for entry in entries:
        nominal = int(entry.get("page") or 1)
        pno = _locate_toc_page(pages, entry["title"], nominal) or nominal
        types = classify_title_all(entry["title"])
        if not types:
            continue
        located.append({
            "page": pno, "title": entry["title"].strip(),
            "type": types[0], "types": types,
        })
    if len(located) < 2:
        return []

    located.sort(key=lambda x: x["page"])
    last_page = pages[-1]["page"]
    sections = []
    for i, h in enumerate(located):
        end = located[i + 1]["page"] - 1 if i + 1 < len(located) else last_page
        sections.append({
            "type": h["type"], "types": h["types"], "title": h["title"],
            "start_page": h["page"], "end_page": max(end, h["page"]),
        })
    return sections


def classify_title_all(title: str) -> list[str]:
    """标题命中的所有类型，按 `_MATCH_ORDER` 排序；空列表表示对不上。"""
    text = title.replace(" ", "")
    hits = []
    for typ in _MATCH_ORDER:
        for alias in TYPE_ALIASES[typ]:
            if alias.replace(" ", "") in text:
                hits.append(typ)
                break
    return hits


def classify_title(title: str) -> str:
    """主类型（兼容旧调用）：一章多类型时取排序最靠前的那个。"""
    hits = classify_title_all(title)
    return hits[0] if hits else "unknown"


def _heading_candidates(pages: list[dict]) -> list[dict]:
    """从每页靠前的块里找章标题。同一章可以有多个类型，同类型也可以出现多章。"""
    found = []
    for page in pages:
        early = [b["text"].replace("\n", "").strip() for b in page.get("blocks", [])[:12]]
        chapter_hits = [t for t in early if _CHAPTER_LINE_RE.match(t)]
        # 目录页同一页堆多条「第X章」，跳过，等正文
        if len(chapter_hits) >= 3:
            continue
        for block in page.get("blocks", [])[:10]:
            line = block["text"].replace("\n", "").strip()
            if "见第" in line or "详见" in line or "按照第" in line:
                continue
            m = _CHAPTER_LINE_RE.match(line)
            if not m:
                continue
            types = classify_title_all(line)
            if not types:
                continue
            found.append({
                "page": page["page"],
                "title": line,
                "type": types[0],
                "types": types,
            })
            break
    return found


def _sections_from_text(pages: list[dict]) -> list[dict]:
    """正文正则切章（默认路径）。"""
    heads = _heading_candidates(pages)
    cleaned = []
    for h in heads:
        # 目录页常把所有章堆在第 1～4 页；同页出现三个以上不同类型则跳过这一条
        same_page = [x for x in heads if x["page"] == h["page"]]
        if h["page"] <= 4 and len({x["type"] for x in same_page}) >= 3:
            continue
        # 页面紧邻、主类型相同的重复条目（目录/页眉复现）跳过
        if cleaned and cleaned[-1]["type"] == h["type"] and h["page"] - cleaned[-1]["page"] <= 1:
            continue
        cleaned.append(h)

    if not cleaned:
        return [{
            "type": "unknown",
            "types": ["unknown"],
            "title": "全文未切出章节",
            "start_page": pages[0]["page"],
            "end_page": pages[-1]["page"],
        }]

    last_page = pages[-1]["page"]
    sections = []
    for i, h in enumerate(cleaned):
        end = cleaned[i + 1]["page"] - 1 if i + 1 < len(cleaned) else last_page
        sections.append({
            "type": h["type"],
            "types": h["types"],
            "title": h["title"],
            "start_page": h["page"],
            "end_page": max(end, h["page"]),
        })
    return sections


def _text_needs_toc(from_text: list[dict], from_toc: list[dict]) -> bool:
    """正文是否需要书签补洞：关键 type 缺失，或章数明显偏少。"""
    if not from_toc:
        return False
    real = [s for s in from_text if s.get("type") != "unknown"]
    if not real:
        return True
    have: set[str] = set()
    for sec in real:
        have.update(types_of(sec))
    if any(t not in have for t in _KEY_TYPES):
        return True
    # 比书签少 _TOC_CHAPTER_GAP 章及以上
    if len(real) + _TOC_CHAPTER_GAP <= len(from_toc):
        return True
    return False


def _merge_sections(from_text: list[dict], from_toc: list[dict],
                    last_page: int) -> list[dict]:
    """正文给骨架，书签只补缺。

    书签页码/类型错了会整段带偏；所以默认相信正文。仅当正文关键类型缺失、
    章数明显偏少、或完全没切开时，才把书签里正文没有的类型按页插进去，
    再统一重算 end_page。
    """
    real = [s for s in from_text if s.get("type") != "unknown"]
    # 正文完全失败：整份改用书签
    merged = list(from_toc) if not real else list(real)
    have = {s["type"] for s in merged}
    if real:
        for sec in from_toc:
            if sec["type"] in have:
                continue
            merged.append(sec)
            have.add(sec["type"])
    merged.sort(key=lambda s: s["start_page"])
    for i, sec in enumerate(merged):
        end = merged[i + 1]["start_page"] - 1 if i + 1 < len(merged) else last_page
        sec["end_page"] = max(end, sec["start_page"])
    return merged


def split_sections(pages: list[dict], toc: list[dict] | None = None) -> list[dict]:
    """切章：默认正文；关键 type 缺失或章数明显偏少时，才用书签补。

    正文正则不受坏书签误导；书签在「正文漏章 / 章标题排版怪异」时补洞。
    """
    if not pages:
        return []
    from_text = _sections_from_text(pages)
    if not toc:
        return from_text
    from_toc = _sections_from_toc(pages, toc)
    if from_toc and _text_needs_toc(from_text, from_toc):
        return _merge_sections(from_text, from_toc, pages[-1]["page"])
    return from_text


def section_of_page(sections: list[dict], page: int) -> dict | None:
    """页码落到哪一章。"""
    for sec in sections:
        if sec["start_page"] <= page <= sec["end_page"]:
            return sec
    return None


def types_of(section: dict | None) -> list[str]:
    """章节的类型集合，兼容没有 `types` 字段的旧数据。"""
    if not section:
        return []
    return section.get("types") or [section.get("type") or "unknown"]


def required_types_present(sections: list[dict], types: list[str]) -> list[str]:
    """返回缺失的章类型。一章多类型时按并集算。"""
    have = set()
    for sec in sections:
        have.update(types_of(sec))
    return [t for t in types if t not in have]
