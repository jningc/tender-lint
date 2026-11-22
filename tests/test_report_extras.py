"""render_tables 追加块的渲染：只做纯文本，不碰 IO。"""

from __future__ import annotations

from tender.report_extras import collect, render


def test_collect_none_returns_empty():
    assert collect(None) == {}


def test_render_frontmatter_truncates_with_total():
    extras = {
        "frontmatter": [{
            "table_id": "x", "pages": [23],
            "items": [{"no": str(i), "name": f"项{i}", "value": "值", "page": 23, "bbox": []}
                      for i in range(20)],
        }],
        "matter": None,
    }
    lines = render(extras, limit=5)
    assert any("前附表 20 条" in l for l in lines)
    assert sum(1 for l in lines if l.strip().startswith("p23")) == 5
    assert any("另有 15 条" in l for l in lines)


def test_render_matter_marks_three_states():
    extras = {
        "frontmatter": [],
        "matter": {
            "name": "投标保证金", "coverage": "2/4",
            "fields": {
                "amount": {"value": "8000 元", "status": "有值",
                           "refs": [{"page": 8}]},
                "method": {"value": "本项目不收取", "status": "原文未要求", "refs": []},
                "verdict": {"value": "", "status": "待人工", "refs": []},
                "evidence": {"value": "", "status": "待人工", "refs": []},
            },
        },
    }
    text = "\n".join(render(extras))
    assert "多源覆盖 2/4 章" in text
    assert "8000 元" in text and "[p8]" in text
    assert "原文未要求" in text
    assert "（待人工）" in text
