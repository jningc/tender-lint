"""时间节点全扫：正则找原文里所有「日期/时间 + 时间语境」，跟表里抽到的对比。

为什么先做这张表：答案是可枚举的字面形态（`2026年8月18日`、`09:00`），
正则找到的就是原文里真有的，**不涉及任何模型判断**。用它来量化
「检索路径漏了多少」，最便宜也最硬。

用法:
  python eval/time_scan.py                  # 三份都扫，对比 04_check.txt 里的时间节点表
  python eval/time_scan.py --detail         # 逐条列出
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tender.parse import parse_pdf  # noqa: E402

#: 日期：2026年8月18日 / 2026-08-18 / 8月18日
DATE = re.compile(r"\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日"
                  r"|\d{4}\s*[-/.]\s*\d{1,2}\s*[-/.]\s*\d{1,2}"
                  r"|\d{1,2}\s*月\s*\d{1,2}\s*日")
#: 时刻：09:00 / 9：30
CLOCK = re.compile(r"\d{1,2}\s*[:：]\s*\d{2}")
#: 期限规则：「15 日前」「7 个工作日内」「90 日历天」「30 分钟」
#: —— 招标文件里这类比具体日期还多，日期正则抓不到（表里 9 条有 6 条是这种）
DURATION = re.compile(r"\d+\s*个?\s*(?:日历)?(?:工作)?(?:日|天)(?:前|内|以上|以内|之内)"
                      r"|\d+\s*个?\s*(?:工作)?月(?:\s*内)?"
                      r"|\d+\s*(?:个)?\s*小时(?:\s*内)?"
                      r"|\d+\s*(?:个)?\s*分钟(?:\s*内)?")
#: 时间语境：命中日期/时刻的附近必须有这些词之一，否则大概率是无关日期
CONTEXT = re.compile(r"截止|开标|递交|提交|质疑|澄清|获取|报名|保证金|有效期"
                     r"|之前|前|起算|日历天|工作日内|顺延|答复")
#: 上下文窗口（字）
WINDOW = 40
#: 明显不是时间节点的语境（业绩年份、编制时间、印发日期）
NOISE = re.compile(r"业绩|近三?年|类似工程|编制时间|印发|版本|生效日期|成立于|注册于")

DOCS = ("beijing_road", "chongzuo_green", "wenling_video")


def scan(doc: str) -> list[dict]:
    """逐页扫，返回 [{page, match, context}]。"""
    hits = []
    for page in parse_pdf(f"sample/{doc}.pdf"):
        text = page.get("text") or ""
        flat = re.sub(r"\s+", " ", text)
        for pattern in (DATE, CLOCK, DURATION):
            for m in pattern.finditer(flat):
                start, end = max(0, m.start() - WINDOW), min(len(flat), m.end() + WINDOW)
                ctx = flat[start:end]
                if not CONTEXT.search(ctx):
                    continue
                if NOISE.search(ctx):
                    continue
                hits.append({
                    "page": page["page"],
                    "match": re.sub(r"\s+", "", m.group()),
                    "context": ctx.strip(),
                })
    return hits


def table_pages(doc: str) -> set[int]:
    """现有时间节点表里出现的页码（从 04_check.txt 的 `[4,6]` 里取）。"""
    path = ROOT / "data" / "step_output" / doc / "04_check.txt"
    if not path.is_file():
        return set()
    text = path.read_text(encoding="utf-8")
    m = re.search(r"== 时间节点[^\n]*\n(.*?)(?=\n== |\Z)", text, re.S)
    if not m:
        return set()
    pages: set[int] = set()
    for pm in re.finditer(r"\[([\d,\s]+)\]", m.group(1)):
        pages.update(int(x) for x in pm.group(1).split(",") if x.strip())
    return pages


def main(argv: list[str]) -> int:
    detail = "--detail" in argv
    grand_scan = grand_table = 0
    print(f"{'文档':<16}{'正则命中':>10}{'表里页':>8}{'表覆盖命中':>12}{'漏掉的页':>24}")
    for doc in DOCS:
        hits = scan(doc)
        pages = {h["page"] for h in hits}
        tpages = table_pages(doc)
        covered = pages & tpages
        missed = sorted(pages - tpages)
        grand_scan += len(hits)
        grand_table += len(covered)
        print(f"{doc:<16}{len(hits):>10}{len(tpages):>8}{len(covered):>12}"
              f"{str(missed[:6]):>24}")
        if detail:
            for h in hits:
                flag = "✓" if h["page"] in tpages else "✗"
                print(f"   {flag} p{h['page']:<4}{h['match']:<16}{h['context'][:60]}")
    print(f"\n合计：正则命中 {grand_scan} 处；表里出现过的页 {grand_table} 页"
          f"（命中页 / 正则页 = 覆盖率）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
