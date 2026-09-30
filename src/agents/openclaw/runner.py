from __future__ import annotations

import logging
import os
from src.utils import runtime_subprocess as subprocess
import time
from pathlib import Path


from src.agents.base import AgentExecution, AgentTaskSpec, BaseAgent
from src.utils.grading import extract_usage_from_jsonl
from src.utils.execution_validity import openclaw_provider_error
from src.utils.docker_utils import (
    DOCKER_TIMEOUT,
    inject_openclaw_models,
    run_background,
    run_warmup,
    setup_skills,
    setup_workspace,
    start_container,
)


logger = logging.getLogger(__name__)


class OpenClawAgent(BaseAgent):
    def __init__(
        self,
        gateway_port: int,
        openrouter_api_key: str = "",
        openrouter_base_url: str = "https://openrouter.ai/api/v1",
        image_model: str | None = None,
    ) -> None:
        self.gateway_port = gateway_port
        self.openrouter_api_key = openrouter_api_key
        self.openrouter_base_url = openrouter_base_url
        self.image_model = image_model if image_model is not None else os.environ.get("OPENCLAW_IMAGE_MODEL", "").strip()

    @property
    def expects_gateway(self) -> bool:
        return True

    @property
    def transcript_container_path(self) -> str:
        return "/root/.openclaw/agents/main/sessions/chat.jsonl"

    def run_task(self, spec: AgentTaskSpec) -> AgentExecution:
        gateway_proc = None
        agent_proc = None
        elapsed_time = float(spec.timeout_seconds)

        try:
            exec_path = os.path.join(spec.workspace_path, "exec")
            tmp_path = os.path.join(spec.workspace_path, "tmp")
            os.makedirs(exec_path, exist_ok=True)

            start_container(
                spec.task_id,
                exec_path,
                extra_env=spec.task.get("env", ""),
                tmp_path=tmp_path,
            )

            setup_workspace(spec.task_id, thinking=spec.thinking)
            setup_skills(spec.task_id, spec.task.get("skills", ""), spec.task.get("skills_path", ""))
            run_warmup(spec.task_id, spec.task.get("warmup", ""))

            if spec.models_config:
                inject_openclaw_models(spec.task_id, spec.models_config)

            self._set_model(spec.task_id, spec.model)
            self._inject_openrouter_key(spec.task_id)
            image_model = self.image_model or spec.model
            self._set_image_model(spec.task_id, image_model)

            gateway_proc = run_background(
                spec.task_id,
                bash_cmd=f"openclaw gateway --port {self.gateway_port}",
                log_path=spec.output_dir / "gateway.log",
                env={
                    "OPENROUTER_API_KEY": self.openrouter_api_key,
                    "OPENROUTER_BASE_URL": self.openrouter_base_url,
                },
            )
            self._wait_for_gateway(spec.task_id, gateway_proc)

            safe_prompt = spec.prompt.replace("'", "'\\''")
            start_time = time.perf_counter()
            agent_proc = run_background(
                spec.task_id,
                # Let the host enforce the benchmark deadline before OpenClaw's own timeout.
                bash_cmd=f"openclaw agent --json --session-id chat --timeout {spec.timeout_seconds + 30} --message '{safe_prompt}'",
                log_path=spec.output_dir / "agent.log",
            )

            logger.info("[%s] Waiting for agent to finish...", spec.task_id)
            timed_out = False
            try:
                agent_proc.wait(timeout=spec.timeout_seconds)
                elapsed_time = time.perf_counter() - start_time
                logger.info(
                    "[%s] Agent finished successfully, elapsed: %.2f seconds",
                    spec.task_id,
                    elapsed_time,
                )
            except subprocess.TimeoutExpired:
                logger.info("[%s] Agent timed out...", spec.task_id)
                elapsed_time = float(spec.timeout_seconds)
                timed_out = True
                agent_proc.kill()
                agent_proc.wait()

            logger.info("[%s] Agent exit code: %s", spec.task_id, agent_proc.returncode)
            return AgentExecution(
                elapsed_time=elapsed_time,
                error=None if timed_out or agent_proc.returncode == 0 else f"OpenClaw failed (exit={agent_proc.returncode})",
                gateway_proc=gateway_proc,
                agent_proc=agent_proc,
                timed_out=timed_out,
            )
        except Exception as exc:
            logger.error("[%s] Execution error: %s", spec.task_id, exc)
            return AgentExecution(
                elapsed_time=float(spec.timeout_seconds),
                error=str(exc),
                gateway_proc=gateway_proc,
                agent_proc=agent_proc,
            )

    def collect_usage(self, task_id: str, output_dir: Path, elapsed_time: float) -> dict:
        transcript_host = output_dir / "chat.jsonl"
        output_dir.mkdir(parents=True, exist_ok=True)
        r_cp = subprocess.run(
            ["docker", "cp", f"{task_id}:{self.transcript_container_path}", str(transcript_host)],
            capture_output=True,
            text=True, timeout=DOCKER_TIMEOUT,
        )
        if r_cp.returncode == 0 and transcript_host.exists():
            usage = extract_usage_from_jsonl(transcript_host)
        else:
            logger.warning("[%s] Transcript copy failed: %s", task_id, r_cp.stderr.strip())
            usage = {
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "total_tokens": 0,
                "cost_usd": 0.0,
                "request_count": 0,
            }
        usage["elapsed_time"] = round(elapsed_time, 2)
        return usage

    def get_execution_error(
        self, task_id: str, output_dir: Path, execution: AgentExecution
    ) -> str | None:
        return execution.error or openclaw_provider_error(output_dir / "chat.jsonl")

    def _wait_for_gateway(self, task_id: str, gateway_proc: subprocess.Popen, timeout: float = 60) -> None:
        """Poll until the gateway accepts TCP connections; fail if it exits or never listens."""
        probe = f"exec 3<>/dev/tcp/127.0.0.1/{self.gateway_port}"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if gateway_proc.poll() is not None:
                raise RuntimeError(f"OpenClaw gateway exited early (exit={gateway_proc.returncode}); see gateway.log")
            r = subprocess.run(["docker", "exec", task_id, "/bin/bash", "-c", probe],
                               capture_output=True, timeout=DOCKER_TIMEOUT)
            if r.returncode == 0:
                logger.info("[%s] Gateway ready on port %s", task_id, self.gateway_port)
                return
            time.sleep(0.5)
        raise RuntimeError(f"OpenClaw gateway not ready after {timeout:.0f}s; see gateway.log")

    def _set_model(self, task_id: str, model: str) -> None:
        r = subprocess.run(
            ["docker", "exec", task_id, "/bin/bash", "-c", f"openclaw models set '{model}'"],
            capture_output=True,
            text=True, timeout=DOCKER_TIMEOUT,
        )
        if r.returncode != 0:
            raise RuntimeError(f"Model setup failed:\n{r.stderr}")
        logger.info("[%s] Model set: %s", task_id, model)

    def _inject_openrouter_key(self, task_id: str) -> None:
        if not self.openrouter_api_key:
            return

        # Credentials travel only on stdin, never in process arguments or scripts.
        script = """import json, pathlib, sys
p = pathlib.Path('/root/.openclaw/agents/main/agent/auth-profiles.json')
d = json.loads(p.read_text()) if p.exists() else {'version': 1, 'profiles': {}}
d.setdefault('profiles', {})['openrouter:default'] = {
    'type': 'api_key', 'provider': 'openrouter', 'key': sys.stdin.read()}
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text(json.dumps(d, indent=2))
p.chmod(0o600)
"""
        try:
            r = subprocess.run(
                ["docker", "exec", "-i", task_id, "python3", "-c", script],
                input=self.openrouter_api_key, capture_output=True,
                text=True, timeout=DOCKER_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError("OpenRouter key injection timed out") from None
        if r.returncode != 0:
            # Do not echo runtime stderr: an error could repeat the supplied secret.
            raise RuntimeError(f"OpenRouter key injection failed (exit={r.returncode})")
        logger.info("[%s] Injected OPENROUTER_API_KEY into auth-profiles.json", task_id)

    def _set_image_model(self, task_id: str, model: str) -> None:
        r = subprocess.run(
            ["docker", "exec", task_id, "/bin/bash", "-c", f"openclaw config set agents.defaults.imageModel.primary '{model}'"],
            capture_output=True,
            text=True, timeout=DOCKER_TIMEOUT,
        )
        if r.returncode != 0:
            raise RuntimeError(f"Image model setup failed:\n{r.stderr}")
        logger.info("[%s] imageModel set: %s", task_id, model)
