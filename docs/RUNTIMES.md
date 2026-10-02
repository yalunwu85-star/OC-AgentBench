# Agent runtimes

All adapters run containers as their configured image user (the supplied recipes
use root inside the container), copy only task inputs, and write to
`/tmp_workspace`. The Docker socket and host home directory are not mounted.
Use dedicated evaluation machines and limited provider credentials.

| Adapter | Environment variable | Default local tag | Runtime contract |
| --- | --- | --- | --- |
| `codex` | `DOCKER_IMAGE_CODEX` | `xperiencebench-codex:local` | `codex` CLI, bash, python3, Node |
| `openclaw` | `DOCKER_IMAGE` | `xperiencebench-openclaw:local` | Configured `openclaw` CLI and gateway |
| `hermesagent` | `HERMES_DOCKER_IMAGE` | `xperiencebench-hermes:local` | `/opt/hermes`, `/opt/hermes/.venv/bin/python3`, compatible `run_agent.AIAgent` |
| `claudecode` | `DOCKER_IMAGE_CLAUDECODE` | `xperiencebench-claudecode:local` | Original custom `/claude_code/start.sh`, source and transcript writer |

## Prebuilt images and included build recipes

Image archives are kept outside Git, in
[XperienceBench-Images/linux-amd64](https://huggingface.co/datasets/wuyalunnn/OC-AgentBench/tree/v1.0/XperienceBench-Images/linux-amd64)
within the [OC-AgentBench dataset repository](https://huggingface.co/datasets/wuyalunnn/OC-AgentBench).
If the repository is private, first sign in with `hf auth login`. From the code
repository root, download only image assets:

```bash
python -m pip install -r requirements-hf.txt
export DATASET_REPO=wuyalunnn/OC-AgentBench
export DATASET_REVISION=v1.0
hf download "$DATASET_REPO" --repo-type dataset --revision "$DATASET_REVISION" \
  --include 'XperienceBench-Images/**' --local-dir data
```

These commands use release `v1.0`. Verify the downloaded archives before
loading them:

```bash
(cd data/XperienceBench-Images/linux-amd64 && sha256sum -c SHA256SUMS)
docker load -i data/XperienceBench-Images/linux-amd64/xperiencebench-codex-0.121.0-linux-amd64.tar.gz
docker load -i data/XperienceBench-Images/linux-amd64/xperiencebench-openclaw-2026.3.11-linux-amd64.tar.gz
```

On macOS, use `shasum -a 256 -c SHA256SUMS` instead. The archives target
**Linux amd64** and restore the default local tags.

To build the same target platform yourself, run from the code repository root:

```bash
docker build --platform linux/amd64 -f docker/Dockerfile --target codex -t xperiencebench-codex:local .
docker build --platform linux/amd64 -f docker/Dockerfile --target openclaw -t xperiencebench-openclaw:local .
```

Defaults are Codex `0.121.0` and OpenClaw `2026.3.11`, pinned by the build recipe. These are version choices for compatibility,
not claims about the latest releases. Override with `--build-arg CODEX_VERSION=...`
or `--build-arg OPENCLAW_VERSION=...` only after checking adapter compatibility.
If the Debian package mirror is slow, override it with
`--build-arg DEBIAN_MIRROR=https://mirrors.ustc.edu.cn`; package signatures
continue to be checked by APT.

The base provides Python 3, pinned common document/data libraries, FFmpeg,
LibreOffice, Poppler, Tesseract, Pandoc, XeLaTeX with Chinese/LaTeX template
packages, ReportLab and CJK fonts. The task runtime also includes system Chromium,
Python Playwright and pytest-playwright, plus click, ruamel.yaml and deepdiff.
`PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=/usr/bin/chromium` lets the bundled KT-44
pytest configuration select the installed browser without downloading one.
For another harness, build the `task-runtime` target to reuse these task
dependencies; the `base` target is only the earlier foundation layer.
KT-03 can use either its preferred Pandoc/XeLaTeX
build or its bundled ReportLab fallback without installing those dependencies
during a task. It does not promise every
possible ML model, audio codec, network service or additional task-specific package.
Use a task-specific extension where necessary and record its image digest. OS
packages and the Node base tag are mutable, so preserve built images for exact
reproduction. No API key should be present during a build. `.dockerignore` allows
only the `docker/` files into the build context.

OpenClaw's gateway is bound to container loopback, with no Docker port publication.
The bundled gateway configuration has no authentication and must not be exposed
outside the container. The adapter selects chat/image models at runtime.

## External legacy runtimes

Hermes and the historical ClaudeCode runtime sources/build recipes are not part
of this repository. Their adapters are retained,
but **those images are not automatically downloadable or buildable here**. Supply
an audited compatible image via the environment variables above. Local historical
image tags are not public registry addresses.

Hermes expects `run_agent.AIAgent` to accept `model`, `api_key`, `base_url`,
`max_iterations`, `save_trajectories`, `verbose_logging` and `reasoning_config`,
and provide `run_conversation(prompt)`. The harness injects its benchmark config
and converts session trajectories to the grading transcript format. A version with
a different API requires adapting `src/agents/hermesagent/bench_runner.py`.

ClaudeCode expects `start.sh --add-dir /tmp_workspace -p PROMPT --model MODEL`
and `/claude_code/log/chat.json` (plus optional `usage.json`). It patches a known
usage-handling issue in the custom TypeScript runtime when the source is present.
An image containing only an official `claude` executable does not implement this
contract. Do not claim this adapter reproduces a generic CLI installation.
This legacy runtime has no verified reasoning-effort interface. The runner rejects
`--thinking` with `--agent-backend claudecode` before starting a container or
creating run artifacts; omit it to use the runtime's own default.

## Credentials and provider protocols

Codex uses `OPENROUTER_API_KEY` and `OPENROUTER_BASE_URL` with a Responses-compatible
API. Its optional image helper model is selected by `XPERIENCEBENCH_IMAGE_MODEL`;
when unset, the helper uses the task model. ClaudeCode uses `ANTHROPIC_API_KEY`/`ANTHROPIC_BASE_URL`, falling back to the
configured OpenRouter key and normalized endpoint. OpenClaw/Hermes accept the
standard key and URL or a custom provider JSON with `${ENV_VAR}` references.
Keep custom JSON in `configs/*.local.json`, which is ignored by Git.

Do not include authentication in endpoint URLs. A shell proxy address accessible
from the host may not be reachable from a container; use `HTTP_PROXY_INNER`,
`HTTPS_PROXY_INNER`, and `NO_PROXY_INNER` if needed.

### OpenClaw

Complete the README setup and load or build the OpenClaw image above, then copy the model configuration:

```bash
cp configs/models.example.json configs/models.local.json
```

Set `baseUrl`, the model `id` and supported `input` in the copied file; keep `apiKey` as `${OPENROUTER_API_KEY}` to use the key from `.env`. Replace `your-model-id` below with that same model ID; `custom/` is the provider name in the example configuration.

```bash
export OPENCLAW_MODEL=custom/your-model-id
python run.py --data-root data/XperienceBench_cn \
  --category Code_Software_Engineering_and_Security --condition current_task --limit 1 \
  --agent-backend openclaw --model "$OPENCLAW_MODEL" \
  --models-config configs/models.local.json
```

### Codex built-in web search

`CODEX_WEB_SEARCH` optionally sets Codex `web_search` to `disabled`, `cached`,
or `live` in the generated `config.toml`; unset preserves the CLI default.
A Responses-compatible gateway may still reject built-in search combined with
function tools. In that case, explicitly choosing `disabled` is a runtime
configuration to test, not proof of compatibility. Retain the generated
`config.toml` and report this tool-availability change with experiment results.
See [Codex configuration: web search](https://developers.openai.com/codex/config-basic/).
