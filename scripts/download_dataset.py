#!/usr/bin/env python3
"""Download a fixed Hub snapshot; never record authentication tokens."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.utils.task_parser import parse_task_md, resolve_tasks_dir


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--repo-id', required=True, help='Hugging Face dataset owner/OC-AgentBench')
    p.add_argument('--revision', required=True, help='Fixed dataset commit or release tag')
    p.add_argument('--local-dir', type=Path, default=Path(__file__).resolve().parents[2]/'HuggingFace/OC-AgentBench')
    p.add_argument('--language', choices=('en', 'cn', 'all'), default='all')
    p.add_argument('--tasks-dir', type=Path, help='Task directory relative to each language root')
    args = p.parse_args(argv)
    from huggingface_hub import HfApi, snapshot_download
    root = args.local_dir.expanduser().resolve()
    if root.exists() and any(root.iterdir()):
        raise ValueError('Choose an empty --local-dir to avoid mixing dataset revisions')
    languages = ('en', 'cn') if args.language == 'all' else (args.language,)
    prefixes = ['XperienceBench_' + language for language in languages]
    info = HfApi().dataset_info(args.repo_id, revision=args.revision)
    patterns = ['README.md', 'LICENSE*', 'tasks/**', 'workspace/**', 'skills/**']
    patterns += [prefix + '/**' for prefix in prefixes]
    snapshot_download(repo_id=args.repo_id, repo_type='dataset', revision=info.sha,
                      local_dir=str(root), allow_patterns=patterns)
    # Keep compatibility with previously released single-language layouts.
    nested = any((root / ('XperienceBench_' + language)).exists() for language in ('en', 'cn'))
    datasets = [root / prefix for prefix in prefixes] if nested else [root]
    records = []
    for dataset in datasets:
        tasks_dir = resolve_tasks_dir(dataset, args.tasks_dir)
        if not tasks_dir.is_relative_to(dataset):
            raise ValueError('--tasks-dir must be within its language dataset root')
        files = sorted(path for path in tasks_dir.glob('*/*.md') if path.is_file())
        if not files:
            raise ValueError('No matching tasks in --tasks-dir; see docs/DATASET.md')
        tasks = [parse_task_md(task, dataset) for task in files]
        if len({task['task_id'] for task in tasks}) != len(tasks):
            raise ValueError('Duplicate task IDs in language dataset')
        records.append((dataset, {'repo_id': args.repo_id, 'revision': info.sha,
                        'dataset_subdir': dataset.relative_to(root).as_posix(),
                        'tasks': len(files), 'tasks_dir': tasks_dir.relative_to(dataset).as_posix()}))
    for dataset, record in records:
        (dataset / '.xperiencebench-dataset.json').write_text(json.dumps(record, indent=2)+'\n')
        print(f'Validated {record["tasks"]} tasks at {dataset}; revision={info.sha}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
