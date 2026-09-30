# Dataset format

The dataset repository is
[OC-AgentBench](https://huggingface.co/datasets/wuyalunnn/OC-AgentBench).
The release layout uses `XperienceBench_cn/`, `XperienceBench_en/`, and
`XperienceBench-Images/`.
The two languages cover the same 50 scenarios: 50 × 3 conditions × 2 languages
= 300 task variants, not 100 independent scenarios.

The dataset card is a single `README.md` at the Hugging Face repository root.
Each language root contains:

```text
tasks/<category>/<task_id>.md
workspace/XperienceBench/<category>/<task_id>/exec/...
workspace/XperienceBench/<category>/<task_id>/gt/...
workspace/XperienceBench/<category>/<task_id>/tmp/...   # optional task resources
skills/<skill_name>/SKILL.md                            # only if referenced
```

Preserve task IDs, category directories, filenames and file contents. Do not
flatten directories or rename experience files: graders may depend on them.
The runner and downloader auto-detect this layout and the legacy
`tasks/XperienceBench/<category>/<task_id>.md` layout. When both contain task
definitions, choose one explicitly with `--tasks-dir`. Each language package is
a separate dataset root passed to `--data-root`.
Exclude newly generated evaluation results, private model logs, `.env`, provider
configurations, Git history and personal files from dataset uploads. The supplied
`history.jsonl` files and task input logs under `exec/` are benchmark materials;
keep them with their corresponding task workspaces.

Each language has 150 definitions/workspaces:

| Directory | Task variants |
| --- | ---: |
| Code_Software_Engineering_and_Security | 27 |
| Data_Finance_and_Analytical_Reasoning | 18 |
| Document_and_Office_Productivity | 18 |
| Information_Retrieval_and_Knowledge_Synthesis | 15 |
| Multimodal_Media_Processing | 30 |
| Planning_Coordination_and_Agent_Workflows | 24 |
| Web_API_and_Tool_Operations | 18 |

Each definition begins with YAML frontmatter (`id`, positive `timeout_seconds`)
and contains `## Prompt`, `## Workspace Path` and `## Automated Checks`.
The workspace path is **relative to the dataset root**, for example:

```text
workspace/XperienceBench/Code_Software_Engineering_and_Security/KT-38_unit_test_fixing_V2_structured_experience
```

The checks block defines `grade(**kwargs) -> dict`. The harness supplies
`transcript` and `workspace_path`; `/tmp_workspace/gt` is available only in the
separate offline grading container. Graders must not need network access or
symbolic links in agent outputs (both are removed). Keep the grader embedded in the dataset so the dataset commit
pins both the task and its scoring rubric. The code repository does not duplicate
the benchmark questions or answer files.

Optional sections: `Env` lists environment-variable **names**, one per line;
`Skills` lists dataset-relative skill directories under `skills/`; `Warmup`
contains trusted setup shell commands. Background services started by `Warmup`
are stopped with the agent before grading, so graders must read files rather
than query such services. Markdown `##` inside fenced code is
preserved as code, not treated as a new section. `exec/` symlinks must not escape
the mounted input directory.

The optional `scripts/download_dataset.py` accepts the dataset ID
`wuyalunnn/OC-AgentBench`, a revision, an empty local directory, and
`--language en|cn|all` (default `all`). Use `main` for the current snapshot or a
fixed commit to reproduce a version; the downloader records the resolved commit.
It preserves the language directory names and does not download image archives. Install `requirements-hf.txt` before
using it. Set `--data-root` to a language subdirectory, for example
`data/XperienceBench_cn` when downloaded with `--local-dir data`.

Task definitions pin the grading code. Preserve the repository commit and runtime
image ID with experiment results.

Dataset and third-party asset licensing is separate from the harness.
Consult the dataset distribution terms; the code's MIT license does not
automatically apply to dataset materials.
