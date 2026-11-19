"""表格嵌套：两级表头 + 合并单元格归属 + Σ分项=合计。

题集 `eval/pingfen_rows.json` 的口径就按这里来：错的不是「行列数不对」，
而是「合计 10 分属于哪一项说不清」。所以这几条都盯着嵌套关系。
"""

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from tender.tabular import extract_tables, pingfen_tables  # noqa: E402

SAMPLE = PROJECT_ROOT / "sample"


@pytest.fixture(scope="module")
def bj_tables():
    return pingfen_tables(extract_tables(SAMPLE / "beijing_road.pdf", stem="beijing_road"))


def find(tables, table_id):
    return next(t for t in tables if t.id == table_id)


def test_two_level_header(bj_tables):
    """「标准分」是跨两列的粗表头，下一行才是「分项 / 合计」。"""
    t = find(bj_tables, "beijing_road:p33:2")
    assert len(t.header_levels) == 2
    assert t.header_levels[0][3] == "标准分"
    assert t.header_levels[0][4] == "标准分"      # 跨列标题要填进子列
    assert t.header_levels[1][3] == "分项"
    assert t.header_levels[1][4] == "合计"
    assert t.header == t.header_levels[-1]


def test_subheader_row_not_in_body(bj_tables):
    """子表头那行不能留成数据行，否则会当成一个评分项。"""
    t = find(bj_tables, "beijing_road:p33:2")
    assert len(t.rows) == 3
    assert t.rows[0].cells[1] == "1"
    assert t.rows[0].cells[3] == "4分"


def test_rowspan_only_on_source_row(bj_tables):
    """`商务部分（10分）` 跨 3 行、`10分` 跨 3 行，只记在合并的源行上。"""
    t = find(bj_tables, "beijing_road:p33:2")
    assert {"col": 0, "rowspan": 3, "colspan": 1} in t.rows[0].spans
    assert {"col": 4, "rowspan": 3, "colspan": 1} in t.rows[0].spans
    # 被覆盖的 2、3 行不该再报合并（值虽已继承，但结构上是被覆盖的）
    assert t.rows[1].spans == []
    assert t.rows[2].spans == []


def test_merged_values_inherited(bj_tables):
    """结构记在源行，值要继承到每一行——否则取数时缺字段。"""
    t = find(bj_tables, "beijing_road:p33:2")
    assert [r.cells[0] for r in t.rows] == ["商务部分（10分）"] * 3
    assert [r.cells[4] for r in t.rows] == ["10分"] * 3
    assert [r.cells[1] for r in t.rows] == ["1", "2", "3"]   # 序号列不继承


def test_no_false_colspan(bj_tables):
    """「评审标准」列很宽，不能被算成跨列。"""
    t = find(bj_tables, "beijing_road:p33:2")
    for row in t.rows:
        for span in row.spans:
            assert not (span["col"] == 5 and span["colspan"] > 1)


def test_single_level_header_not_split(bj_tables):
    """单级表头别硬拆：`评分项目/评分标准/说明` 没有第二级。"""
    t = find(bj_tables, "beijing_road:p33:1")
    assert len(t.header_levels) == 1
    assert t.header_levels[0] == ["评分项目", "评分标准", "说明"]


def test_rowspan_does_not_cross_pages(bj_tables):
    """跨页表的下半部分在下一页，rowspan 不能跨页算。"""
    t = find(bj_tables, "beijing_road:p33:3")
    for row in t.rows:
        for span in row.spans:
            covered = [r.page for r in t.rows]
            assert span["rowspan"] <= len(covered)
    # 每行的 rowspan 覆盖范围内必须是同一页
    for i, row in enumerate(t.rows):
        for span in row.spans:
            for k in range(1, span["rowspan"]):
                assert t.rows[i + k].page == row.page


def test_inherited_marks(bj_tables):
    """稠密的 cells + inherited 标记：值在每行都有，但只有源行是「自己声明的」。"""
    t = find(bj_tables, "beijing_road:p33:2")
    assert t.rows[0].inherited == [False] * 6
    assert t.rows[1].inherited == [True, False, False, False, True, False]
    assert t.rows[2].inherited == [True, False, False, False, True, False]


