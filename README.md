# OC-AgentBench（XperienceBench）

**OC-AgentBench（XperienceBench）** evaluates how tool-using agents select and apply historical experience when solving new tasks. It examines both task completion and whether experience decisions are supported by current evidence.

The benchmark covers **50 scenarios across 7 domains**, with **3 conditions** and corresponding **English and Chinese** versions: **150 task variants per language**, or **300 in total**.

[中文](README_zh.md) · [Dataset format](docs/DATASET.md) · [Runtime setup](docs/RUNTIMES.md)

## Overview

Past experience can reduce repeated exploration, but it can also carry outdated rules, mismatched assumptions or incomplete procedures. OC-AgentBench places historical experience alongside a current task to examine whether agents can retain useful methods, make necessary adjustments and verify advice before applying it.

Tasks involve supplied files, code, media and tool environments, with concrete deliverables such as reports, repaired code or processed artifacts. Each scenario keeps the same current-task goal across three conditions, allowing comparison of performance without supplied history and with two different forms of experience:

| Condition | Agent-visible material | Comparison |
| --- | --- | --- |
| `current_task` | Current task evidence | Baseline without supplied historical experience |
| `structured_experience` | Current evidence and structured experience cards | Transfer from concise, organized experience |
| `trajectory_experience` | Current evidence and historical trajectories | Transfer from earlier interaction records |

In the experience conditions, agents also record which experiences they adopt, adapt, reject or adopt after verification, together with supporting current evidence. Historical advice is treated as a candidate method whose applicability must be assessed against the current task.

## Task coverage

| Domain | Scenarios | Task variants per language |
| --- | ---: | ---: |
| Code, software engineering and security | 9 | 27 |
| Data, finance and analytical reasoning | 6 | 18 |
| Document and office productivity | 6 | 18 |
| Information retrieval and knowledge synthesis | 5 | 15 |
| Multimodal media processing | 10 | 30 |
| Planning, coordination and agent workflows | 8 | 24 |
| Web, API and tool operations | 6 | 18 |
| **Total** | **50** | **150** |

English and Chinese cover the same 50 scenarios. Category directory names and task definitions are described in [Dataset format](docs/DATASET.md).

## Repository structure

GitHub contains the evaluation code. Task data and runtime image archives are packaged separately in the Hugging Face dataset repository [OC-AgentBench](https://huggingface.co/datasets/wuyalunnn/OC-AgentBench), under `XperienceBench_cn/`, `XperienceBench_en/` and `XperienceBench-Images/`.

```text
.
├── run.py          # evaluation entry point
├── src/            # agent adapters and utilities
├── eval/           # batch execution
├── scripts/        # data download and repository checks
├── docker/         # runtime build recipes
├── configs/        # model configuration examples
├── docs/           # data and runtime setup
└── tests/          # automated checks
```

## Quick start

Requires Python **3.10+** and a running Docker daemon. Run commands from the repository root.

### 1. Install and configure

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-hf.txt
cp .env.example .env
```

Set `OPENROUTER_API_KEY`, `OPENROUTER_BASE_URL` and `DEFAULT_MODEL` in your local `.env`. Codex requires a Responses-compatible model API.

### 2. Prepare the data

Download from [OC-AgentBench](https://huggingface.co/datasets/wuyalunnn/OC-AgentBench) into an empty `data/` directory and validate the tasks. If the repository is private, first run `hf auth login` with an account that has access.

```bash
export DATASET_REPO=wuyalunnn/OC-AgentBench
export DATASET_REVISION=v1.0
python scripts/download_dataset.py --repo-id "$DATASET_REPO" \
  --revision "$DATASET_REVISION" --language all --local-dir data
python run.py --data-root data/XperienceBench_cn --category all --dry-run
```

For English tasks, use `data/XperienceBench_en`. Existing local data can be used directly by changing `--data-root`. `--dry-run` validates task definitions without calling the model. The downloader records the resolved commit for reproducibility.

### 3. Build and run

The following uses Codex. Skip the build if you already have its runtime image. Prebuilt images and other backend settings are covered in [Runtime setup](docs/RUNTIMES.md).

```bash
docker build --platform linux/amd64 -f docker/Dockerfile --target codex -t xperiencebench-codex:local .
python run.py --data-root data/XperienceBench_cn \
  --category Code_Software_Engineering_and_Security --condition current_task --limit 1 \
  --agent-backend codex
```

Evaluate all scenarios under one condition:

```bash
python run.py --data-root data/XperienceBench_cn --category all \
  --condition structured_experience --agent-backend codex --parallel 2
```

Omit `--condition` to run all three conditions. Use `--task` instead of `--category` to select a task file, `--model` to override the configured model, and `--help` for all options.

## Results

Results are saved under `output/<backend>/<run-id>/`. `summary.json` aggregates metrics by condition; each task directory contains `score.json` and collected files in `task_output/workspace/`. `manifest.json` records the model, runtime image ID and task definition hashes.

## Development checks

```bash
python -m unittest discover -s tests -v
python scripts/check_release.py
```

## License

The evaluation code is licensed under [MIT](LICENSE).
