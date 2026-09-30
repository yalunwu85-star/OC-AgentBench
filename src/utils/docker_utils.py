from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess as cleanup_subprocess
from src.utils import runtime_subprocess as subprocess
import tempfile
from pathlib import Path, PurePosixPath

logger = logging.getLogger(__name__)

DOCKER_IMAGE  = os.environ.get("DOCKER_IMAGE",   "xperiencebench-openclaw:local")
TMP_WORKSPACE = os.environ.get("TMP_WORKSPACE",  "/tmp_workspace")

BRAVE_API_KEY = os.environ.get("BRAVE_API_KEY", "")
# Upper bound for setup, copy and collection commands; agent runs use task deadlines.
DOCKER_TIMEOUT = 600
# Every benchmark container carries this label: docker rm -f $(docker ps -aq --filter label=xperiencebench)
CONTAINER_LABEL = ["--label", "xperiencebench"]


def docker_env(values: dict[str, str]) -> tuple[list[str], dict[str, str]]:
    """Return `-e NAME` flags plus the CLI environment, keeping values out of argv."""
    args: list[str] = []
    for key in values:
        args += ["-e", key]
    return args, {**os.environ, **values}


def remove_container(name: str, timeout: float = 15) -> str | None:
    """Best-effort cleanup; report failures without masking the task outcome."""
    try:
        result = cleanup_subprocess.run(["docker", "rm", "-f", name], capture_output=True,
                                text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return str(exc)
    if result.returncode and "No such container" not in (result.stderr or ""):
        return (result.stderr or "Container removal failed").strip()
    return None

def start_container(task_id: str, workspace_path: str, extra_env: str = "",
                    tmp_path: str = "") -> None:
    workspace = Path(workspace_path).expanduser()
    if not workspace.is_dir():
        raise RuntimeError(f"Workspace path does not exist or is not a directory: {workspace}")

    proxy_http = os.environ.get('HTTP_PROXY_INNER', '')
    proxy_https = os.environ.get('HTTPS_PROXY_INNER', '')
    env_map = {
        "http_proxy": proxy_http,
        "https_proxy": proxy_https,
        "HTTP_PROXY": proxy_http,
        "HTTPS_PROXY": proxy_https,
        "BRAVE_API_KEY": BRAVE_API_KEY,
        "no_proxy": '' if not proxy_http else os.environ.get('NO_PROXY_INNER', ''),
    }
    for line in extra_env.splitlines():
        key = line.strip()
        if not key or key.startswith("#"):
            continue
        value = os.environ.get(key, "")
        env_map[key] = value
        masked = "(set)" if value else "(empty)"
        logger.info("[%s] Injecting env var: %s=%s", task_id, key, masked)
    env_args, cli_env = docker_env(env_map)

    cmd = [
        "docker", "run", "-d",
        "--name", task_id,
        *CONTAINER_LABEL,
        *env_args,
        "-v", f"{workspace}:/app:ro",
        DOCKER_IMAGE,
        "/bin/bash", "-c", "tail -f /dev/null",
    ]
    logger.info("[%s] Starting container, mounting %s → /app (ro)", task_id, workspace)
    r = subprocess.run(cmd, capture_output=True, text=True, env=cli_env, timeout=DOCKER_TIMEOUT)
    if r.returncode != 0:
        raise RuntimeError(f"Container startup failed:\n{r.stderr}")
    logger.info("[%s] Container ID: %s", task_id, r.stdout.strip()[:12])

    if tmp_path and os.path.exists(tmp_path):
        mkdir_cmd = ["docker", "exec", task_id, "mkdir", "-p", "/tmp_workspace/tmp"]
        subprocess.run(mkdir_cmd, capture_output=True, timeout=DOCKER_TIMEOUT)

        cp_cmd = ["docker", "cp", f"{tmp_path}/.", f"{task_id}:/tmp_workspace/tmp/"]
        
        logger.info("[%s] Copying temp files: %s → /tmp_workspace/tmp", task_id, tmp_path)
        cp_r = subprocess.run(cp_cmd, capture_output=True, text=True, timeout=DOCKER_TIMEOUT)
        
        if cp_r.returncode != 0:
            logger.error("[%s] File copy failed: %s", task_id, cp_r.stderr)
        else:
            logger.info("[%s] Temp file copy complete", task_id)

def setup_workspace(task_id: str, thinking: str | None = None) -> None:
    logger.info("[%s] Copying /app → %s", task_id, TMP_WORKSPACE)
    r = subprocess.run(
        ["docker", "exec", task_id, "/bin/bash", "-c",
         f"mkdir -p {TMP_WORKSPACE} /root/.openclaw && cp -r /app/. {TMP_WORKSPACE} && chmod -R u+w {TMP_WORKSPACE}"],
        capture_output=True, text=True, timeout=DOCKER_TIMEOUT,
    )
    if r.returncode != 0:
        raise RuntimeError(f"Workspace copy failed:\n{r.stderr}")

    if thinking is not None:
        logger.info("[%s] Setting thinkingDefault to %s", task_id, thinking)
        thinking_result = subprocess.run(
            ["docker", "exec", task_id,
             "openclaw", "config", "set", "agents.defaults.thinkingDefault", thinking],
            capture_output=True, text=True, timeout=DOCKER_TIMEOUT,
        )
        if thinking_result.returncode != 0:
            raise RuntimeError(
                f"Failed to set thinkingDefault to {thinking}:\n{thinking_result.stderr}"
            )

    # Symlink OpenClaw workspace → TMP_WORKSPACE so the image tool's
    # media-local-roots check allows reading files under /tmp_workspace.
    subprocess.run(
        ["docker", "exec", task_id, "/bin/bash", "-c",
         f"rm -rf /root/.openclaw/workspace && ln -s {TMP_WORKSPACE} /root/.openclaw/workspace"],
        capture_output=True, text=True, timeout=DOCKER_TIMEOUT,
    )

def setup_skills(
    task_id: str,
    skills: str,
    skills_path: str,
    container_skills_root: str = "/root/skills",
) -> None:
    container_skills_root = container_skills_root.rstrip("/")
    subprocess.run(
        ["docker", "exec", task_id, "mkdir", "-p", container_skills_root],
        capture_output=True,
        text=True, timeout=DOCKER_TIMEOUT,
    )
    seen_dest_names: set[str] = set()
    for line in skills.splitlines():
        line = line.strip()
        if not line:
            continue
        src_rel = line.replace("\\", "/").strip("/")
        dest_name = PurePosixPath(src_rel).name
        if not dest_name:
            logger.warning("[%s] Invalid skill path %r, skipping", task_id, line)
            continue
        if dest_name in seen_dest_names:
            logger.warning(
                "[%s] Duplicate flattened skill target %s from %s, skipping",
                task_id,
                dest_name,
                line,
            )
            continue
        seen_dest_names.add(dest_name)
        subprocess.run(
            ["docker", "exec", task_id,
             "mkdir", "-p", f"{container_skills_root}/{dest_name}"],
            capture_output=True, text=True, timeout=DOCKER_TIMEOUT,
        )
        r = subprocess.run(
            ["docker", "cp",
             f"{skills_path}/{src_rel}/.", f"{task_id}:{container_skills_root}/{dest_name}/"],
            capture_output=True, text=True, timeout=DOCKER_TIMEOUT,
        )
        if r.returncode != 0:
            logger.warning(
                "[%s] Failed to copy skill %s to %s/%s: %s",
                task_id,
                line,
                container_skills_root,
                dest_name,
                r.stderr.strip(),
            )


def inject_openclaw_models(task_id: str, models_config: dict) -> None:
    """Inject custom models into ~/.openclaw/openclaw.json."""
    container_tmp_path = "/tmp/openclaw_models.json"
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as tmp_file:
        json.dump(models_config, tmp_file, indent=2)
        tmp_file_path = tmp_file.name

    try:
        cp_r = subprocess.run(
            ["docker", "cp", tmp_file_path, f"{task_id}:{container_tmp_path}"],
            capture_output=True, text=True, timeout=DOCKER_TIMEOUT,
        )
        if cp_r.returncode != 0:
            raise RuntimeError(f"Failed to copy models config into container:\n{cp_r.stderr}")

        inject_cmd = f"""python3 - <<'PY'
import json
import pathlib

config_path = pathlib.Path('/root/.openclaw/openclaw.json')
models_path = pathlib.Path('{container_tmp_path}')

config = json.loads(config_path.read_text()) if config_path.exists() else {{}}
models = json.loads(models_path.read_text())
config['models'] = models

config_path.write_text(json.dumps(config, indent=2))
PY"""
        r = subprocess.run(
            ["docker", "exec", task_id, "/bin/bash", "-c", inject_cmd],
            capture_output=True, text=True, timeout=DOCKER_TIMEOUT,
        )
        if r.returncode != 0:
            raise RuntimeError(f"Failed to inject models config:\n{r.stderr}")
    finally:
        Path(tmp_file_path).unlink(missing_ok=True)

    logger.info("[%s] Injected custom models config", task_id)


def run_warmup(
    task_id: str,
    warmup: str,
    *,
    detach_background: bool = False,
) -> None:
    """Execute warmup bash commands line by line inside the container (skip blank lines and comments)."""
    if not warmup.strip():
        return
    commands = [
        line.strip()
        for line in warmup.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if not commands:
        return

    logger.info("[%s] Running warmup (%d commands)", task_id, len(commands))
    for idx, cmd in enumerate(commands, start=1):
        logger.info("[%s] warmup: %s", task_id, cmd)
        stripped_cmd = cmd.rstrip()
        if detach_background and stripped_cmd.endswith("&"):
            background_cmd = stripped_cmd[:-1].strip()
            log_path = f"/tmp/xperiencebench_warmup_{idx}.log"
            wrapped = (
                f"cd {TMP_WORKSPACE} && "
                f"nohup /bin/bash -lc {shlex.quote(background_cmd)} "
                f"> {shlex.quote(log_path)} 2>&1 < /dev/null &"
            )
            r = subprocess.run(
                ["docker", "exec", task_id, "/bin/bash", "-lc", wrapped],
                capture_output=True,
                text=True, timeout=DOCKER_TIMEOUT,
            )
            if r.returncode != 0:
                raise RuntimeError(
                    f"Warmup background command failed: {cmd!r}\n{r.stderr}"
                )
            continue

        r = subprocess.run(
            ["docker", "exec", task_id, "/bin/bash", "-c", cmd],
            capture_output=True, text=True, timeout=DOCKER_TIMEOUT,
        )
        if r.returncode != 0:
            raise RuntimeError(f"Warmup command failed: {cmd!r}\n{r.stderr}")


def run_background(task_id: str, bash_cmd: str, log_path: Path,
                   env: dict[str, str] | None = None) -> subprocess.Popen:
    """Start bash_cmd via docker exec; env values are passed without appearing in argv."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = log_path.open("w", encoding="utf-8")
    env_args, cli_env = docker_env(env or {})
    proc = subprocess.Popen(
        ["docker", "exec", *env_args, task_id, "/bin/bash", "-c",
         f"cd {TMP_WORKSPACE} && {bash_cmd}"],
        stdout=log_file,
        stderr=subprocess.STDOUT,
        encoding="utf-8",
        env=cli_env,
    )
    proc._log_file = log_file
    logger.info("[%s] Started process PID=%s → %s", task_id, proc.pid, log_path)
    return proc


def close_proc_log(proc: subprocess.Popen) -> None:
    """Close the log file handle created by run_background."""
    log_file = getattr(proc, "_log_file", None)
    if log_file and not log_file.closed:
        log_file.close()


def collect_output_from_container(
    task_id: str,
    output_dir: Path,
    *,
    include_workspace_changes: bool = False,
) -> None:
    """Collect task output files from the container to output_dir/task_output/.

    Collection strategy:
      1. All files under /tmp/openclaw/ (agent session logs, etc.)
      2. Task output files under /tmp_workspace/results/
      3. Optionally, the full /tmp_workspace/ tree for runners that may write
         deliverables outside results/
    """
    task_output_dir = output_dir / "task_output"
    _safe_collection_directory(task_output_dir)
    # Container-controlled log members must never occupy the workspace destination.
    logs_out = task_output_dir / "logs"
    workspace_out = task_output_dir / "workspace"
    _safe_collection_directory(logs_out, fresh=True)
    _safe_collection_directory(workspace_out, fresh=True)
    _copy_dir_from_container(task_id, "/tmp/openclaw/.", str(logs_out))
    _safe_collection_directory(workspace_out)

    if include_workspace_changes:
        ok = _copy_dir_from_container(task_id, f"{TMP_WORKSPACE}/.", str(workspace_out))
        if not ok:
            logger.warning("[%s] workspace directory does not exist or is empty", task_id)
        return

    results_out = workspace_out / "results"
    _safe_collection_directory(results_out, fresh=True)
    ok = _copy_dir_from_container(
        task_id, f"{TMP_WORKSPACE}/results/.", str(results_out),
    )
    if not ok:
        logger.warning("[%s] results/ directory does not exist or is empty", task_id)


def _safe_collection_directory(path: Path, *, fresh: bool = False) -> None:
    """Only copy into host-owned directories without symlink ancestors."""
    path = path.absolute()
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError(f"Unsafe collection path (symbolic link): {part}")
    if fresh and path.exists():
        raise ValueError(f"Collection destination already exists: {path}")
    path.mkdir(parents=True, exist_ok=not fresh)


def _copy_dir_from_container(task_id: str, src: str, dest: str) -> bool:
    r = subprocess.run(
        ["docker", "cp", f"{task_id}:{src}", dest],
        capture_output=True, text=True, timeout=DOCKER_TIMEOUT,
    )
    if r.returncode == 0:
        logger.info("[%s] Collected container directory %s → %s", task_id, src, dest)
        return True
    return False