def test_own_excludes_inherited(bj_tables):
    """`own()` 只给本行自己写的值——求和要用它，不然合计会被加 3 遍。"""
    t = find(bj_tables, "beijing_road:p33:2")
    assert [r.own(0) for r in t.rows] == ["商务部分（10分）", "", ""]
    assert [r.own(4) for r in t.rows] == ["10分", "", ""]
    assert [r.own(3) for r in t.rows] == ["4分", "3分", "3分"]
    assert [r.own(1) for r in t.rows] == ["1", "2", "3"]


def test_sum_with_own_matches_total(bj_tables):
    """Σ分项（去继承）== 合计（去继承取第一个）——这条就是核对时要的那个数。"""
    import re
    t = find(bj_tables, "beijing_road:p33:2")
    items = [int(re.search(r"\d+", r.own(3)).group()) for r in t.rows if r.own(3)]
    totals = [int(re.search(r"\d+", r.own(4)).group()) for r in t.rows if r.own(4)]
    assert sum(items) == 10
    assert totals == [10]          # 只出现一次，不是三个 10


def test_cells_sparse_with_absolute_coords(bj_tables):
    """稀疏 cell 列表：被覆盖的位置不出现，源格带绝对 row/col。"""
    t = find(bj_tables, "beijing_road:p33:2")
    cells = {(c["row"], c["col"]): c for c in t.cells()}
    assert cells[(0, 0)]["rowspan"] == 3
    assert cells[(0, 4)]["rowspan"] == 3
    assert (1, 0) not in cells      # 被 rowspan=3 覆盖
    assert (2, 4) not in cells
    assert cells[(1, 1)]["text"] == "2"
    assert cells[(2, 3)]["text"] == "3分"


def test_grid_shape(bj_tables):
    """稠密二维：每行列数一致，值已继承。"""
    t = find(bj_tables, "beijing_road:p33:2")
    g = t.grid()
    assert len(g) == 3 and all(len(r) == 6 for r in g)
    assert g[1][0] == "商务部分（10分）"
    assert g[2][4] == "10分"


def test_html_column_count(bj_tables):
    """HTML 每行渲染出的列数必须等于表头列数。

    判据要算上**被上方 rowspan 占用的列**：第 2、3 行只写 4 个 td，
    但上行两个 `rowspan=3` 还压着 2 列，浏览器渲染出来仍是 6 列。
    第一版按「td 数 + colspan」校验，把对的 HTML 判成了错。
    """
    import re
    t = find(bj_tables, "beijing_road:p33:2")
    covered = set()
    for i, row in enumerate(t.rows):
        for span in row.spans:
            for dr in range(span["rowspan"]):
                for dc in range(span["colspan"]):
                    if dr or dc:
                        covered.add((i + dr, span["col"] + dc))
    body = t.to_html().split("<tbody>")[1]
    trs = re.findall(r"<tr>(.*?)</tr>", body, re.S)
    assert len(trs) == len(t.rows)
    for i, tr in enumerate(trs):
        n_td = tr.count("<td")
        extra = sum(int(x) - 1 for x in re.findall(r'colspan="(\d+)"', tr))
        occupied = sum(1 for j in range(t.col_count) if (i, j) in covered)
        assert n_td + extra + occupied == t.col_count, (
            f"第 {i} 行渲染 {n_td + extra + occupied} 列，应为 {t.col_count}")


def test_html_does_not_repeat_merged_td(bj_tables):
    """被合并覆盖的行只写 4 个 td，不重复写「商务部分」「10分」。"""
    import re
    t = find(bj_tables, "beijing_road:p33:2")
    body = t.to_html().split("<tbody>")[1]
    rows = re.findall(r"<tr>(.*?)</tr>", body, re.S)
    assert len(rows) == 3
    assert rows[0].count("<td") == 6
    assert rows[1].count("<td") == 4
    assert rows[2].count("<td") == 4


