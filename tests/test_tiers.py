"""废标行的分档与分类：判据全在原文措辞上，可对照复核。"""

from __future__ import annotations

from types import SimpleNamespace

from tender.tiers import fill, kind_of, tier_of


def _row(condition="", text="", verdict="人工看", refs=None):
    if refs is None:
        refs = [SimpleNamespace(text=text)] if text else []
    return SimpleNamespace(condition=condition, note="", verdict=verdict,
                           refs=refs, kind="", tier="", star=False)


# ---------- 档 ----------

def test_tier_must_fail_on_explicit_invalidity():
    assert tier_of(_row(text="投标有效期不足的，其投标无效。")) == "必废"
    assert tier_of(_row(text="资格审查不合格的")) == "必废"


def test_tier_must_fail_on_disqualification():
    assert tier_of(_row(text="被列入失信名单的，不得参加政府采购活动")) == "必废"


def test_tier_conditional_when_prefixed_by_ru():
    """「如…否则投标无效」是附条件，不是无条件必废。"""
    r = _row(text="如第五章要求提供证明文件的，投标人应当按具体要求提供，否则投标无效。")
    assert tier_of(r) == "需确认"


def test_tier_must_fail_when_conditional_but_unconditional_else():
    """没有条件引子的「否则投标无效」仍是必废。"""
    r = _row(text="未按上述要求缴纳投标保证金的，其投标无效。")
    assert tier_of(r) == "必废"


def test_tier_not_bidder():
    assert tier_of(_row(text="投标人不足 3 家的，不予开标。")) == "非投标人"


def test_tier_obligation_without_consequence_is_confirm():
    r = _row(text="投标人应按照招标文件要求提交投标文件并保证资料真实。")
    assert tier_of(r) == "需确认"


# ---------- 类 ----------

def test_kind_qualification():
    assert kind_of(_row(text="拟派总监理工程师具备注册监理工程师证书")) == "资格门槛"
    assert kind_of(_row(text="近三年类似业绩")) == "资格门槛"


def test_kind_invalidity():
    assert kind_of(_row(text="提供选择性报价的，其投标无效")) == "无效情形"


# ---------- 批量 ----------

def test_fill_sets_both_and_is_idempotent():
    rows = [
        _row(text="投标有效期不足，其投标无效"),
        _row(text="拟派总监需具备注册监理工程师证书"),
    ]
    fill(rows)
    assert rows[0].tier == "必废" and rows[0].kind == "无效情形"
    assert rows[1].kind == "资格门槛"
    before = [(r.tier, r.kind) for r in rows]
    fill(rows)                       # 再跑一次不能变
    assert [(r.tier, r.kind) for r in rows] == before


def test_fill_keeps_preexisting_values():
    """模型已经给了 tier 就不覆盖（人工/上游优先）。"""
    row = _row(text="投标无效")
    row.tier = "手工标"
    fill([row])
    assert row.tier == "手工标"


# ---------- 派生：role 与 客观/主观 ----------

def test_stamp_roles_is_idempotent_and_respects_existing():
    from tender.tiers import stamp_roles
    refs = [SimpleNamespace(role=""), SimpleNamespace(role="金额")]
    rows = [SimpleNamespace(refs=refs)]
    stamp_roles(rows, "无效句")
    assert [r.role for r in refs] == ["无效句", "金额"]      # 已有的不覆盖
    stamp_roles(rows, "条款")
    assert refs[0].role == "无效句"


def test_feibiao_roles_split_by_kind():
    from tender.tiers import stamp_feibiao_roles
    a = SimpleNamespace(kind="资格门槛", refs=[SimpleNamespace(role="")])
    b = SimpleNamespace(kind="无效情形", refs=[SimpleNamespace(role="")])
    stamp_feibiao_roles([a, b])
    assert a.refs[0].role == "资格句"
    assert b.refs[0].role == "无效句"


def test_nature_objective():
    from tender.tiers import nature_of
    assert nature_of(_row_std("具有近三年类似业绩，每个 2 分，满分 4 分。")) == "客观"
    assert nature_of(_row_std("三体系认证，有一个得 1 分，满分 3 分。")) == "客观"


def test_nature_subjective():
    from tender.tiers import nature_of
    assert nature_of(_row_std("方案合理、内容完整，得 8 分。")) == "主观"
    assert nature_of(_row_std("进度计划详细、可行，得 6 分。")) == "主观"


def test_subjective_score_is_not_estimated():
    """主观分不能给预估——评委会裁量，模型估值会被当真。"""
    from tender.tiers import fill_pingfen
    rows = [
        SimpleNamespace(standard="方案合理、内容完整，得 8 分", nature="", estimate="8 分", item="实施方案", refs=[]),
        SimpleNamespace(standard="每个 2 分，满分 4 分", nature="", estimate="4 分", item="业绩", refs=[]),
    ]
    fill_pingfen(rows)
    assert rows[0].nature == "主观" and rows[0].estimate == ""
    assert rows[1].nature == "客观" and rows[1].estimate == "4 分"


def _row_std(std):
    return SimpleNamespace(standard=std, estimate="", item="", nature="", refs=[])
