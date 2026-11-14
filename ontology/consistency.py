"""本体 ↔ 管道的对齐自检。不依赖 rdflib，只读这两份 TTL 的固定写法。

查四件事：

1. **domain/range 用对了没**。`check.py` 只查属性有没有写 domain/range，
   这个查的是实例有没有越界用（例如 `Bond` 用 `cites`）。
2. **类/属性有没有用未定义的**。
3. **契约能不能落到本体**：`contracts.ClauseType` 六个值要各有 tl 类，
   `contracts.Verdict` 四个值要和本体里 `tl:VerdictKind` 的 `owl:oneOf` 逐字对上。
4. **bbox 有没有被建模**（有属性 + 有实例在用），否则「整页框不算定位」这条规矩在本体里没落点。

用法:
  python ontology/consistency.py        # 有违规就 exit 1
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

HERE = Path(__file__).resolve().parent

#: 六张表 → 本体里的类（表是输出格式，类是语义）
TYPE_TO_CLASS = {
    "废标": "DisqualificationCondition",
    "评分": "ScoringItem",
    "材料": "RequiredMaterial",
    "空位": "BlankField",
    "时间": "Deadline",
    "合同": "ContractTerm",
}


def _blocks(text: str) -> list[str]:
    """按「行首是新主语」切块。够用即可，不追求完整 Turtle。"""
    lines = text.splitlines()
    out, cur = [], []
    for line in lines:
        if re.match(r"^(tl|inst):\w+\s", line):
            if cur:
                out.append("\n".join(cur))
            cur = [line]
        elif cur:
            cur.append(line)
    if cur:
        out.append("\n".join(cur))
    return out


def _bare(name: str) -> str:
    return name.rsplit(":", 1)[-1]


def load() -> dict:
    ontology = (HERE / "tender.ttl").read_text(encoding="utf-8")
    instances_text = (HERE / "instances.ttl").read_text(encoding="utf-8")

    classes = {_bare(c) for c in re.findall(r"^(tl:\w+)\s+a owl:Class", ontology, re.M)}
    oprops, dprops = set(), set()
    subclass, domain, ran, oneof = {}, {}, {}, {}

    for block in _blocks(ontology):
        m = re.match(r"^(tl:\w+)", block)
        if not m:
            continue
        name = _bare(m.group(1))
        if re.search(r"\ba owl:ObjectProperty\b", block):
            oprops.add(name)
        if re.search(r"\ba owl:DatatypeProperty\b", block):
            dprops.add(name)
        sc = re.search(r"rdfs:subClassOf\s+(tl:\w+)", block)
        if sc:
            subclass[name] = _bare(sc.group(1))
        dom = re.search(r"rdfs:domain\s+\[\s*owl:unionOf\s*\(([^)]*)\)\s*\]", block)
        if dom:
            domain[name] = {_bare(c) for c in re.findall(r"tl:\w+", dom.group(1))}
        else:
            single = re.search(r"rdfs:domain\s+(tl:\w+)", block)
            if single:
                domain[name] = {_bare(single.group(1))}
        rng = re.search(r"rdfs:range\s+(tl:\w+)", block)
        if rng:
            ran[name] = _bare(rng.group(1))
        oo = re.search(r"owl:oneOf\s*\(([^)]*)\)", block)
        if oo:
            oneof[name] = re.findall(r'"([^"]+)"', oo.group(1))

    instances = {}  # 主语 → (类, {对象属性: [对象]})
    dprops_used = set()
    for block in _blocks(instances_text):
        m = re.match(r"^(inst:\w+)\s+a\s+(tl:\w+)", block)
        if not m:
            continue
        subj, cls = m.group(1), _bare(m.group(2))
        props = {}
        for pm in re.finditer(r"^\s*tl:(\w+)\s+([^\n;]+)", block, re.M):
            prop, raw = pm.groups()
            if prop in dprops:
                dprops_used.add(prop)
                continue
            if prop not in oprops:
                continue
            objs = re.findall(r"inst:\w+", raw)
            if objs:
                props[prop] = objs
        instances[subj] = (cls, props)

    return {
        "classes": classes, "oprops": oprops, "dprops": dprops,
        "dprops_used": dprops_used,
        "subclass": subclass, "domain": domain, "range": ran,
        "oneof": oneof, "instances": instances,
    }


def _closure(model, cls: str) -> set[str]:
    """自己 + 所有祖先（本体只有单继承，够用）。"""
    seen = {cls}
    while cls in model["subclass"]:
        cls = model["subclass"][cls]
        seen.add(cls)
    return seen


def check() -> dict:
    model = load()
    errors, warns = [], []

    for subj, (cls, props) in model["instances"].items():
        if cls not in model["classes"]:
            errors.append(f"{subj} 用了未定义的类 tl:{cls}")
        for prop, objs in props.items():
            allowed = model["domain"].get(prop)
            if allowed and not (_closure(model, cls) & allowed):
                errors.append(
                    f"{subj}（{cls}）越界用 tl:{prop}：domain 是 {'/'.join(sorted(allowed))}")
            rng = model["range"].get(prop)
            if not rng:
                continue
            for obj in objs:
                obj_cls = model["instances"].get(obj, (None, {}))[0]
                if obj_cls and rng not in _closure(model, obj_cls):
                    errors.append(f"{subj} 的 {obj} 是 {obj_cls}，不满足 tl:{prop} 的 range {rng}")

    from tender.contracts import ClauseType, Verdict  # 延迟导入

    for ctype in getattr(ClauseType, "__args__", ()):
        target = TYPE_TO_CLASS.get(ctype)
        if not target:
            errors.append(f"契约类型「{ctype}」没有映射到本体类")
        elif target not in model["classes"]:
            errors.append(f"契约类型「{ctype}」映射的 tl:{target} 在本体里不存在")

    verdict_values = set(getattr(Verdict, "__args__", ()))
    declared = set(model["oneof"].get("VerdictKind", []))
    for value in sorted(verdict_values - declared):
        errors.append(f"契约判定值「{value}」本体没声明（tl:VerdictKind 缺）")
    for value in sorted(declared - verdict_values):
        errors.append(f"本体声明了判定值「{value}」，契约里没有")

    if "bbox" not in model["dprops"]:
        errors.append("tl:bbox 未定义：PageReference 表达不了条款级定位")
    elif "bbox" not in model["dprops_used"]:
        warns.append("tl:bbox 已定义但没有实例在用它，样例不含条款级框")

    return {"errors": errors, "warns": warns, "model": model}


def main() -> int:
    result = check()
    model = result["model"]
    print(f"类 {len(model['classes'])} · 对象属性 {len(model['oprops'])} · "
          f"数据属性 {len(model['dprops'])} · 实例 {len(model['instances'])}")
    for w in result["warns"]:
        print(f"  警告 {w}")
    for e in result["errors"]:
        print(f"  违规 {e}")
    print("\n结果:", "通过" if not result["errors"] else f"失败（{len(result['errors'])} 处）")
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