def test_html_header_span(bj_tables):
    """「标准分」写 colspan=2 + rowspan=2，第二行只出「分项/合计」。"""
    t = find(bj_tables, "beijing_road:p33:2")
    html = t.to_html()
    # 跨列的格只写 colspan，不能再 rowspan——否则第二行的分项/合计没地方放
    assert '<th colspan="2">标准分</th>' in html
    assert '<th rowspan="2">分类</th>' in html
    head = html.split("<thead>")[1].split("</thead>")[0]
    assert "<th>分项</th><th>合计</th>" in head


def test_category_cell_not_treated_as_header():
    """「技术部分（60分）」是分类列的值，不是列名。

    实测 beijing t008：表头原本被读成 `技术部分（60分）| 评审内容 | 评审标准`，
    真正的列名被挤进数据行，p34 顶端重复表头也被当成数据行。
    """
    from tender.tabular import extract_tables, pingfen_tables
    ts = pingfen_tables(extract_tables(SAMPLE / "beijing_road.pdf", stem="beijing_road"))
    t = next(x for x in ts if x.id == "beijing_road:p33:3")
    assert t.header == ["分类", "评审内容", "评审标准"]
    assert len(t.rows) == 8
    assert all(r.cells[0] == "技术部分（60分）" for r in t.rows)
    assert any(r.page == 34 for r in t.rows)


def test_repeat_header_row_dropped():
    """续页重复印的表头不算数据行。"""
    from tender.tabular import extract_tables, pingfen_tables
    ts = pingfen_tables(extract_tables(SAMPLE / "beijing_road.pdf", stem="beijing_road"))
    t = next(x for x in ts if x.id == "beijing_road:p33:3")
    for r in t.rows:
        assert r.cells[1:] != t.header[1:], f"重复表头行没删：{r.cells[:3]}"


def test_cell_cut_across_pages_joined():
    """同一单元格跨页被切开要拼回去，不能多出假数据行。

    实测 chongzuo t010：(5) 在 p46 末行最后一格，(6) 价格分公式在 p47 首行，
    前三列全空——原本是同一个单元格。
    """
    from tender.tabular import extract_tables, pingfen_tables
    ts = pingfen_tables(extract_tables(SAMPLE / "chongzuo_green.pdf", stem="chongzuo_green"))
    t = next(x for x in ts if x.id == "chongzuo_green:p46:1")
    # 三页接上以后共 9 行（p46 2 行 + p47 5 行 + p48 5 行，其中 3 处续行被拼回上一格）
    assert len(t.rows) == 9
    assert sorted({r.page for r in t.rows}) == [46, 47, 48]
    first = t.rows[0].cells[-1]
    assert "（5）" in first and "（6）" in first
    assert "（6）" not in t.rows[1].cells[-1]


def test_score_column_clean():
    """分值列只保留数字——窄列会把邻列的句末标点抓进来（`。2`、`，8`）。"""
    from tender.tabular import extract_tables, pingfen_tables
    ts = pingfen_tables(extract_tables(SAMPLE / "wenling_video.pdf", stem="wenling_video"))
    t = next(x for x in ts if x.id == "wenling_video:p19:1")
    scores = [t.score_of(r) for r in t.rows]
    # 允许空（表尾没有分值），但非空的一律只能是数字，不能带 `。`、`，`
    assert all(not s or s.replace("-", "").replace(".", "").isdigit() for s in scores), scores
    assert scores[:3] == ["2", "2", "2"]


def test_two_level_header_columns_are_right(bj_tables):
    """分项在第 4 列、合计在第 5 列——不能混。"""
    t = find(bj_tables, "beijing_road:p33:2")
    assert t.header_levels[1][3] == "分项"
    assert t.header_levels[1][4] == "合计"
    assert [r.cells[3] for r in t.rows] == ["4分", "3分", "3分"]
    assert {r.cells[4] for r in t.rows} == {"10分"}
