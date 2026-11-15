"""校验段：机械回查，不调模型。

四条检查，都是「能算就别问模型」：

| 检查 | 怎么判 | 退回后果（enforce=True） |
|---|---|---|
| `page_exists` | 页码在 1..page_count | 该行判「人工看」 |
| `text_contains` | 条款里抠出的数值，能在引用页原文里找到（8000 / 8,000 / 捌仟 / 0.8万 归一后比） | 判「人工看」 |
| `cross_ref_section` | 「详见第X章」指的那一章真实存在 | 判「人工看」 |
| `table_exclusive` | 履约 / 质量保证金不算废标条件（除非原文写明不提交即无效、或已判不满足） | 从错表移出，记进 dropped |

`enforce=False` 只记录不改表：阶段 2 靠它保证「等价拆段」，阶段 4 起默认开。
"""

import json
import re
from pathlib import Path

from tender.contracts import Check, Verified
from tender.stages.compare_stage import CompareResult
from tender.stages.extract_stage import ExtractResult

# 金额/比例/天数：从条款原文里抠可回查的数值
_AMOUNT_RE = re.compile(r"(\d[\d,，]*(?:\.\d+)?)\s*(万元|万|元)")
_PERCENT_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*%")
_DAYS_RE = re.compile(r"(\d+)\s*(?:个)?(?:日历天|天|日)")
_CN_AMOUNT_RE = re.compile(r"([零壹贰叁肆伍陆柒捌玖拾佰仟万亿]{2,})\s*元")
_CHAPTER_RE = re.compile(r"第([一二三四五六七八九十\d]+)章")

