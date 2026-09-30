"""Load dataset definitions; only Prompt is passed to the agent."""
from __future__ import annotations
import os
import re
from pathlib import Path
import yaml
ROOT_DIR = Path(__file__).resolve().parents[2]
CONDITIONS = ('current_task', 'structured_experience', 'trajectory_experience')

def resolve_tasks_dir(data_root: Path, tasks_dir: Path | None = None) -> Path:
    """Select a supported task layout, preserving an explicit directory choice."""
    root = Path(data_root).expanduser().resolve()
    if tasks_dir is not None:
        selected = Path(tasks_dir).expanduser()
        return (selected if selected.is_absolute() else root / selected).resolve()
    candidates = [root / 'tasks', root / 'tasks' / 'XperienceBench']
    populated = [directory for directory in candidates
                 if any(path.is_file() for path in directory.glob('*/*.md'))]
    if len(populated) > 1:
        raise ValueError('Ambiguous dataset task layout: both tasks/ and tasks/XperienceBench/ '
                         'contain tasks. Select one explicitly with --tasks-dir.')
    if not populated:
        raise ValueError('No matching tasks. Expected tasks/<category>/*.md or '
                         'tasks/XperienceBench/<category>/*.md; check --data-root or --tasks-dir.')
    return populated[0]

def parse_task_md(task_file: Path, data_root: Path | None = None) -> dict:
    task_file = Path(task_file).resolve()
    root = Path(data_root or os.environ.get('XPERIENCEBENCH_DATA_ROOT', ROOT_DIR / 'data')).resolve()
    content = task_file.read_text(encoding='utf-8')
    match = re.match(r'^---\s*\n(.*?)\n---\s*\n(.*)', content, re.DOTALL)
    if not match:
        raise ValueError(f'Missing YAML frontmatter: {task_file.name}')
    metadata = yaml.safe_load(match[1])
    if not isinstance(metadata, dict):
        raise ValueError(f'Frontmatter must be a mapping: {task_file.name}')
    sections = {}
    current, lines, in_fence = None, [], False
    for line in match[2].splitlines():
        if line.startswith('```'):
            in_fence = not in_fence
        header = re.match(r'^##\s+(.+)$', line) if not in_fence else None
        if header:
            if current is not None:
                sections[current] = '\n'.join(lines).strip()
            current, lines = header[1], []
        else:
            lines.append(line)
    if current is not None:
        sections[current] = '\n'.join(lines).strip()
    def section(name):
        value = sections.get(name, '').strip()
        if value.startswith('```') and value.endswith('```'):
            value = '\n'.join(value.splitlines()[1:-1]).strip()
        return value
    task_id = str(metadata.get('id', task_file.stem))
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', task_id):
        raise ValueError(f'Invalid task id: {task_file.name}')
    prompt, checks = section('Prompt'), section('Automated Checks')
    if not prompt or not checks:
        raise ValueError(f'Missing Prompt or Automated Checks: {task_file.name}')
    compile(checks, str(task_file), 'exec')
    raw = section('Workspace Path')
    wp = Path(raw)
    if not raw or wp.is_absolute() or '..' in wp.parts:
        raise ValueError(f'Workspace Path must be relative to dataset root: {task_file.name}')
    wp = (root / wp).resolve()
    if not wp.is_relative_to(root):
        raise ValueError(f'Workspace escapes dataset root: {task_file.name}')
    for required in ['exec', 'gt']:
        if not (wp / required).is_dir():
            raise ValueError(f'Missing {required}/ for {task_id}; check --data-root')
    for path in (wp / 'exec').rglob('*'):
        if path.is_symlink() and not path.resolve().is_relative_to(wp / 'exec'):
            raise ValueError(f'Input symlink escapes exec/: {task_id}/{path.name}')
    timeout = int(metadata.get('timeout_seconds', 900))
    if timeout <= 0:
        raise ValueError('timeout_seconds must be positive')
    for key in section('Env').splitlines():
        if key.strip() and not key.lstrip().startswith('#') and not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key.strip()):
            raise ValueError('Env must contain environment variable names only')
    for skill in section('Skills').splitlines():
        path = (root / 'skills' / skill.strip()).resolve()
        if not path.is_relative_to(root / 'skills') or not (path / 'SKILL.md').is_file():
            raise ValueError(f'Missing or invalid skill: {skill}')
    condition = next((c for c in CONDITIONS if task_id.endswith('_' + c)), 'unknown')
    return dict(task_id=task_id, prompt=prompt, workspace_path=str(wp), skills_path=str(root/'skills'),
                automated_checks=checks, env=section('Env'), skills=section('Skills'), warmup=section('Warmup'),
                timeout_seconds=timeout, file_path=str(task_file), category=task_file.parent.name, condition=condition)
