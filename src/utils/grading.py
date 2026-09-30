from __future__ import annotations

import json
import logging
import os
from src.utils import runtime_subprocess as subprocess
import tempfile
from pathlib import Path

from src.utils.docker_utils import DOCKER_TIMEOUT, docker_env

logger = logging.getLogger(__name__)

TMP_WORKSPACE = os.environ.get("TMP_WORKSPACE", "/tmp_workspace")

def _write_score(output_dir: Path, task_id: str, scores: dict) -> None:
    score_path = output_dir / "score.json"
    score_path.parent.mkdir(parents=True, exist_ok=True)
    score_path.write_text(
        json.dumps(scores, indent=2, ensure_ascii=False, allow_nan=False), encoding="utf-8"
    )
    logger.info("[%s] Grading results written to → %s", task_id, score_path)


def write_error_score(output_dir: Path, task_id: str, message: str) -> dict:
    scores = {"status": "error", "score_eligible": False, "overall_score": None, "error": message}
    _write_score(output_dir, task_id, scores)
    return scores


def _grading_error(
    output_dir: Path,
    task_id: str,
    message: str,
    persist_error: bool,
) -> dict:
    if persist_error:
        return write_error_score(output_dir, task_id, message)
    return {"error": message}


def run_grading(
    task_id: str,
    automated_checks: str,
    output_dir: Path,
    extra_env: str = "",
    transcript_container_path: str = "",
    write_error_score: bool = False,
) -> dict:
    logger.info("[%s] Starting in-container grading...", task_id)

    loader_src = Path(__file__).with_name("transcript_loader.py")
    if not loader_src.exists():
        logger.error("[%s] transcript loader module not found: %s", task_id, loader_src)
        return _grading_error(
            output_dir,
            task_id,
            f"transcript loader module not found: {loader_src}",
            write_error_score,
        )

    runner_code = "\n".join([
        "import json",
        "from _transcript_loader import load_transcript",
        f"_transcript = load_transcript({json.dumps(transcript_container_path)})",
        "",
        automated_checks,
        "",
        f'result = grade(transcript=_transcript, workspace_path="{TMP_WORKSPACE}")',
        "print(json.dumps(result))",
    ]) + "\n"

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", delete=False, encoding="utf-8"
    ) as f:
        f.write(runner_code)
        runner_host = f.name

    try:
        for source, destination, label in (
            (str(loader_src), "/tmp/_transcript_loader.py", "docker cp transcript loader"),
            (runner_host, "/tmp/_grade_runner.py", "docker cp"),
        ):
            copied = subprocess.run(
                ["docker", "cp", source, f"{task_id}:{destination}"],
                capture_output=True, text=True, timeout=DOCKER_TIMEOUT,
            )
            if copied.returncode:
                message = f"{label} failed: {copied.stderr}"
                logger.error("[%s] %s", task_id, message)
                return _grading_error(output_dir, task_id, message, write_error_score)

        env_map: dict[str, str] = {}
        for line in extra_env.splitlines():
            key = line.strip()
            if not key or key.startswith("#"):
                continue
            env_map[key] = os.environ.get(key, "")
            masked = "(set)" if env_map[key] else "(empty)"
            logger.info("[%s] Injecting grading env: %s=%s", task_id, key, masked)
        env_args, cli_env = docker_env(env_map)

        r = subprocess.run(
            ["docker", "exec", *env_args, task_id, "python3", "/tmp/_grade_runner.py"],
            capture_output=True,
            text=True,
            env=cli_env,
            timeout=120,
        )
        if r.returncode != 0:
            logger.error("[%s] Grading script execution failed: %s", task_id, r.stderr)
            return _grading_error(
                output_dir,
                task_id,
                f"grade script failed: {r.stderr}",
                write_error_score,
            )

        try:
            scores = json.loads(r.stdout.strip())
        except json.JSONDecodeError:
            scores = None
            for line in reversed(r.stdout.strip().splitlines()):
                line = line.strip()
                if line.startswith("{"):
                    try:
                        scores = json.loads(line)
                        break
                    except json.JSONDecodeError:
                        continue
            if scores is None:
                logger.error("[%s] Failed to parse grading result, no valid JSON found in stdout\nstdout: %s", task_id, r.stdout[:500])
                return _grading_error(
                    output_dir,
                    task_id,
                    "json parse failed: no valid JSON in stdout",
                    write_error_score,
                )

    finally:
        Path(runner_host).unlink(missing_ok=True)

    if not isinstance(scores, dict):
        return _grading_error(output_dir, task_id, "Grader must return a JSON object", write_error_score)
    try:
        _write_score(output_dir, task_id, scores)
    except ValueError:
        return _grading_error(output_dir, task_id, "Grader returned a non-finite number", write_error_score)
    return scores



def extract_usage_from_jsonl(jsonl_path: Path) -> dict:
    totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "total_tokens": 0,
        "cost_usd": 0.0,
        "request_count": 0,
    }
    if not jsonl_path.exists():
        return totals
    for line in jsonl_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("type") != "message":
            continue
        msg = entry.get("message", {})
        if msg.get("role") != "assistant":
            continue
        totals["request_count"] += 1
        usage = msg.get("usage", {})
        totals["input_tokens"]       += usage.get("input",       0)
        totals["output_tokens"]      += usage.get("output",      0)
        totals["cache_read_tokens"]  += usage.get("cacheRead",   0)
        totals["cache_write_tokens"] += usage.get("cacheWrite",  0)
        totals["total_tokens"]       += usage.get("totalTokens", 0)
        cost = usage.get("cost", {})
        totals["cost_usd"] += cost.get("total", 0.0)
    totals["cost_usd"] = round(totals["cost_usd"], 6)
    return totals