_CN_DIGITS = {"零": 0, "壹": 1, "贰": 2, "叁": 3, "肆": 4, "伍": 5, "陆": 6, "柒": 7, "捌": 8, "玖": 9,
              "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNITS = {"拾": 10, "佰": 100, "仟": 1000, "十": 10, "百": 100, "千": 1000}


# ---------- 数值归一 ----------

def cn_to_number(text: str) -> float | None:
    """中文数字 → 数值。覆盖 捌仟 / 壹万 / 壹仟贰佰 这类常见写法，别的返回 None。"""
    total, section, num = 0, 0, 0
    for ch in text:
        if ch in _CN_DIGITS:
            num = _CN_DIGITS[ch]
        elif ch in _CN_UNITS:
            section += (num or 1) * _CN_UNITS[ch]
            num = 0
        elif ch in ("万", "亿"):
            total += (section + num) * (10000 if ch == "万" else 100000000)
            section, num = 0, 0
        else:
            return None
    return float(total + section + num)


def _norm(s: str) -> str:
    """比对用：去空格、去千分位。"""
    return re.sub(r"[\s,，]", "", s or "")


def values_in(text: str) -> set[float]:
    """从一段文字里抠出所有可比对的数值。"""
    out: set[float] = set()
    for m in _AMOUNT_RE.finditer(text or ""):
        n = float(m.group(1).replace(",", "").replace("，", ""))
        out.add(n * 10000 if m.group(2) in ("万元", "万") else n)
    for m in _PERCENT_RE.finditer(text or ""):
        out.add(float(m.group(1).replace(",", "")))
    for m in _DAYS_RE.finditer(text or ""):
        out.add(float(m.group(1)))
    for m in _CN_AMOUNT_RE.finditer(text or ""):
        n = cn_to_number(m.group(1))
        if n:
            out.add(n)
    return out


def has_number(text: str, number: float) -> bool:
    """原文里有没有这个数：数字、千分位、万元写法、大写、以及抠出来的数值集合。"""
    digits = str(int(round(number))) if abs(number - round(number)) < 1e-9 else f"{number:g}"
    if digits in _norm(text):
        return True
    if number >= 10000 and "万" in text and f"{number / 10000:g}" in _norm(text):
        return True
    return any(abs(n - number) < 1e-6 for n in values_in(text))


# ---------- 四条检查 ----------

def check_page(finding, page_count: int | None) -> Check:
    pages = [e.page for e in finding.evidence]
    ok = all(1 <= p <= (page_count or 10 ** 6) for p in pages)
    return Check(name="page_exists", ok=ok, detail="、".join(f"p{p}" for p in pages))


def check_text(finding, texts: dict[int, str], values: set[float]) -> Check:
    """条款里抠出的数值，要在引用页原文里找得到；抠不出数值就跳过。"""
    if not values:
        return Check(name="text_contains", ok=True, detail="条款无可比对数值，跳过")
    joined = "\n".join(texts.get(e.page, "") for e in finding.evidence)
    missing = sorted(v for v in values if not has_number(joined, v))
    if missing:
        return Check(name="text_contains", ok=False, detail=f"引用页原文里找不到 {missing}")
    return Check(name="text_contains", ok=True, detail=f"比对 {sorted(values)}")


def check_cross_ref(clause, sections: list) -> Check:
    """「详见第X章」指的那一章得真实存在；没引用算通过。"""
    m = _CHAPTER_RE.search(clause.text or "")
    if not m:
        return Check(name="cross_ref_section", ok=True, detail="无章号引用")
    raw = m.group(1)
    num = int(raw) if raw.isdigit() else int(cn_to_number(raw) or 0)
    existing = set()
    for s in sections or []:
        t = getattr(s, "title", "") or ""
        mm = _CHAPTER_RE.search(t)
        if not mm:
            continue
        v = mm.group(1)
        existing.add(int(v) if v.isdigit() else int(cn_to_number(v) or 0))
    ok = num in existing if existing else num >= 1
    return Check(name="cross_ref_section", ok=ok,
                 detail=f"引用第{raw}章，现存章号 {sorted(existing)}")


def check_table_exclusive(clause, finding) -> Check:
    """履约/质量保证金是合同约束，不是废标条件——除非原文写明不提交即无效。"""
    text = clause.text or ""
    if not any(k in text for k in ("履约保证金", "质量保证金")) or finding.table != "废标对照":
        return Check(name="table_exclusive", ok=True, detail="不涉及")
    invalid = ("投标无效", "废标", "不提交", "未提交", "拒不提交", "拒绝提交")
    if any(w in text for w in invalid) or finding.verdict == "不满足":
        return Check(name="table_exclusive", ok=True, detail="原文写明不满足即无效，保留")
    return Check(name="table_exclusive", ok=False,
                 detail="履约/质量保证金是合同约束，不该进废标表")


# ---------- 入口 ----------

def run(cmp: CompareResult, ex: ExtractResult, texts: dict[int, str] | None = None,
        page_count: int | None = None, sections: list | None = None,
        enforce: bool = False) -> list:
    """逐条回查，返回 list[Verified]。"""
    clause_of = {c.id: c for c in ex.clauses}
    texts = texts or {}
    out = []
    for f in cmp.findings:
        clause = clause_of.get(f.clause_id)
        checks = [check_page(f, page_count)]
        if clause is not None:
            checks.append(check_text(f, texts, values_in(clause.text)))
            checks.append(check_cross_ref(clause, sections or []))
            checks.append(check_table_exclusive(clause, f))
        ok = all(c.ok for c in checks)
        reason = next((c.detail for c in checks if not c.ok), "")
        try:
            out.append(Verified(finding_id=f.clause_id, ok=ok, checks=checks,
                                reject_reason="" if ok else f"{f.clause_id}：{reason}"))
        except Exception as e:
            out.append({"finding_id": f.clause_id, "error": str(e)[:120]})
    return out


def scan_exclusive(ex: ExtractResult) -> list[dict]:
    """行级扫串表：履约/质量保证金不该出现在废标对照。

    必须按**行**扫而不是按 finding 扫——没有 bbox 的行进不了契约，也就不会变成
    Finding，只查 finding 会让这类行整批逃过校验（44% 的行没有框）。
    """
    out = []
    invalid = ("投标无效", "废标", "不提交", "未提交", "拒不提交", "拒绝提交")
    for table, rows in ex.rows.items():
        if table != "feibiao":
            continue
        for row in rows:
            text = "；".join(str(row.get(k) or "") for k in ("condition", "note", "company"))
            if not any(k in text for k in ("履约保证金", "质量保证金")):
                continue
            if any(w in text for w in invalid) or row.get("verdict") == "不满足":
                continue
            out.append({"row": row, "reason": "履约/质量保证金是合同约束，不该进废标表"})
    return out


def apply(verified: list, ex: ExtractResult, enforce: bool = True) -> dict:
    """按回查结果改表：证据不足 → 判「人工看」；串表 → 从错表移出。"""
    changes = {"enforced": enforce, "downgraded": [], "moved_out": []}
    if not enforce:
        return changes

    # 1) 行级串表（不依赖 bbox/finding）
    flagged = {id(item["row"]): item["reason"] for item in scan_exclusive(ex)}
    for table, rows in ex.rows.items():
        keep = []
        for row in rows:
            if id(row) in flagged:
                changes["moved_out"].append(
                    {"row": {k: row.get(k) for k in ("condition", "material", "item") if row.get(k)},
                     "table": table, "reason": flagged[id(row)]})
                ex.dropped.setdefault(table, []).append(
                    {"missing": ["verdict"], "raw": row, "reason": "table_exclusive"})
                continue
            keep.append(row)
        ex.rows[table] = keep

    # 2) 证据不足（只对进了契约的行：它们才有 finding 与 clause）
    failed = {v.finding_id: [c.name for c in v.checks if not c.ok]
              for v in verified if getattr(v, "ok", None) is False}
    for cid, names in failed.items():
        if "table_exclusive" in names:
            continue
        row = ex.row_of.get(cid)
        if row is None or "verdict" not in row:
            continue
        row["verdict"] = "人工看"
        tag = "校验退回：" + "、".join(names)
        row["note"] = f"{row.get('note') or ''}；{tag}".strip("；")
        changes["downgraded"].append({"clause_id": cid, "checks": names})
    return changes


def summary(verified: list) -> dict:
    ok = sum(1 for v in verified if getattr(v, "ok", False))
    return {"checked": len(verified), "passed": ok, "rejected": len(verified) - ok}


def dump(verified: list, folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    payload = [v.model_dump() if hasattr(v, "model_dump") else v for v in verified]
    (folder / "verify.verified.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def dump_changes(changes: dict, folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "verify.changes.json").write_text(
        json.dumps(changes, ensure_ascii=False, indent=2), encoding="utf-8")
