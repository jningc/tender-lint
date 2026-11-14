# ontology/ · 招标文件核对本体

把「招标文件里可核对的东西」建模成类与属性，让抽取结果落在同一个结构上，评测时比对实例而不是自由文本。

**现状**：23 个类、12 个对象属性、12 个数据属性、24 条实例（取自 `sample/beijing_road.pdf`，页码逐条核过）。OWL 2 / Turtle，无推理机依赖。

v0.2 修的六处（都是和 `tender/contracts.py` 对不上的地方，由 `consistency.py` 扫出来）：

| 原来 | 现在 |
|---|---|
| `cites` domain 只写 `Clause`，但 `Budget`/`Bond`/`Evidence` 都在用 | domain 改成并集 `Clause ∪ Evidence ∪ Budget ∪ Bond` |
| `hasVerdict` domain 只写 `Requirement`，废标条件与保证金没处挂 | domain 改成并集 `Clause ∪ Bond ∪ Budget` |
| 判定写「三值」，契约是四值 | 加 `tl:VerdictKind`（`owl:oneOf` 四值）与 `tl:verdictKind` |
| `PageReference` 只有页号 | 加 `tl:bbox`（四数字符串），附一条条款级框实例 |
| `Bond` 说「靠属性区分」但没属性 | 加 `tl:BondKind`（投标/履约/质量）与 `tl:bondKind` |
| `Evidence` 只有页码 | 加 `tl:quote`（原文摘录，校验段回查用） |

```
tender.ttl      本体：类、属性、domain/range
instances.ttl   实例：一个项目、一个包、保证金、时间节点、两类判定
```

## 为什么要有它

`tender/tables.py` 里六张表是**输出格式**，本体是**语义**。两者不重复：

| 表里的字段 | 本体里是什么 |
|---|---|
| `FeibiaoRow.condition` | `tl:DisqualificationCondition` 实例 |
| `Ref(page=17)` | `tl:PageReference` 实例 + `tl:pageNumber` |
| `verdict="人工看"` | `tl:Verdict` 实例，`hasEvidence` 为空 |

好处是判定可校验：一条 `tl:Verdict` 若没有 `hasEvidence`，或证据里的 `PageReference` 没页码，按规则就该降级成「人工看」。这条约束写在类注释里，将来可以用 SHACL 自动查。

## 校验

两个脚本，分工不同：

```bash
# 1) 结构自检（要 rdflib，不在主依赖里，只跑这个时装）
pip install rdflib && python ontology/check.py

# 2) 与管道对齐（不需要 rdflib，只读 TTL 的固定写法）
python ontology/consistency.py
```

`check.py` 只做三件事：能否解析、类/属性/实例计数、有没有用了没定义的类或漏 domain/range 的属性。

`consistency.py` 查的是「用对没对」：实例有没有越界用属性（例：`Bond` 用 `cites`）、契约的六种条款类型能不能落到本体类、四个判定值是否与 `tl:VerdictKind` 逐字一致、`bbox` 有没有被建模并被实例使用。**有违规 exit 1**，`pytest tests/test_ontology.py` 也会跑它。

## 边界

- **不做推理**：没有引入 reasoning，`subClassOf` 只用于文档语义，不指望它推导出结论。
- **不替代规则**：页数、金额这类可计算的，仍然走代码（`calc_price_score` 一类），本体只存结果。
- **实例不是语料**：`instances.ttl` 是给结构做样板，不是训练数据。
