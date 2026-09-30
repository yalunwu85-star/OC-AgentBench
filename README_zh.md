<div align="center">

# OC-AgentBench

### 面向工具型 Agent 的经验迁移评测

![场景数](https://img.shields.io/badge/Scenarios-50-blue)
![任务领域](https://img.shields.io/badge/Domains-7-blue)
![经验条件](https://img.shields.io/badge/Conditions-3-blue)

[English](README.md) · [快速开始](#快速开始) · [数据集](https://huggingface.co/datasets/wuyalunnn/OC-AgentBench) · [数据格式](docs/DATASET.md) · [运行环境](docs/RUNTIMES.md)

</div>

---

**OC-AgentBench（XperienceBench）** 用于评估工具型 agent 在新任务中选择和运用历史经验的能力。基准同时关注任务完成情况，以及 agent 的经验使用决策是否得到当前证据的支持。

基准包含 **7 个领域的 50 个场景**，每个场景设置 **3 种条件**，提供对应的**中文和英文**版本：每种语言 **150 个任务版本**，合计 **300 个任务版本**。

## 基准概览

已有经验能够减少重复探索，但也可能携带过时规则、不匹配的假设或不完整的步骤。OC-AgentBench 将历史经验与当前任务放在一起，观察 agent 能否保留有用的方法、作出必要调整，并在采用经验前完成核验。

任务涉及给定的文件、代码、多媒体材料和工具环境，要求交付报告、修复后的代码或处理后的文件等具体成果。每个场景在三种条件下保持相同的当前任务目标，便于比较不提供历史经验时的表现，以及两种经验形式带来的影响：

| 条件 | agent 可见材料 | 对比目的 |
| --- | --- | --- |
| `current_task` | 当前任务证据 | 不提供历史经验的基线 |
| `structured_experience` | 当前证据和结构化经验卡 | 观察简洁、有组织的经验能否迁移 |
| `trajectory_experience` | 当前证据和历史轨迹 | 观察过往交互记录中的经验能否迁移 |

在经验条件下，agent 还需记录哪些经验被采用、调整、拒绝或核验后采用，并给出相应的当前证据。历史建议作为候选方法，其适用性需要结合当前任务判断。

## 任务覆盖

| 领域 | 场景数 | 每种语言的任务版本数 |
| --- | ---: | ---: |
| 代码、软件工程与安全 | 9 | 27 |
| 数据、金融与分析推理 | 6 | 18 |
| 文档与办公生产力 | 6 | 18 |
| 信息检索与知识综合 | 5 | 15 |
| 多模态媒体处理 | 10 | 30 |
| 规划、协调与 agent 工作流 | 8 | 24 |
| Web、API 与工具操作 | 6 | 18 |
| **合计** | **50** | **150** |

中英文对应同一组 50 个场景。分类目录名称与任务定义格式见[数据格式说明](docs/DATASET.md)。

## 仓库结构

GitHub 存放评测代码。任务数据与运行镜像单独组织在 Hugging Face 数据仓库 [OC-AgentBench](https://huggingface.co/datasets/wuyalunnn/OC-AgentBench) 中，包含 `XperienceBench_cn/`、`XperienceBench_en/` 和 `XperienceBench-Images/`。

```text
.
├── run.py          # 评测入口
├── src/            # agent 适配器与公共工具
├── eval/           # 批量执行
├── scripts/        # 数据下载与仓库检查
├── docker/         # 运行镜像构建配方
├── configs/        # 模型配置示例
├── docs/           # 数据格式与环境配置
└── tests/          # 自动化检查
```

## 快速开始

需要 Python **3.10+** 和已启动的 Docker。以下命令均在仓库根目录执行。

### 1. 安装与配置

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-hf.txt
cp .env.example .env
```

在本地 `.env` 填写 `OPENROUTER_API_KEY`、`OPENROUTER_BASE_URL` 和 `DEFAULT_MODEL`。Codex 需要支持 Responses API 的模型服务。

### 2. 准备数据

从 [OC-AgentBench](https://huggingface.co/datasets/wuyalunnn/OC-AgentBench) 下载到空的 `data/` 目录，并校验任务。若仓库为私有，先运行 `hf auth login`，登录有访问权限的账号。

```bash
export DATASET_REPO=wuyalunnn/OC-AgentBench
export DATASET_REVISION=v1.0
python scripts/download_dataset.py --repo-id "$DATASET_REPO" \
  --revision "$DATASET_REVISION" --language all --local-dir data
python run.py --data-root data/XperienceBench_cn --category all --dry-run
```

英文任务使用 `data/XperienceBench_en`。已有本地数据可直接替换 `--data-root`，无需下载。`--dry-run` 只校验任务定义，不调用模型。下载器会记录实际 commit，便于复现。

### 3. 构建与运行

下面以 Codex 为例，已有对应运行镜像可跳过构建。预构建镜像和其他后端配置见[运行环境](docs/RUNTIMES.md)。

```bash
docker build --platform linux/amd64 -f docker/Dockerfile --target codex -t xperiencebench-codex:local .
python run.py --data-root data/XperienceBench_cn \
  --category Code_Software_Engineering_and_Security --condition current_task --limit 1 \
  --agent-backend codex
```

在一个条件下批量评测全部场景：

```bash
python run.py --data-root data/XperienceBench_cn --category all \
  --condition structured_experience --agent-backend codex --parallel 2
```

省略 `--condition` 可运行全部三个条件；用 `--task` 替代 `--category` 选择任务文件，`--model` 覆盖配置中的模型，`--help` 查看完整参数。

## 查看结果

结果保存在 `output/<后端>/<运行ID>/`。`summary.json` 按条件汇总指标；各任务目录下的 `score.json` 保存逐题评分，`task_output/workspace/` 保存收集的文件；`manifest.json` 记录模型、运行镜像 ID 和任务定义哈希。

## 开发检查

```bash
python -m unittest discover -s tests -v
python scripts/check_release.py
```

## 许可

评测代码采用 [MIT 许可](LICENSE)。
