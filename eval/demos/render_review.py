"""生成评分表行核对页（A1 用）。

把 `eval/demos/pingfen_rows.json`、`tabular` 的合并结构、`to_html` 的渲染合成一个本地页面：
每张表按原样渲染（合并、两级表头都在），每行前面一个标记格，点一下在
「未核 → 确认 → 有问题 → 未核」之间循环，顶部实时显示进度和问题清单。

不使用任何 CDN、不联网：拷到哪里都能开。

用法:
  python eval/demos/render_review.py            # 写到 eval/demos/review.html
  python eval/demos/render_review.py out.html
"""

from __future__ import annotations

import html as _html
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tender.tabular import extract_tables  # noqa: E402

CASES = ROOT / "eval" / "demos" / "pingfen_rows.json"
SAMPLE = ROOT / "sample"

STYLE = """
:root { color-scheme: light; }
body { font-family: -apple-system, "PingFang SC", "Helvetica Neue", sans-serif;
       color: #1d1d1f; background: #fff; max-width: 1180px; margin: 0 auto; padding: 28px 24px 80px; }
h1 { font-size: 20px; margin: 0 0 6px; }
.sub { color: #666; font-size: 13px; margin-bottom: 18px; }
#bar { position: sticky; top: 0; background: #fff; border-bottom: 1px solid #e5e5e5;
       padding: 10px 0; margin-bottom: 18px; font-size: 13px; z-index: 10; }
#bar b { font-variant-numeric: tabular-nums; }
#issues { color: #b3261e; }
.block { margin: 30px 0 42px; }
.block h2 { font-size: 15px; margin: 0 0 4px; }
.meta { color: #666; font-size: 12px; margin-bottom: 8px; }
.checks { color: #0b6b3a; font-weight: 600; }
table { border-collapse: collapse; width: 100%; font-size: 13px; }
th, td { border: 1px solid #cfcfcf; padding: 6px 9px; vertical-align: middle; text-align: left; }
th { background: #f6f6f7; font-weight: 600; }
td.mark { width: 34px; text-align: center; cursor: pointer; user-select: none;
          background: #fafafa; font-size: 15px; }
td.mark::after { content: "○"; color: #bbb; }
tr.ok td.mark { background: #e8f5ec; }
tr.ok td.mark::after { content: "✓"; color: #137333; font-weight: 700; }
tr.bad td.mark { background: #fdecea; }
tr.bad td.mark::after { content: "✕"; color: #b3261e; font-weight: 700; }
tr.ok td { background: #f7fcf8; }
tr.bad td:not(.mark) { background: #fff8f7; }
tr:hover td.mark { filter: brightness(0.96); }
.hint { color: #666; font-size: 12px; margin-top: 10px; }
"""

SCRIPT = """
const state = {};   // caseId -> 0 未核 / 1 确认 / 2 有问题
function paint() {
  let ok = 0, bad = 0, left = 0;
  document.querySelectorAll('tr[data-case]').forEach(tr => {
    const s = state[tr.dataset.case] || 0;
    tr.classList.remove('ok', 'bad');
    if (s === 1) { tr.classList.add('ok'); ok++; }
    else if (s === 2) { tr.classList.add('bad'); bad++; }
    else left++;
  });
  document.getElementById('done').textContent = ok;
  document.getElementById('badcount').textContent = bad;
  document.getElementById('left').textContent = left;
  const bads = Object.entries(state).filter(([, v]) => v === 2).map(([k]) => k);
  document.getElementById('issues').textContent = bads.length ? '有问题：' + bads.join('、') : '';
}
document.addEventListener('click', e => {
  const mark = e.target.closest('td.mark');
  if (!mark) return;
  const id = mark.parentElement.dataset.case;
  state[id] = ((state[id] || 0) + 1) % 3;
  paint();
});
paint();
"""


