from __future__ import annotations

import logging
import shlex
from src.utils import runtime_subprocess as subprocess
import tempfile
from pathlib import Path, PurePosixPath

from src.utils.docker_utils import DOCKER_TIMEOUT

logger = logging.getLogger(__name__)

CODEX_PROMPT_PATH = "/tmp/codex_prompt.txt"

def _copy_text_to_container(task_id: str, container_path: str, text: str) -> None:
    container_target = str(PurePosixPath(container_path))
    container_dir = str(PurePosixPath(container_target).parent)
    mkdir_result = subprocess.run(
        ["docker", "exec", "-u", "0", task_id, "mkdir", "-p", container_dir],
        capture_output=True,
        text=True, timeout=DOCKER_TIMEOUT,
    )
    if mkdir_result.returncode != 0:
        raise RuntimeError(
            f"Failed to create container directory {container_dir}:\n"
            f"{mkdir_result.stderr or mkdir_result.stdout}"
        )

    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as tmp_file:
        tmp_file.write(text)
        tmp_host_path = tmp_file.name

    try:
        copy_result = subprocess.run(
            ["docker", "cp", tmp_host_path, f"{task_id}:{container_target}"],
            capture_output=True,
            text=True, timeout=DOCKER_TIMEOUT,
        )
        if copy_result.returncode != 0:
            debug_result = subprocess.run(
                [
                    "docker",
                    "exec",
                    "-u",
                    "0",
                    task_id,
                    "/bin/sh",
                    "-lc",
                    (
                        f"ls -ld {shlex.quote(container_dir)} 2>&1 || true; "
                        "id -un 2>/dev/null || true; "
                        "echo HOME=${HOME:-}"
                    ),
                ],
                capture_output=True,
                text=True, timeout=DOCKER_TIMEOUT,
            )
            raise RuntimeError(
                "Failed to copy file into container:\n"
                f"{copy_result.stderr}"
                "Container path debug:\n"
                f"{debug_result.stdout or debug_result.stderr}"
            )
    finally:
        Path(tmp_host_path).unlink(missing_ok=True)


def load_skill_documents(
    skills: str,
    skills_path: str,
    container_skill_root: str = "/root/skills",
) -> list[dict[str, str]]:
    loaded_skills: list[dict[str, str]] = []
    for line in skills.splitlines():
        skill_name = line.strip()
        if not skill_name:
            continue

        skill_rel = skill_name.replace("\\", "/").strip("/")
        skill_leaf = PurePosixPath(skill_rel).name
        if not skill_leaf:
            logger.warning("Invalid skill path for Codex prompt injection: %s", skill_name)
            continue

        skill_file = Path(skills_path) / skill_rel / "SKILL.md"
        if not skill_file.is_file():
            logger.warning("Skill file not found for Codex prompt injection: %s", skill_file)
            continue

        content = skill_file.read_text(encoding="utf-8")
        content = content.replace("{baseDir}", f"{container_skill_root}/{skill_leaf}")
        loaded_skills.append({"name": skill_name, "content": content})

    return loaded_skills


def prepare_codex_prompt(task_id: str, prompt: str, container_path: str = CODEX_PROMPT_PATH) -> str:
    _copy_text_to_container(task_id, container_path, prompt)
    return container_path
