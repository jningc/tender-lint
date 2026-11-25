"""前附表直读（B4-a）：证明「关键参数不用检索、不用模型也能拿到」。

跑法:
    HF_HUB_OFFLINE=1 env -u PYTHONPATH ~/miniforge3/envs/report-rag/bin/python eval/demos/frontmatter_demo.py

输出三份样本各自的前附表条数，以及六个关键参数有没有读到、在第几页。
和三张表的现状对比：这些参数以前要么靠检索碰运气，要么根本没抽。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tender.frontmatter import extract  # noqa: E402
from tender.parse import parse_pdf  # noqa: E402
from tender.sections import section_of_page, split_sections  # noqa: E402
from tender.tabular import extract_tables  # noqa: E402

#: 关心的关键参数：名称关键词 → 归属哪张表
WANTED = [
    (("投标有效期",), "时间节点"),
    (("投标保证金", "金额"), "废标/材料"),
    (("保证金",), "废标/材料"),
    (("联合体",), "废标"),
    (("分包",), "废标"),
    (("解密",), "时间节点"),
    (("资格证明文件", "组成"), "材料"),
    (("服务期",), "合同"),
]

DOCS = ["beijing_road", "chongzuo_green", "wenling_video"]


def main() -> int:
    total_items = 0
    for stem in DOCS:
        pdf = ROOT / f"sample/{stem}.pdf"
        pages = parse_pdf(str(pdf))
        secs = split_sections(pages)
        types = {p["page"]: (section_of_page(secs, p["page"]) or {}).get("type") for p in pages}
        tables = extract_tables(str(pdf), stem=stem)
        fronts = extract(tables, types)

        print(f"\n{'='*72}\n{stem}")
        if not fronts:
            print("  没有前附表（这份文件的须知是正文式，关键参数在段落里）")
            continue
        for fm in fronts:
            total_items += len(fm)
            print(f"  {fm.table_id}  p{fm.pages}  {len(fm)} 条")

            seen = set()
            for kws, owner in WANTED:
                it = fm.first(*kws)
                if it is None or (it.no, it.name) in seen:
                    continue                    # 同一行被多个关键词命中，只报一次
                seen.add((it.no, it.name))
                val = it.value[:64].replace("\n", " ")
                print(f"    [{owner:<8}] {it.name:<16} → {val}  p{it.page}")

    print(f"\n{'='*72}\n前附表总条数：{total_items}  （全部零模型调用）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