def _level_html(levels: list[list[str]], esc) -> str:
    """两级表头：粗一级跨列的写 colspan，不跨的写 rowspan=2；第二行只出子列。"""
    if not levels:
        return ""
    top = levels[0]
    group = []
    i = 0
    while i < len(top):
        j = i
        while j + 1 < len(top) and top[j + 1] == top[i]:
            j += 1
        group.append((i, j, top[i]))
        i = j + 1
    first = []
    for start, end, text in group:
        attrs = ""
        if end > start:
            # 跨列的格**不能** rowspan：它下面要留给第二级的子列（分项/合计）。
            # 两个都写的话，浏览器会把子列挤到右边，整张表错位。
            attrs += f' colspan="{end - start + 1}"'
        elif len(levels) > 1:
            attrs += ' rowspan="2"'
        first.append(f"<th{attrs}>{esc(text)}</th>")
    rows = [f"<th class='mark' rowspan='{max(len(levels), 1)}'></th>" + "".join(first)]
    if len(levels) > 1:
        second = []
        for start, end, _ in group:
            if end > start:
                for k in range(start, end + 1):
                    second.append(f"<th>{esc(levels[1][k] if k < len(levels[1]) else '')}</th>")
        if second:
            # 第二行不再加标记列：上面那个标记 th 已经 rowspan 盖住了
            rows.append("".join(second))
    return "".join(f"<tr>{r}</tr>" for r in rows)


def _body_html(table, case_ids: list[str], esc) -> str:
    """按行渲染，每行前面加标记格；被合并覆盖的位置不写 td。"""
    by_row: dict[int, list[dict]] = {}
    for cell in table.cells():
        by_row.setdefault(cell["row"], []).append(cell)
    out = []
    for i, row in enumerate(table.rows):
        cid = case_ids[i] if i < len(case_ids) else ""
        tds = []
        for cell in sorted(by_row.get(i, []), key=lambda c: c["col"]):
            attrs = ""
            if cell["rowspan"] > 1:
                attrs += f' rowspan="{cell["rowspan"]}"'
            if cell["colspan"] > 1:
                attrs += f' colspan="{cell["colspan"]}"'
            tds.append(f"<td{attrs}>{esc(cell['text'])}</td>")
        out.append(f"<tr data-case='{cid}'><td class='mark'></td>{''.join(tds)}</tr>")
    return "".join(out)


def build(out_path: Path) -> Path:
    payload = json.loads(CASES.read_text(encoding="utf-8"))
    cases = payload.get("cases") or []
    meta = {t["table_id"]: t for t in payload.get("tables") or []}
    esc = _html.escape

    tables = {}
    for doc in ("beijing_road", "chongzuo_green", "wenling_video"):
        pdf = SAMPLE / f"{doc}.pdf"
        if not pdf.exists():
            continue
        for t in extract_tables(pdf, stem=doc):
            if t.kind == "pingfen" and t.rows:
                tables[t.id] = t

    blocks = []
    for tid, t in tables.items():
        ids = [c["id"] for c in cases if c["table_id"] == tid]
        if not ids:
            continue
        m = meta.get(tid) or {}
        checks = m.get("checks") or {}
        check_txt = ""
        if "ok" in checks:
            mark = "✓" if checks["ok"] else "✗"
            check_txt = (f"<span class='checks'>{mark} Σ分项 "
                         f"{checks.get('sum_item')} = 合计 {checks.get('total')}</span>")
        levels_txt = " / ".join(" | ".join(lv) for lv in t.header_levels)
        blocks.append(f"""
<section class="block">
  <h2>{esc(tid)}　{t.col_count} 列 · {len(t.rows)} 行</h2>
  <div class="meta">页 {t.pages}　{check_txt}　表头：{esc(levels_txt)}</div>
  <table>
    <thead>{_level_html(t.header_levels, esc)}</thead>
    <tbody>{_body_html(t, ids, esc)}</tbody>
  </table>
</section>""")

    total = len(cases)
    doc = f"""<!doctype html>
<meta charset="utf-8">
<title>评分表行核对</title>
<style>{STYLE}</style>
<div id="bar">
  已确认 <b id="done">0</b> / <b id="left">{total}</b> 待核 ·
  有问题 <b id="badcount">0</b>　<span id="issues"></span>
</div>
<h1>评分表行核对（A1）</h1>
<div class="sub">点每行最左边的圆圈：○ 未核 → ✓ 确认 → ✕ 有问题 → ○。
共 {total} 行、{len(blocks)} 张表。核对完把「有问题」那一串 id 发给我就行。</div>
{''.join(blocks)}
<div class="hint">判据：这一行真实存在 · 文字与页面对得上 · 合并关系对得上（竖跨几行、横跨几列）。
`t006` 是单行表；`t008` 是跨页表，注意 p34 开头那行是重复表头还是数据行。</div>
<script>{SCRIPT}</script>
"""
    out_path.write_text(doc, encoding="utf-8")
    return out_path


def main(argv: list[str]) -> int:
    out = Path(argv[0]) if argv else ROOT / "eval" / "demos" / "review.html"
    path = build(out)
    print(f"写出 {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
