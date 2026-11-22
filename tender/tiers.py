"""废标行的两个派生属性：属于哪一类（kind）、有多确定（tier）。

设计依据 `sample/输出形式-落地版.md`：废标要拆两组（资格门槛 / 无效情形），
并且按**原文措辞**分确定性档——不是让模型说「这条严重」。

| 原文写法 | 档 |
|---|---|
| 投标无效 / 否决 / 不予受理 / 资格审查不合格 | 必废 |
| 不得参加 / 取消资格 / 拒绝 | 必废 |
| 「否则投标无效」但前面带条件（如…、视…） | 需确认 |
| 只写「应 / 须」但没写后果 | 需确认 |
| 主体不是投标人（不足 3 家、不予开标） | 非投标人 |

**全部是字符串判据，可对照原文复核。**
"""

from __future__ import annotations

#: 后果明确的写法 → 必废
MUST_FAIL = ("投标无效", "无效投标", "否决", "不予受理", "资格审查不合格",
             "资格审查不通过", "不得参加", "取消资格", "作无效标处理")
#: 主体不是投标人
NOT_BIDDER = ("投标人不足", "不足3家", "不足 3 家", "不予开标", "重新招标", "重新采购")
#: 条件式后果（前面可能带「如/若/视」）
CONDITIONAL = ("否则投标无效", "否则其投标无效", "否则作无效")
#: 条件引子
COND_WORDS = ("如", "若", "视", "如果", "存在下列")
#: 资格相关
QUALIFY = ("资格", "资质", "证书", "业绩", "人员", "任命书", "社保", "社会保险",
           "信用记录", "联合体", "营业执照")


def _text(row) -> str:
    """把一条废标行的所有可判文本拼起来：条件 + 备注 + 各条证据原文。"""
    parts = [getattr(row, "condition", "") or "", getattr(row, "note", "") or ""]
    for r in getattr(row, "refs", []) or []:
        parts.append(getattr(r, "text", "") or "")
    return " ".join(parts)


def tier_of(row) -> str:
    """必废 / 需确认 / 非投标人 / 已满足 / 其他。

    两条优先规则（实测补的，纯文本判据判不出来）：
    - `verdict=满足` 的行不参与分档，标「已满足」——它已经过了，没有风险
    - `verdict=不满足` 而文本里没有后果词：资格门槛直接算「必废」，
      其他算「需确认」。原因是不满足意味着投标人有问题，后果即使不在本句，
      也在同一条款的下一句里（「任何一项不符合…资格审查不合格，投标无效」）。
    """
    verdict = getattr(row, "verdict", "") or ""
    if verdict == "满足":
        return "已满足"
    if verdict == "无须提供":
        return "已满足"

    t = _text(row)
    if not t.strip():
        return "需确认" if verdict == "不满足" else "其他"
    if any(w in t for w in NOT_BIDDER):
        return "非投标人"
    # 条件式要先判：句子可能同时含「否则投标无效」和「如」
    for w in CONDITIONAL:
        if w in t:
            head = t.split(w)[0]
            if any(c in head for c in COND_WORDS):
                return "需确认"
            return "必废"
    if any(w in t for w in MUST_FAIL):
        return "必废"
    # 公司这边已经「不满足」：资格门槛直接必废，其他标需确认。
    # 必须排在「义务词」之前——不满足是事实，义务词只是句式。
    if verdict == "不满足":
        return "必废" if kind_of(row) == "资格门槛" else "需确认"
    # 只有义务词、没写后果
    if any(w in t for w in ("应", "须", "必须")):
        return "需确认"
    return "其他"


def kind_of(row) -> str:
    """资格门槛 / 无效情形。"""
    t = _text(row)
    if any(w in t for w in QUALIFY):
        return "资格门槛"
    return "无效情形"


#: 评分标准里出现这些词 → 主观分（由评委会裁量，不能预估）
SUBJECTIVE = ("合理", "完整", "完善", "科学", "针对性", "可行", "详细",
              "齐全", "清晰", "规范", "优化", "先进")
#: 出现这些 → 客观分（有明确档位或计数规则）
OBJECTIVE = ("每个", "满分", "有一个得", "以上", "每项", "每提供", "按顺序")

#: 各表的表级 role（出处在这张表里充当什么）
TABLE_ROLE = {
    "feibiao": "无效句",
    "cailiao": "格式",
    "shijian": "期限",
    "hetong": "条款",
    "kongwei": "格式",
}


def stamp_roles(rows, role: str) -> None:
    """给每行的 refs 打**表级** role（幂等，已有值不覆盖）。

    表级判据零成本、100% 覆盖；句级（金额 / 公式）要正则再判，覆盖率不高，先不做。
    """
    for row in rows or []:
        for ref in getattr(row, "refs", []) or []:
            if isinstance(ref, dict):          # 已经 model_dump 过的行
                if not ref.get("role"):
                    ref["role"] = role
            elif not getattr(ref, "role", ""):
                ref.role = role


def stamp_feibiao_roles(rows) -> None:
    """废标表按 kind 分两种 role：资格门槛 → 资格句，其余 → 无效句。"""
    for row in rows or []:
        role = "资格句" if getattr(row, "kind", "") == "资格门槛" else "无效句"
        stamp_roles([row], role)


def nature_of(row) -> str:
    """客观 / 主观。判据在评分标准的措辞上，可对照原文复核。"""
    t = getattr(row, "standard", "") or ""
    if not t.strip():
        return ""
    if any(w in t for w in SUBJECTIVE):
        return "主观"
    if any(w in t for w in OBJECTIVE):
        return "客观"
    return ""


def fill_pingfen(rows) -> None:
    """补 `nature`，并把**主观分**的 `estimate` 清空。

    为什么清空：实施方案这类主观分由评委会按档位裁量，模型给不出可信估值；
    给一个「预估 8 分」比不给更糟——用户会当真。
    """
    for row in rows or []:
        if not getattr(row, "nature", ""):
            row.nature = nature_of(row)
        if row.nature == "主观":
            row.estimate = ""
        # 价格分那行的标准里通常是公式，出处标「公式」
        if "公式" in (getattr(row, "standard", "") or "") or "×" in (getattr(row, "standard", "") or ""):
            stamp_roles([row], "公式")
        else:
            stamp_roles([row], "期限" if "期" in (getattr(row, "item", "") or "") else "")


def fill(rows) -> None:
    """就地补齐每一行的 tier / kind。idempotent：重复调用结果不变。"""
    for row in rows or []:
        if not getattr(row, "tier", ""):
            row.tier = tier_of(row)
        if not getattr(row, "kind", ""):
            row.kind = kind_of(row)
