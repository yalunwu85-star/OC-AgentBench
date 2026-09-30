"""Small public checks for the CLI, scoring boundary and optional Docker run."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import get_type_hints
from unittest.mock import patch

from eval.run_batch import discover, main, parser, run_single_task, summarize
from scripts.check_release import scan
from src.agents.base import AgentExecution
from src.agents.openclaw.runner import OpenClawAgent
from src.utils import runtime_subprocess
from src.utils.execution_validity import codex_provider_error, openclaw_provider_error
from src.utils.security import load_models_config, redact
from src.utils.task_parser import parse_task_md


def fixture(root, task_id='KT-00_smoke_V2_current_task'):
    workspace = root / 'workspace/XperienceBench/Smoke' / task_id
    (workspace / 'exec').mkdir(parents=True)
    (workspace / 'gt').mkdir()
    (workspace / 'exec/input.txt').write_text('hello')
    (workspace / 'gt/expected.txt').write_text('hello')
    task = root / 'tasks/Smoke' / (task_id + '.md')
    task.parent.mkdir(parents=True, exist_ok=True)
    task.write_text(f'''---
id: {task_id}
timeout_seconds: 30
---
## Prompt
Copy input.txt to results/answer.txt.
## Workspace Path
```text
workspace/XperienceBench/Smoke/{task_id}
```
## Automated Checks
```python
def grade(**kwargs):
    from pathlib import Path
    root = Path(kwargs['workspace_path'])
    actual = (root/'results/answer.txt').read_text()
    expected = (root/'gt/expected.txt').read_text()
    return {{'status': 'valid', 'score_eligible': True,
            'overall_score': float(actual == expected)}}
```
''')
    return task


class SmokeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.task = fixture(self.root)
        runtime_subprocess.reset()
        self.addCleanup(runtime_subprocess.reset)

    def test_task_loading_and_condition_selection(self):
        task = parse_task_md(self.task, self.root)
        self.assertNotIn('def grade', task['prompt'])
        self.assertTrue(Path(task['workspace_path']).is_relative_to(self.root))
        fixture(self.root, 'KT-00_smoke_V2_structured_experience')
        args = parser().parse_args(['--category', 'all', '--data-root', str(self.root),
                                    '--condition', 'structured_experience'])
        self.assertEqual([t['condition'] for t in discover(args)], ['structured_experience'])

    def test_workspace_escape_is_rejected(self):
        self.task.write_text(self.task.read_text().replace('workspace/XperienceBench/', '../'))
        with self.assertRaises(ValueError):
            parse_task_md(self.task, self.root)

    def test_dry_run_avoids_docker(self):
        with patch('eval.run_batch.subprocess.run', side_effect=AssertionError('Docker called')):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                code = main(['--category', 'all', '--data-root', str(self.root), '--dry-run'])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())['count'], 1)

    def test_credential_expansion_and_redaction(self):
        config = self.root / 'models.json'
        config.write_text(json.dumps({'providers': {'custom': {'apiKey': '${SMOKE_API_KEY}'}}}))
        with patch.dict(os.environ, {'SMOKE_API_KEY': 'offline' + '-credential'}):
            value = load_models_config(config)['providers']['custom']['apiKey']
            self.assertEqual(redact(value), '[REDACTED]')
        config.write_text(json.dumps({'providers': {'custom': {'apiKey': 'inline'}}}))
        with self.assertRaises(ValueError):
            load_models_config(config)

    def test_key_is_not_in_process_arguments(self):
        key = 'offline' + '-credential'
        with patch('src.agents.openclaw.runner.subprocess.run') as run:
            run.return_value.returncode = 0
            OpenClawAgent(gateway_port=18789, openrouter_api_key=key)._inject_openrouter_key('offline')
        self.assertFalse(any(key in arg for arg in run.call_args.args[0]))
        self.assertEqual(run.call_args.kwargs['input'], key)

    def test_native_provider_errors_allow_recovery(self):
        log = self.root / 'agent.jsonl'
        cases = [(codex_provider_error, {'type': 'error', 'message': '401'},
                  {'type': 'turn.completed'}),
                 (openclaw_provider_error, {'type': 'message', 'message': {
                     'role': 'assistant', 'stopReason': 'error'}},
                  {'type': 'message', 'message': {'role': 'assistant', 'stopReason': 'stop'}})]
        for check, failed, recovered in cases:
            log.write_text(json.dumps(failed) + '\n')
            self.assertIsNotNone(check(log))
            log.write_text(log.read_text() + json.dumps(recovered) + '\n')
            self.assertIsNone(check(log))

    def test_summary_counts_valid_zero_and_excludes_failures(self):
        rows = [dict(condition='current_task', error=None, scores={'overall_score': 0}),
                dict(condition='current_task', error='API failed', scores={'overall_score': 1}),
                dict(condition='current_task', error=None, scores={
                    'status': 'invalid_evaluation', 'score_eligible': False})]
        summary = summarize(rows)
        self.assertEqual((summary['failed'], summary['invalid_evaluation']), (1, 1))
        self.assertEqual(summary['by_condition']['current_task']['metrics']['overall_score'],
                         {'mean': 0.0, 'n': 1})

    def test_subprocess_timeout_and_typing(self):
        self.assertIn('agent_proc', get_type_hints(AgentExecution))
        with self.assertRaises(subprocess.TimeoutExpired):
            runtime_subprocess.run([sys.executable, '-c', 'import time; time.sleep(10)'],
                                   capture_output=True, timeout=0.1)

    def test_private_files_are_flagged(self):
        (self.root / '.env').write_text('DUMMY=local')
        self.assertTrue(any('private configuration' in reason for _, reason in scan(self.root)))


@unittest.skipUnless(os.getenv('XPB_SMOKE_IMAGE'), 'Set XPB_SMOKE_IMAGE for Docker')
class DockerSmoke(unittest.TestCase):
    def test_collection_and_fresh_grading(self):
        class OfflineAgent:
            image = os.environ['XPB_SMOKE_IMAGE']
            answer = 'hello'

            def run_task(self, spec):
                def run(*cmd):
                    subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=30)
                run('docker', 'run', '-d', '--network', 'none', '--label', 'xperiencebench',
                    '--name', spec.task_id, '-v', spec.workspace_path + '/exec:/app:ro',
                    self.image, '/bin/bash', '-c', 'tail -f /dev/null')
                run('docker', 'exec', spec.task_id, '/bin/bash', '-c',
                    'mkdir -p /tmp_workspace/results /tmp_workspace/gt && '
                    'cp -r /app/. /tmp_workspace && printf forged > /tmp_workspace/gt/expected.txt && '
                    f'printf {self.answer} > /tmp_workspace/results/answer.txt && '
                    'touch /tmp/chat.jsonl')
                return AgentExecution(0.1)

            def prepare_grading_transcript(self, container): return '/tmp/chat.jsonl'
            def collect_usage(self, *args): return {'request_count': 0, 'total_tokens': 0}

        for answer, expected in [('hello', 1.0), ('forged', 0.0)]:
            with self.subTest(answer=answer), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                task = parse_task_md(fixture(root), root)
                agent = OfflineAgent(); agent.answer = answer
                args = parser().parse_args(['--category', 'all', '--model', 'offline'])
                with patch('eval.run_batch.make_backend', return_value=agent):
                    result = run_single_task(task, args, root / 'out')
                self.assertIsNone(result['error'])
                self.assertFalse(result.get('cleanup_errors'))
                self.assertEqual(result['scores']['overall_score'], expected)
                self.assertTrue(next((root / 'out').rglob('answer.txt')).is_file())
                exported = list((root / 'out').rglob('expected.txt'))
                self.assertTrue(exported)
                self.assertTrue(all(path.read_text() == 'forged' for path in exported))


if __name__ == '__main__':
    unittest.main()
