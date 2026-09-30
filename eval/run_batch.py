"""Dataset-driven batch evaluation for XperienceBench."""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path, PurePosixPath
import re
from src.utils import runtime_subprocess as subprocess
import tempfile
import threading
import time
import uuid
import yaml
from src.agents.base import AgentTaskSpec, BaseAgent
from src.utils.docker_utils import (CONTAINER_LABEL, DOCKER_TIMEOUT, close_proc_log,
    collect_output_from_container, remove_container)
from src.utils.grading import run_grading, write_error_score
from src.utils.security import SecretFilter, load_models_config, redact
from src.utils.transcript_loader import OPENCLAW_FALLBACK_PATH
from src.utils.task_parser import CONDITIONS, ROOT_DIR, parse_task_md, resolve_tasks_dir
BACKENDS = ('codex', 'openclaw', 'hermesagent', 'claudecode')
log = logging.getLogger(__name__)
ACTIVE_CONTAINERS = set()
STOP_REQUESTED = threading.Event()

def parser():
    p = argparse.ArgumentParser(description='XperienceBench evaluation runner')
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument('--task', type=Path, help='Task Markdown path, relative to dataset root or absolute')
    mode.add_argument('--category', help='Dataset category directory name, or all')
    p.add_argument('--data-root', type=Path, default=Path(os.getenv('XPERIENCEBENCH_DATA_ROOT', ROOT_DIR / 'data')))
    p.add_argument('--tasks-dir', type=Path, help='Relative to data root; auto-detect tasks/ or tasks/XperienceBench/ by default')
    p.add_argument('--output-dir', type=Path, default=ROOT_DIR / 'output')
    p.add_argument('--agent-backend', choices=BACKENDS, default='codex')
    p.add_argument('--model', default=os.getenv('DEFAULT_MODEL', ''), help='Required except for --dry-run')
    p.add_argument('--condition', choices=('all', *CONDITIONS), default='all')
    p.add_argument('--parallel', type=int, default=1)
    p.add_argument('--limit', type=int, help='First N selected tasks, sorted by path')
    p.add_argument('--thinking', help='Optional backend reasoning setting')
    p.add_argument('--models-config', type=Path, help='OpenClaw/Hermes JSON with ${ENV_VAR} references')
    p.add_argument('--openclaw-image-model', help='Override OpenClaw image tool model')
    p.add_argument('--dry-run', action='store_true', help='Validate/list tasks; no Docker, API or output writes')
    return p

def discover(args):
    root = args.data_root.expanduser().resolve()
    if args.task:
        files = [args.task if args.task.is_absolute() else root / args.task]
    else:
        tasks_dir = resolve_tasks_dir(root, args.tasks_dir)
        if args.category == 'all':
            files = sorted(tasks_dir.glob('*/*.md'))
        else:
            if not re.fullmatch(r'[A-Za-z0-9_-]+', args.category):
                raise ValueError('Invalid category directory name')
            files = sorted((tasks_dir / args.category).glob('*.md'))
    if args.condition != 'all':
        files = [f for f in files if f.stem.endswith('_' + args.condition)]
    if args.limit is not None:
        files = files[:args.limit]
    if not files:
        raise ValueError('No matching tasks. Check --data-root, --tasks-dir, --category and --condition.')
    tasks = [parse_task_md(f, root) for f in files]
    ids = [t['task_id'] for t in tasks]
    if len(set(ids)) != len(ids):
        raise ValueError('Duplicate task IDs in selection')
    return tasks

