# tests/ 单元与守卫

不调 LLM、可进 CI。端到端效果评测在 `eval/`（见 `eval/README.md`）。

## 目录

| 目录 | 测什么 |
|---|---|
| `parse/` | 解析、切章、条款树、表格结构/检索块 |
| `rules/` | 废标分档、术语、前附表、事项卡、离线判定、报告渲染 |
| `pipeline/` | 契约、校验段、本体、并发抽取、引擎开关、RRF |
| `gate/` | 回归门禁逻辑、题集清单与 `questions.json` 一致性 |
| `hygiene/` | 仓库卫生（防提交手机号/邮箱） |

## 常用命令

```bash
# 全量
env -u PYTHONPATH ~/miniforge3/envs/report-rag/bin/python -m pytest tests/ -q

# 按层
python -m pytest tests/parse/ -q
python -m pytest tests/rules/ -q
python -m pytest tests/pipeline/ -q
python -m pytest tests/gate/ -q
python -m pytest tests/hygiene/ -q

# 单文件
python -m pytest tests/pipeline/test_contracts.py -q
python -m pytest tests/gate/test_regression_gate.py -q
```
