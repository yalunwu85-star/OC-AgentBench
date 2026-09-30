# Security and credentials

- Store provider secrets in a local `.env` or environment variables. The harness
  loads only the repository-root `.env`; existing environment values take priority.
- Example JSON uses `${ENV_VAR}` references. Inline credentials in model-provider
  configuration are rejected. Missing referenced values fail before execution.
- The host logger redacts known environment credentials and common token formats.
  Original agent logs, transcripts and outputs can still contain sensitive data;
  they are private artifacts and must not be uploaded without separate review.
- Docker images must not contain baked-in credentials. The build context excludes
  local configuration and task data. Never pass secrets as build arguments.
- Docker API access and container environment variables are accessible to local
  Docker administrators. These containers are an evaluation boundary, not a
  hardened defense against a malicious host administrator or kernel exploit.
- Only `exec/` is mounted from the task workspace. Credentials are passed to
  Docker through the CLI environment, not command-line arguments, but remain
  visible to `docker inspect` on the agent container.
- Grading runs in a fresh network-less container without provider credentials.
  It receives the agent's workspace with any agent-created `gt/` and all symbolic
  links removed, then the real ground truth. Agent processes never share a
  container with ground truth. Dataset Markdown, grading code, and host
  credentials are never mounted as a complete repository.
- Dataset warmup and grader blocks are executable code. Use trusted revisions.
- Do not include keys, private endpoints, transcripts, or account details in public
  issue reports. Send credential-free reproductions. Rotate any credential that
  was accidentally published; deleting the latest file does not remove Git history.

Run `python scripts/check_release.py` to check publishable repository files.
