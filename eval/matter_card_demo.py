"""保证金事项卡演示（B4-b）：一个事项散在 4 章，拼成一张卡。

跑法:
    HF_HUB_OFFLINE=1 env -u PYTHONPATH ~/miniforge3/envs/report-rag/bin/python eval/matter_card_demo.py
    ... --json    # 同时把卡写到 data/step_output/<stem>/matters.json

看三样东西：
1. **多源覆盖率**：这张卡的证据落到了几章 / 期望几章
2. **每个字段的来源章与页码**（不同字段来自不同章，这才是「多源」）
3. **扫过多少条款**：半穷尽是「过滤」不是「检索」，扫过的数量可证明
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tender.frontmatter import extract as extract_frontmatter  # noqa: E402
from tender.index import load_tender_index  # noqa: E402
from tender.matters import build_deposit_card  # noqa: E402
from tender.parse import parse_pdf  # noqa: E402
from tender.sections import section_of_page, split_sections  # noqa: E402
from tender.tabular import extract_tables  # noqa: E402

DOCS = ["beijing_road", "chongzuo_green", "wenling_video"]
FIELD_LABEL = {
    "amount": "金额",
    "method": "交纳方式/到账",
    "verdict": "无效后果",
    "evidence": "凭证/担保函模板",
}


def main() -> int:
    want_json = "--json" in sys.argv
    for stem in DOCS:
        pdf = ROOT / f"sample/{stem}.pdf"
        pages = parse_pdf(str(pdf))
        secs = split_sections(pages)
        types = {p["page"]: (section_of_page(secs, p["page"]) or {}).get("type") for p in pages}
        fronts = extract_frontmatter(extract_tables(str(pdf), stem=stem), types)
        index = load_tender_index(pdf)

        card = build_deposit_card(index, fronts)
        print(f"\n{'='*74}\n{stem}   多源覆盖 {card.coverage()}")

        for key, label in FIELD_LABEL.items():
            f = card.fields[key]
            mark = "✓" if f.status == "有值" else "·"
            pages_ = sorted({r["page"] for r in f.refs if r.get("page")})
            layers = sorted({r.get("role") or "" for r in f.refs if r.get("role")})
            head = f.value[:52].replace("\n", " ") if f.value else "（待人工）"
            print(f"  {mark} {label:<12} {head}")
            if pages_:
                print(f"      └ {len(f.refs)} 处 · p{pages_} · {'+'.join(layers)}")

        scanned = {}
        for k, v in card.scanned.items():
            typ = k.split(":", 1)[1]
            scanned[typ] = max(scanned.get(typ, 0), v)
        if scanned:
            print(f"  扫过条款：{scanned}")

        if want_json:
            out = ROOT / "data/step_output" / stem
            out.mkdir(parents=True, exist_ok=True)
            (out / "matters.json").write_text(
                json.dumps([card.to_dict()], ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"  → {out/'matters.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