def make_backend(args):
    key, url = os.getenv('OPENROUTER_API_KEY', ''), os.getenv('OPENROUTER_BASE_URL', '')
    if args.agent_backend == 'codex':
        from src.agents.codex import CodexAgent
        return CodexAgent()
    if args.agent_backend == 'openclaw':
        from src.agents.openclaw import OpenClawAgent
        from src.utils.endpoint_utils import normalize_openrouter_base_url_for_openclaw
        return OpenClawAgent(gateway_port=18789, openrouter_api_key=key,
            openrouter_base_url=normalize_openrouter_base_url_for_openclaw(url), image_model=args.openclaw_image_model)
    if args.agent_backend == 'hermesagent':
        from src.agents.hermesagent import HermesAgentAgent
        from src.utils.endpoint_utils import normalize_openrouter_base_url_for_openclaw
        return HermesAgentAgent(openrouter_api_key=key, openrouter_base_url=normalize_openrouter_base_url_for_openclaw(url))
    from src.agents.claudecode import ClaudeCodeAgent
    return ClaudeCodeAgent(anthropic_api_key=os.getenv('ANTHROPIC_API_KEY') or key,
        anthropic_base_url=os.getenv('ANTHROPIC_BASE_URL', ''), openrouter_base_url=url)

def image_for(backend):
    from src.utils.docker_utils import DOCKER_IMAGE
    return getattr(backend, 'image', DOCKER_IMAGE)

