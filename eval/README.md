# eval/ 评测目录

按用途分文件夹。日常优先看 **门禁（根目录）** 和本索引。

## 目录

| 目录 / 位置 | 干什么 | 常用入口 |
|---|---|---|
| **根目录（门禁）** | 六表回归判红 | `run_regression.py`、`compare.py`、`questions.json`、`markers.json`、`regression/` |
| `agent/` | ask_box 工具调用评测 | `ask_eval.py` |
| `metrics/` | 专项指标（表召回、术语、时间覆盖、幻觉案例、**覆盖率**） | `table_recall.py`、`terms_score.py`、`time_scan.py`、`coverage.py`、`mapreduce_probe.py` |
| `demos/` | 演示 / 人工核对 | `frontmatter_demo.py`、`matter_card_demo.py`、`render_review.py`、`题集核对清单.md` |
| `snapshots/` | 历史快照（少翻） | `baseline/`、`engine/`、`contracts/` |
| `legacy/` | 生成快照的旧脚本 | `run_engine.py`、`freeze_baseline.py`、`adapt_v1.py` |

门禁相关说明见 `docs/规则/回归门禁.md`。单元测试目录见 `tests/README.md`。

## 常用命令

```bash
# 硬门禁（改行为后）
env -u PYTHONPATH ~/miniforge3/envs/report-rag/bin/python eval/run_regression.py --engine v2

# 单次打分
python eval/compare.py data/step_output/beijing_road/04_check.txt --doc beijing_road -v

# Agent
HF_HUB_OFFLINE=1 env -u PYTHONPATH ~/miniforge3/envs/report-rag/bin/python eval/agent/ask_eval.py

# 专项
python eval/metrics/table_recall.py
python eval/metrics/time_scan.py
python eval/metrics/terms_score.py

# 覆盖率（输出覆盖率 = 表里 refs 覆盖的候选条款 / 全文候选条款）
HF_HUB_OFFLINE=1 env -u PYTHONPATH ~/miniforge3/envs/report-rag/bin/python eval/metrics/coverage.py --all
# 全扫实验（按条款打包 → 每包一次调用 → 去重 → 算覆盖）
HF_HUB_OFFLINE=1 env -u PYTHONPATH ~/miniforge3/envs/report-rag/bin/python eval/metrics/mapreduce_probe.py --doc beijing_road
HF_HUB_OFFLINE=1 env -u PYTHONPATH ~/miniforge3/envs/report-rag/bin/python eval/metrics/mapreduce_probe.py --doc beijing_road --dry-run   # 只打包不调模型

# 演示
python eval/demos/frontmatter_demo.py
python eval/demos/matter_card_demo.py
python eval/demos/render_review.py
```

## 路径迁移动对照（旧 → 新）

| 旧路径 | 新路径 |
|---|---|
| `eval/ask_eval.py` / `ask_cases.json` | `eval/agent/` |
| `eval/table_*.py` / `terms_*` / `time_scan.py` / `hallucination_cases.json` | `eval/metrics/` |
| `eval/frontmatter_demo.py` 等演示与核对清单 | `eval/demos/` |
| `eval/baseline` / `engine` / `contracts` | `eval/snapshots/` |
| `eval/run_engine.py` / `freeze_baseline.py` / `adapt_v1.py` | `eval/legacy/` |

未搬家：`run_regression.py`、`compare.py`、`questions.json`、`markers.json`、`regression/`（门禁命令保持最短）。