def preflight(backend):
    image = image_for(backend)
    result = subprocess.run(['docker', 'image', 'inspect', '--format', '{{.Id}}', image],
                            capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise ValueError(f'Docker image unavailable: {image}. See docs/RUNTIMES.md.')
    return result.stdout.strip()

def stop_agent_processes(container):
    """Kill agent descendants before making evaluator files visible; fail closed."""
    code = """import os, signal, time
from pathlib import Path
keep = {1, os.getpid()}
p = os.getppid()
while p > 1 and p not in keep:
    keep.add(p)
    try:
        p = int(Path(f'/proc/{p}/stat').read_text().rsplit(')', 1)[1].split()[1])
    except (OSError, ValueError):
        break
def live():
    found = []
    for path in Path('/proc').iterdir():
        if not path.name.isdigit() or int(path.name) in keep:
            continue
        try:
            state = (path / 'stat').read_text().rsplit(')', 1)[1].split()[0]
            if state not in ('Z', 'X'):
                found.append(int(path.name))
        except OSError:
            pass
    return found
for _ in range(20):
    active = live()
    if not active:
        break
    for pid in active:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    time.sleep(0.05)
if live():
    raise SystemExit('Agent processes remain; refusing ground truth injection')
"""
    result = subprocess.run(['docker', 'exec', container, 'python3', '-c', code],
                            capture_output=True, text=True, timeout=15)
    if result.returncode:
        raise RuntimeError('Failed to stop agents before grading: ' + result.stderr)

def start_grading_container(agent_container, grader, image, gt_path, transcript):
    """Grade in a fresh offline container holding only agent outputs and the real ground truth."""
    def run(*cmd):
        r = subprocess.run(list(cmd), capture_output=True, text=True, timeout=DOCKER_TIMEOUT)
        if r.returncode:
            raise RuntimeError(f'Grading container setup failed ({cmd[1]}): {r.stderr.strip()}')
    run('docker', 'run', '-d', '--network', 'none', '--name', grader, *CONTAINER_LABEL,
        image, '/bin/bash', '-c', 'tail -f /dev/null')
    source = subprocess.Popen(['docker', 'cp', agent_container + ':/tmp_workspace/.', '-'],
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        sink = subprocess.run(['docker', 'cp', '-', grader + ':/tmp_workspace'], stdin=source.stdout,
                              capture_output=True, text=True, timeout=DOCKER_TIMEOUT)
    finally:
        source.stdout.close()
        source.wait(timeout=DOCKER_TIMEOUT)
    if source.returncode or sink.returncode:
        raise RuntimeError('Workspace copy to grading container failed: ' + sink.stderr.strip())
    # An agent-made gt/ or symlink could point graders at forged or out-of-workspace files.
    run('docker', 'exec', grader, '/bin/bash', '-c',
        'rm -rf /tmp_workspace/gt && find /tmp_workspace -type l -delete && mkdir /tmp_workspace/gt')
    run('docker', 'cp', str(gt_path) + '/.', grader + ':/tmp_workspace/gt')
    # Transcripts live outside the workspace; copy the loader's candidates when they are files.
    for path in dict.fromkeys(p for p in (transcript, OPENCLAW_FALLBACK_PATH) if p):
        with tempfile.TemporaryDirectory() as directory:
            local = Path(directory) / 'transcript'
            copied = subprocess.run(['docker', 'cp', '-L', f'{agent_container}:{path}', str(local)],
                                    capture_output=True, text=True, timeout=DOCKER_TIMEOUT)
            if copied.returncode == 0 and local.is_file():
                run('docker', 'exec', grader, 'mkdir', '-p', str(PurePosixPath(path).parent))
                run('docker', 'cp', str(local), f'{grader}:{path}')

def invalid_evaluation(result):
    """Grader rejected its own reference data; the dataset does not count this as a model failure."""
    scores = result.get('scores')
    return not result.get('error') and isinstance(scores, dict) and scores.get('status') == 'invalid_evaluation'

def failed(result):
    if result.get('error'):
        return True
    scores = result.get('scores', {})
    if not isinstance(scores, dict):
        return True
    if invalid_evaluation(result):
        return False
    if scores.get('score_eligible') is False or scores.get('status') not in (None, 'valid'):
        return True
    # Dataset graders may use error to explain a valid zero, such as missing outputs.
    if scores.get('status') == 'valid' and scores.get('score_eligible') is True:
        return False
    return bool(scores.get('error'))

def run_single_task(task, args, run_root, models_config=None):
    container = 'xpb-' + uuid.uuid4().hex
    grader = container + '-grade'
    ACTIVE_CONTAINERS.update((container, grader))
    out = run_root.resolve() / task['category'] / task['task_id']
    result = dict(task_id=task['task_id'], category=task['category'], condition=task['condition'],
                  scores={}, error=None, timed_out=False)
    execution = None
    try:
        out.mkdir(parents=True, exist_ok=False)
        if STOP_REQUESTED.is_set():
            raise RuntimeError('Run interrupted before task startup')
        backend = make_backend(args)
        prompt = (f"Solve this task in the container within {task['timeout_seconds']} seconds. "
                  'Run foreground commands without interactive input. Save deliverables under /tmp_workspace/results/.\n\n'
                  + task['prompt'])
        execution = backend.run_task(AgentTaskSpec(task_id=container, task=task,
            workspace_path=task['workspace_path'], prompt=prompt, timeout_seconds=task['timeout_seconds'],
            output_dir=out, model=args.model, thinking=args.thinking, models_config=models_config))
        result['error'] = execution.error
        result['timed_out'] = execution.timed_out
        if STOP_REQUESTED.is_set():
            raise RuntimeError('Run interrupted during task execution')
        stop_agent_processes(container)
        transcript = backend.prepare_grading_transcript(container)
        result['usage'] = backend.collect_usage(container, out, execution.elapsed_time)
        (out/'usage.json').write_text(json.dumps(result['usage'], indent=2), encoding='utf-8')
        if isinstance(backend, BaseAgent):
            provider_error = backend.get_execution_error(container, out, execution)
            if provider_error:
                result['error'] = result['error'] or provider_error
        collect_output_from_container(container, out, include_workspace_changes=True)
        start_grading_container(container, grader, image_for(backend),
                                Path(task['workspace_path'])/'gt', transcript)
        result['scores'] = run_grading(grader, task['automated_checks'], out,
            extra_env=task['env'], transcript_container_path=transcript, write_error_score=True)
    except Exception as exc:
        # Keep the root cause when a later step fails after an execution error.
        if STOP_REQUESTED.is_set():
            result['cancelled'] = True
        message = redact(str(exc))
        result['error'] = f"{result['error']}; then {message}" if result['error'] else message
        result['scores'] = write_error_score(out, task['task_id'], result['error'])
    finally:
        try:
            process_errors = []
            if execution:
                for label, proc in [('agent', execution.agent_proc), ('gateway', execution.gateway_proc)]:
                    if proc is None:
                        continue
                    try:
                        if proc.poll() is None:
                            proc.terminate()
                            try:
                                proc.wait(timeout=5)
                            except subprocess.TimeoutExpired:
                                proc.kill()
                                proc.wait(timeout=5)
                    except Exception as exc:
                        process_errors.append(f'{label}: {redact(str(exc))}')
                    finally:
                        try:
                            close_proc_log(proc)
                        except Exception as exc:
                            process_errors.append(f'{label} log: {redact(str(exc))}')
            if process_errors:
                result['process_cleanup_error'] = '; '.join(process_errors)
        finally:
            cleanup_errors = {}
            for name in (container, grader):
                try:
                    error = remove_container(name)
                except Exception as exc:
                    error = str(exc)
                if error:
                    cleanup_errors[name] = redact(error)
                else:
                    ACTIVE_CONTAINERS.discard(name)
            if cleanup_errors:
                result['cleanup_errors'] = cleanup_errors
    (out/'result.json').write_text(redact(json.dumps(result, ensure_ascii=False, indent=2)), encoding='utf-8')
    log.info('%s: %s', task['task_id'], 'ERROR' if failed(result) else
             'INVALID EVALUATION' if invalid_evaluation(result) else 'finished')
    return result

def summarize(results):
    groups = {}
    for condition in sorted({r['condition'] for r in results}):
        rows = [r for r in results if r['condition'] == condition]
        valid = [r for r in rows if not failed(r) and not invalid_evaluation(r)]
        keys = sorted({k for r in valid for k,v in r['scores'].items() if isinstance(v,(int,float)) and not isinstance(v,bool)})
        metrics = {}
        for key in keys:
            values = [r['scores'][key] for r in valid if isinstance(r['scores'].get(key),(int,float)) and not isinstance(r['scores'].get(key),bool)]
            metrics[key] = {'mean': sum(values)/len(values), 'n': len(values)}
        groups[condition] = {'total':len(rows), 'failed':sum(map(failed,rows)),
                             'invalid_evaluation':sum(map(invalid_evaluation,rows)),
                             'timed_out':sum(bool(r.get('timed_out')) for r in rows), 'metrics':metrics}
    return {'total':len(results), 'failed':sum(map(failed,results)),
        'invalid_evaluation':sum(map(invalid_evaluation,results)),
        'cleanup_failed':sum(bool(r.get('cleanup_errors') or r.get('process_cleanup_error')) for r in results),
        'timed_out':sum(bool(r.get('timed_out')) for r in results), 'by_condition':groups,
        'aggregation':'Means exclude execution/grading errors and invalid evaluations, include scored task timeouts; n is reported per metric. Score scales unchanged.', 'results':results}

def main(argv=None):
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    for handler in logging.getLogger().handlers:
        handler.addFilter(SecretFilter())
    STOP_REQUESTED.clear()
    subprocess.reset()
    args = parser().parse_args(argv)
    try:
        if args.agent_backend == 'claudecode':
            from src.agents.claudecode import ClaudeCodeAgent
            ClaudeCodeAgent.validate_thinking(args.thinking)
        if args.parallel < 1 or (args.limit is not None and args.limit < 1):
            raise ValueError('--parallel and --limit must be positive')
        if os.environ.get('TMP_WORKSPACE','/tmp_workspace') != '/tmp_workspace':
            raise ValueError('This dataset requires TMP_WORKSPACE=/tmp_workspace')
        tasks = discover(args)
        if args.dry_run:
            print(json.dumps({'count':len(tasks), 'tasks':[{k:t[k] for k in ('task_id','category','condition','timeout_seconds')} for t in tasks]},ensure_ascii=False,indent=2))
            return 0
        if not args.model or any(c in args.model for c in ('\n','\r','"',"'",'`','$','\\')):
            raise ValueError('Provide a valid --model (provider-specific model identifier)')
        if args.thinking and not re.fullmatch(r'[A-Za-z0-9_-]+', args.thinking):
            raise ValueError('Invalid --thinking setting')
        config = load_models_config(args.models_config) if args.models_config else None
        if config and args.agent_backend in ('codex','claudecode'):
            raise ValueError('--models-config is for OpenClaw/Hermes only; use environment credentials for this backend')
        credential = os.getenv('ANTHROPIC_API_KEY') if args.agent_backend == 'claudecode' else None
        if not (credential or os.getenv('OPENROUTER_API_KEY') or config):
            raise ValueError('Set OPENROUTER_API_KEY (or ANTHROPIC_API_KEY for ClaudeCode) in your local .env')
        image_id = preflight(make_backend(args))
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:8]
        run_root = args.output_dir.expanduser().resolve()/args.agent_backend/stamp
        run_root.mkdir(parents=True,exist_ok=False)
        manifest = dict(created_at=stamp, backend=args.agent_backend, model=args.model, thinking=args.thinking,
            image_id=image_id, tasks=[{'id':t['task_id'],'definition_sha256':hashlib.sha256(Path(t['file_path']).read_bytes()).hexdigest()} for t in tasks])
        dataset_record = args.data_root/'.xperiencebench-dataset.json'
        if dataset_record.is_file():
            record = json.loads(dataset_record.read_text())
            manifest['dataset'] = {k:record[k] for k in ('repo_id','revision') if k in record}
        (run_root/'manifest.json').write_text(redact(json.dumps(manifest,indent=2)),encoding='utf-8')
        pool = ThreadPoolExecutor(max_workers=args.parallel)
        futures = [pool.submit(run_single_task,task,args,run_root,config) for task in tasks]
        interrupted = False
        try:
            wait(futures)
        except KeyboardInterrupt:
            # Cancel queued tasks; removing containers makes running tasks fail fast and clean up.
            interrupted = True
            STOP_REQUESTED.set()
            subprocess.cancel_all()
            log.warning('Interrupted: cancelling queued tasks and stopping running containers')
            pool.shutdown(wait=False, cancel_futures=True)
            deadline = time.monotonic() + 30
            while not all(f.done() for f in futures) and time.monotonic() < deadline:
                for name in list(ACTIVE_CONTAINERS):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    try:
                        error = remove_container(name, timeout=min(5, remaining))
                    except Exception as exc:
                        error = str(exc)
                    if error:
                        log.warning('Container cleanup incomplete: %s: %s', name, redact(error))
                wait([f for f in futures if not f.done()], timeout=min(0.2, max(0, deadline-time.monotonic())))
        pool.shutdown(wait=not interrupted, cancel_futures=interrupted)
        results = []
        pending_tasks = []
        for task, future in zip(tasks, futures):
            if future.cancelled() or not future.done():
                pending_tasks.append(task['task_id'])
                continue
            try:
                results.append(future.result())
            except Exception as exc:
                results.append(dict(task_id=task['task_id'], category=task['category'],
                    condition=task['condition'], scores={}, error=redact(str(exc)), timed_out=False))
        summary = summarize(results)
        if interrupted:
            summary['interrupted'] = True
        summary['unfinished_tasks'] = pending_tasks
        summary['cleanup_pending_containers'] = sorted(ACTIVE_CONTAINERS)
        (run_root/'summary.json').write_text(redact(json.dumps(summary,ensure_ascii=False,indent=2)),encoding='utf-8')
        print(f"Completed {len(results)}/{len(tasks)} tasks; failures={summary['failed']}; "
              f"invalid_evaluation={summary['invalid_evaluation']}; timed_out={summary['timed_out']}; output={run_root}")
        if interrupted:
            return 130
        return 1 if summary['failed'] or summary['cleanup_failed'] or summary['cleanup_pending_containers'] else 0
    except (OSError,ValueError,SyntaxError,yaml.YAMLError,subprocess.SubprocessError) as exc:
        log.error('%s',exc)
        return 2
if __name__ == '__main__':
    raise SystemExit(main())
